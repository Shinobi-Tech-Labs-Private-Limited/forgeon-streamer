# Prompt for Codex

Copy the text below into Codex while its working directory is the Flask app.

---

Integrate the UI-free Heartbeat sidecar into this Flask application.

The integration files are in
`/home/shikhar/Downloads/heartbeat/heartbeat/flask_integration_bundle`:
`heartbeat_client.py`, `heartbeat_blueprint.py`, and `README.md`. First inspect
this Flask project's structure, app creation pattern, dependency management,
authentication/authorization, existing API conventions, frontend architecture,
and test setup. Preserve all existing behavior and unrelated changes.

Requirements:

1. Copy or adapt the Heartbeat client and Blueprint into the appropriate Flask
   package. Register it exactly once using this application's existing app
   factory or initialization pattern.
2. Add the HTTP dependency using the project's existing dependency mechanism.
   Reuse an already-installed HTTP client if that is more consistent.
3. Configure `HEARTBEAT_SERVICE_URL`, defaulting to
   `http://127.0.0.1:8000`, using this application's existing configuration and
   environment-variable conventions.
4. Preserve or add equivalent endpoints for latest HR, device status,
   recording status, start recording (`csv` or `jsonl`), and stop recording.
   Apply this Flask app's existing authentication and authorization rules to
   mutating recording endpoints.
5. Extend the existing Flask UI—do not copy the Heartbeat HUD. Show live BPM,
   connection state, format selection, separate Start and Stop buttons,
   filename, and sample count. Disable Start and format selection while
   recording; disable Stop while idle. Derive states from the server's
   `active` value and poll status without browser caching.
6. Handle the Heartbeat service being offline with a clear, non-fatal UI state
   and HTTP 502 response from the Flask proxy.
7. Do not embed Bluetooth scanning or an asyncio loop in Flask, and do not
   launch one Heartbeat instance per Flask worker. Document how to run exactly
   one sidecar process alongside Flask and how to configure a shared absolute
   sessions directory.
8. Add tests that match this project's conventions for client success/failure,
   invalid formats, Blueprint routes, and Start/Stop UI state where practical.
9. Run the relevant tests and static checks. Report changed files, commands to
   run both services, URLs, and any remaining manual setup.

Do not make assumptions about the Flask app layout before inspecting it. Ask
only if a decision cannot be safely inferred from the repository.

---
