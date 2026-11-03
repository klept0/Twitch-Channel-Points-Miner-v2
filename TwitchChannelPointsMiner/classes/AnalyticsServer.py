import hmac
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from threading import Thread

import pandas as pd
from flask import Flask, Response, cli, render_template, request

from TwitchChannelPointsMiner.classes.Settings import Settings
from TwitchChannelPointsMiner.utils import download_file

cli.show_server_banner = lambda *_: None
logger = logging.getLogger(__name__)

# On first load the log viewer only gets the end of the log file, not all of it
LOG_TAIL_BYTES = 256 * 1024


def streamer_name(filename):
    # Not filename.strip(".json"): strip() removes those characters, so "jackson" became "jack"
    return filename[: -len(".json")] if filename.endswith(".json") else filename


def streamers_available():
    path = Settings.analytics_path
    return [
        f
        for f in os.listdir(path)
        if os.path.isfile(os.path.join(path, f)) and f.endswith(".json")
    ]


def aggregate(df, freq="30Min"):
    df_base_events = df[(df.z == "Watch") | (df.z == "Claim")]
    df_other_events = df[(df.z != "Watch") & (df.z != "Claim")]

    be = df_base_events.groupby(
        [pd.Grouper(freq=freq, key="datetime"), "z"]).max()
    be = be.reset_index()

    oe = df_other_events.groupby(
        [pd.Grouper(freq=freq, key="datetime"), "z"]).max()
    oe = oe.reset_index()

    result = pd.concat([be, oe])
    return result


def filter_datas(start_date, end_date, datas):
    # Note: https://stackoverflow.com/questions/4676195/why-do-i-need-to-multiply-unix-timestamps-by-1000-in-javascript
    start_date = (
        datetime.strptime(start_date, "%Y-%m-%d").timestamp() * 1000
        if start_date is not None
        else 0
    )
    end_date = (
        datetime.strptime(end_date, "%Y-%m-%d")
        if end_date is not None
        else datetime.now()
    ).replace(hour=23, minute=59, second=59).timestamp() * 1000

    original_series = datas.get("series", [])
    datas["series"] = sorted(
        (point for point in original_series if start_date <= point["x"] <= end_date),
        key=lambda point: (point["x"], point["y"]),
    )

    # If no data is found within the timeframe, that usually means the streamer hasn't streamed within that timeframe
    # We create a series that shows up as a straight line on the dashboard, with 'No Stream' as labels
    if len(datas["series"]) == 0:
        # Use the last known balance from before the provided timeframe, if there is one
        before = [point for point in original_series if point["x"] <= start_date]
        if before:
            last_balance = max(before, key=lambda point: (point["x"], point["y"]))["y"]
            datas["series"] = [
                {"x": start_date, "y": last_balance, "z": "No Stream"},
                {"x": end_date, "y": last_balance, "z": "No Stream"},
            ]

    datas["annotations"] = sorted(
        (
            annotation
            for annotation in datas.get("annotations", [])
            if start_date <= annotation["x"] <= end_date
        ),
        key=lambda annotation: annotation["x"],
    )

    return datas


def read_json(streamer, return_response=True):
    start_date = request.args.get("startDate", type=str)
    end_date = request.args.get("endDate", type=str)

    path = Settings.analytics_path
    streamer = streamer if streamer.endswith(".json") else f"{streamer}.json"

    # Only plain file names inside the analytics folder ("\" is a separator on Windows)
    is_plain_name = streamer == os.path.basename(streamer) and "\\" not in streamer

    # Check if the file exists before attempting to read it
    if not is_plain_name or not os.path.isfile(os.path.join(path, streamer)):
        error_message = f"File '{streamer}' not found."
        logger.error(error_message)
        if return_response:
            return Response(json.dumps({"error": error_message}), status=404, mimetype="application/json")
        else:
            return {"error": error_message}

    try:
        with open(os.path.join(path, streamer), 'r') as file:
            data = json.load(file)
    except json.JSONDecodeError as e:
        error_message = f"Error decoding JSON in file '{streamer}': {str(e)}"
        logger.error(error_message)
        if return_response:
            return Response(json.dumps({"error": error_message}), status=500, mimetype="application/json")
        else:
            return {"error": error_message}

    # Handle filtering data, if applicable
    filtered_data = filter_datas(start_date, end_date, data)
    if return_response:
        return Response(json.dumps(filtered_data), status=200, mimetype="application/json")
    else:
        return filtered_data


def last_point(streamer):
    """Return (last_activity, points) from the newest point of a streamer's series.

    Reads the file once and skips date filtering: /streamers calls this for every
    streamer on each page load.
    """
    try:
        with open(os.path.join(Settings.analytics_path, streamer), "r") as file:
            series = json.load(file).get("series", [])
    except (OSError, ValueError, AttributeError):
        return 0, 0
    if not series:
        return 0, 0
    newest = max(series, key=lambda point: (point["x"], point["y"]))
    return newest["x"], newest["y"]


def json_all():
    return Response(
        json.dumps(
            [
                {
                    "name": streamer_name(streamer),
                    "data": read_json(streamer, return_response=False),
                }
                for streamer in streamers_available()
            ]
        ),
        status=200,
        mimetype="application/json",
    )


