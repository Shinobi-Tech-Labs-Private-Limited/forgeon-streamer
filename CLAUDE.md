# Repository guidance

- Production entry point: `app35_cam_sole.py`; release number: `VERSION`.
- Production UI: `templates/active/index35_cam_sole.html`.
- Runtime helpers remain at repository root; focus and cube code live in
  `codesharpnessmeasure/` and `calibration/`.
- `legacy/` is recovery history, not a launch target. `tools/` contains manual
  utilities. `flask_integration_bundle/` is reference material, not active code.
- Never commit sessions, recordings, calibration captures, credentials, tokens,
  real `.env` files, virtual environments, caches, or generated Flutter output.
- Preserve as-built cube values in `calibration/config.yaml`; compare them with
  the upstream Forgeon calibration package before changing either copy.
- Work on feature branches. Never push directly to `main`.
- Run `python -m compileall`, focus tests, `git diff --check`, and secret/runtime
  file checks before proposing a PR.
- Cut rig releases as `rig-vNN` only after merge and hardware acceptance.
