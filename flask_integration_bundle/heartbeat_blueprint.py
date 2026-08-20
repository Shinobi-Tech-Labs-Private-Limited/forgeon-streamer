from __future__ import annotations

import os
from typing import Any

from flask import Blueprint, Flask, current_app, jsonify, request

from .heartbeat_client import HeartbeatClient, HeartbeatServiceError

heartbeat_bp = Blueprint("heartbeat", __name__, url_prefix="/api/heartbeat")


def _client() -> HeartbeatClient:
    return HeartbeatClient(
        base_url=current_app.config["HEARTBEAT_SERVICE_URL"],
        timeout_seconds=current_app.config["HEARTBEAT_TIMEOUT_SECONDS"],
    )


def _service_response(operation: Any) -> tuple[Any, int] | Any:
    try:
        return jsonify(operation())
    except HeartbeatServiceError as error:
        return jsonify({"detail": "Heartbeat service unavailable", "error": str(error)}), 502


@heartbeat_bp.get("/hr")
def latest_heart_rate() -> tuple[Any, int] | Any:
    return _service_response(_client().latest)


@heartbeat_bp.get("/device")
def device_status() -> tuple[Any, int] | Any:
    return _service_response(_client().device)


@heartbeat_bp.get("/recording")
def recording_status() -> tuple[Any, int] | Any:
    return _service_response(_client().recording_status)


@heartbeat_bp.post("/recording/start")
def start_recording() -> tuple[Any, int] | Any:
    body = request.get_json(silent=True) or {}
    recording_format = body.get("format")
    if recording_format not in ("csv", "jsonl"):
        return jsonify({"detail": "format must be 'csv' or 'jsonl'"}), 400
    return _service_response(lambda: _client().start_recording(recording_format))


@heartbeat_bp.post("/recording/stop")
def stop_recording() -> tuple[Any, int] | Any:
    return _service_response(_client().stop_recording)


def register_heartbeat(app: Flask) -> None:
    """Configure and register the Heartbeat proxy routes on a Flask app."""
    app.config.setdefault(
        "HEARTBEAT_SERVICE_URL",
        os.environ.get("HEARTBEAT_SERVICE_URL", "http://127.0.0.1:8000"),
    )
    app.config.setdefault("HEARTBEAT_TIMEOUT_SECONDS", 5.0)
    app.register_blueprint(heartbeat_bp)