def index(refresh=5, days_ago=7):
    return render_template(
        "charts.html",
        refresh=(refresh * 60 * 1000),
        daysAgo=days_ago,
    )


def streamers():
    result = []
    for s in sorted(streamers_available()):
        last_activity, points = last_point(s)
        result.append({"name": s, "points": points, "last_activity": last_activity})
    return Response(json.dumps(result), status=200, mimetype="application/json")


def find_log_file(username):
    logs_path = Path().absolute() / "logs"
    log_file = logs_path / f"{username}.log"
    if log_file.is_file():
        return log_file
    # With auto_clear=False every run writes its own <username>.<timestamp>.log
    runs = sorted(logs_path.glob(f"{username}.*.log"), key=lambda p: p.stat().st_mtime)
    return runs[-1] if runs else None


def read_log(username):
    """New log text since the byte offset the page sent.

    The response carries the next offset in X-Log-Offset. X-Log-Reset is 1 when the
    text starts from the beginning (first load, or the file was rotated), so the
    page should clear what it shows. A big log is cut to its last LOG_TAIL_BYTES.
    """
    log_file = find_log_file(username)
    if log_file is None:
        return Response("Log file not found.", status=404, mimetype="text/plain")

    offset = request.args.get("offset", 0, type=int)
    size = log_file.stat().st_size
    if offset < 0 or offset > size:  # rotated: the file is new and smaller
        offset = 0

    with open(log_file, "rb") as file:
        if offset == 0 and size > LOG_TAIL_BYTES:
            file.seek(size - LOG_TAIL_BYTES)
            file.readline()  # start on a whole line
        else:
            file.seek(offset)
        content = file.read()
        next_offset = file.tell()

    response = Response(
        content.decode("utf-8", errors="replace"), status=200, mimetype="text/plain"
    )
    response.headers["X-Log-Offset"] = str(next_offset)
    response.headers["X-Log-Reset"] = "1" if offset == 0 else "0"
    return response


def download_assets(assets_folder, required_files):
    Path(assets_folder).mkdir(parents=True, exist_ok=True)
    logger.info(f"Downloading assets to {assets_folder}")

    for f in required_files:
        if os.path.isfile(os.path.join(assets_folder, f)) is False:
            if (
                download_file(os.path.join("assets", f),
                              os.path.join(assets_folder, f))
                is True
            ):
                logger.info(f"Downloaded {f}")


def check_assets():
    required_files = [
        "banner.png",
        "charts.html",
        "script.js",
        "style.css",
        "dark-theme.css",
    ]
    assets_folder = os.path.join(Path().absolute(), "assets")
    if os.path.isdir(assets_folder) is False:
        logger.info(f"Assets folder not found at {assets_folder}")
        download_assets(assets_folder, required_files)
    else:
        for f in required_files:
            if os.path.isfile(os.path.join(assets_folder, f)) is False:
                logger.info(f"Missing file {f} in {assets_folder}")
                download_assets(assets_folder, required_files)
                break


def credentials_match(given, expected):
    # Constant-time comparison; encode so non-ASCII passwords work too
    return hmac.compare_digest((given or "").encode(), expected.encode())


class AnalyticsServer(Thread):

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5000,
        refresh: int = 5,
        days_ago: int = 7,
        username: str = None,
        auth: tuple = None,
    ):
        super(AnalyticsServer, self).__init__()

        check_assets()

        self.host = host
        self.port = port
        self.refresh = refresh
        self.days_ago = days_ago
        self.username = username

        self.app = Flask(
            __name__,
            template_folder=os.path.join(Path().absolute(), "assets"),
            static_folder=os.path.join(Path().absolute(), "assets"),
        )

        if auth is not None:
            auth_user, auth_password = auth

            @self.app.before_request
            def require_login():
                given = request.authorization
                if (
                    given is None
                    or not credentials_match(given.username, auth_user)
                    or not credentials_match(given.password, auth_password)
                ):
                    return Response(
                        "Login required.",
                        status=401,
                        headers={"WWW-Authenticate": 'Basic realm="Analytics"'},
                    )

        elif host not in ("127.0.0.1", "localhost", "::1"):
            logger.warning(
                f"Analytics on {host} has no login: anyone who can reach port {port} "
                "can see your points and your log. Set auth=(user, password)."
            )

        self.app.add_url_rule(
            "/",
            "index",
            index,
            defaults={"refresh": refresh, "days_ago": days_ago},
            methods=["GET"],
        )
        self.app.add_url_rule("/streamers", "streamers",
                              streamers, methods=["GET"])
        self.app.add_url_rule(
            "/json/<string:streamer>", "json", read_json, methods=["GET"]
        )
        self.app.add_url_rule("/json_all", "json_all",
                              json_all, methods=["GET"])
        self.app.add_url_rule(
            "/log",
            "log",
            read_log,
            defaults={"username": username},
            methods=["GET"],
        )

    def run(self):
        logger.info(
            f"Analytics running on http://{self.host}:{self.port}/",
            extra={"emoji": ":globe_with_meridians:"},
        )
        self.app.run(host=self.host, port=self.port,
                     threaded=True, debug=False)
