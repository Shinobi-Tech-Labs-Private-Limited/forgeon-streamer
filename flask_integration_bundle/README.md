# Heartbeat → Flask integration bundle

This bundle intentionally contains no UI. Heartbeat remains a separate local
sidecar process because it owns an asyncio event loop, Bluetooth reconnects,
and WebSockets. Flask calls it through HTTP. This avoids running BLE tasks in
Flask request workers or creating duplicate strap connections when Flask has
multiple workers.

## Files to copy into the Flask app

Copy these two files into the Flask application's Python package (the folder
that contains its app factory or main Flask module):

- `heartbeat_client.py`
- `heartbeat_blueprint.py`

Add `httpx>=0.27` to the Flask application's dependency file. If it already
uses `requests`, Codex may adapt the client to that library instead.

## Register the Blueprint

For an app factory:

```python
from .heartbeat_blueprint import register_heartbeat

def create_app():
    app = Flask(__name__)
    # existing setup...
    register_heartbeat(app)
    return app
```

Adjust the import (`.` or no `.`) to match the Flask package layout.

Configuration defaults to `http://127.0.0.1:8000`. Override it with:

```bash
export HEARTBEAT_SERVICE_URL=http://127.0.0.1:8000
```

## Routes exposed by Flask

- `GET /api/heartbeat/hr`
- `GET /api/heartbeat/device`
- `GET /api/heartbeat/recording`
- `POST /api/heartbeat/recording/start` with `{"format":"csv"}` or
  `{"format":"jsonl"}`
- `POST /api/heartbeat/recording/stop`

The existing Flask UI can poll `/api/heartbeat/hr` approximately once per
second and `/api/heartbeat/recording` while recording. Start and Stop button
states should be derived from the returned `active` field, not from local UI
assumptions.

## Run both applications

Terminal 1:

```bash
cd /home/shikhar/Downloads/heartbeat/heartbeat
PYTHONPATH=src .venv-linux/bin/python -m heartbeat serve --port 8000
```

Terminal 2: run the Flask app normally. Do not start Heartbeat once per Flask
worker; exactly one Heartbeat service should own the Polar H10 connection.

## Recording file ownership

CSV/JSONL files are written by the Heartbeat service into its configured
`--sessions-dir`. To make them appear inside the Flask project, start
Heartbeat with an absolute shared directory:

```bash
PYTHONPATH=src .venv-linux/bin/python -m heartbeat serve \
  --sessions-dir /absolute/path/to/flask-app/data/heartbeat
```

The Flask process should not independently append to an active recording.
