"""Small rotating operational logs without request bodies or credentials."""
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from flask import g, request
from flask.logging import default_handler


def configure_logging(app):
    directory = Path(os.environ.get("HOMEWORK_LOG_DIR", str(Path(app.root_path) / "logs")))
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / "application.log", maxBytes=5*1024*1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    handler.setLevel(logging.INFO)
    app.logger.addHandler(handler)
    app.logger.removeHandler(default_handler)
    app.logger.setLevel(logging.INFO)

    @app.before_request
    def start_timer():
        g.request_started = time.monotonic()

    @app.after_request
    def record_request(response):
        duration = (time.monotonic() - g.get('request_started', time.monotonic()))*1000
        app.logger.info('%s %s status=%s duration_ms=%.1f', request.method, request.endpoint or 'unknown', response.status_code, duration)
        return response
