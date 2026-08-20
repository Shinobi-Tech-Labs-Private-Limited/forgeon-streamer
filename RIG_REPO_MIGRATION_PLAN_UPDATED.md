# Updated plan — bring the Forgeon rig app under version control

Updated on 2026-08-19 after the rig source was inventoried, backed up, cleaned,
and reorganized.

## Goal and fixed decisions

- Use the existing private organization repository:
  `Shinobi-Tech-Labs-Private-Limited/forgeon-streamer`.
- Keep this rig application as a small, separate repository, not part of the
  large Forgeon monorepo.
- Preserve the old Electron application on branch `archive/electron-tray` and
  tag `electron-v0.1-wip`.
- Put all migration work on `feat/rig-app-v13`.
- Never push directly to `main`.
- The operator reviews and creates/approves the PR.
- Do not commit credentials, recordings, session data, calibration captures,
  generated files, virtual environments, or local agent state.

## Safety and approval gates

1. Steps 1–4 are strictly read-only. Do not create, delete, move, rename, or
   overwrite anything during these steps.
2. Step 5 may create only new `~/rig-diff-*.patch` files. It must not alter any
   existing source or backup file.
3. Stop and report after Step 1.
4. Stop and report after Step 5.
5. Stop again in Step 8 immediately before creating the PR.
6. Do not run later steps without the operator's approval at each gate.
7. If a named source is absent, report it. Do not invent a replacement or
   silently compare a different file.

## Known current state

- Rig source:
  `/home/shikhar/Downloads/Cam_Stream/Cam_Stream/camera_app`
- Active entry point: `app35_cam_sole_V13.py`
- Active template: `templates/active/index35_cam_sole.html`
- Python environment: `env/`; `requirements.lock.txt` already exists.
- Old application versions and experiments are documented under `legacy/`.
- Standalone utilities are documented under `tools/`.
- The folder is not currently a Git repository. The accidental approximately
  107 GB `.git` database has already been removed.
- GitHub authentication for `nalin-forgelabs` was verified on 2026-08-19.
- Internal disk had approximately 116 GB free; DATA had approximately 459 GB
  free at the last check.
- Verified backup location:
  `/media/shikhar/DATA/home/shikhar/Downloads/Cam_Stream/Cam_Stream/camera_app/rig-backups/verified-2026-08-19`
- The existing source tarball is a valid pre-reorganization snapshot.
- Calibration captures, heartbeat sessions, dependency lock, and secure copies
  of the former credential files exist in that backup.
- The backup file named `sessions.zip` is incomplete and must not be treated as
  a valid sessions backup.
- The required recording is stored directly on DATA at:
  `/media/shikhar/DATA/home/shikhar/Downloads/Cam_Stream/Cam_Stream/camera_app/sessions/session_2026-08-19_12-42-47`

## Step 1 — current inventory checkpoint (strictly read-only)

Set:

```bash
S=/home/shikhar/Downloads/Cam_Stream/Cam_Stream/camera_app
B=/media/shikhar/DATA/home/shikhar/Downloads/Cam_Stream/Cam_Stream/camera_app/rig-backups/verified-2026-08-19
```

Run read-only checks and capture their output in the agent report, not in new
files:

```bash
gh auth status
git config --global user.name
git config --global user.email
git ls-remote https://github.com/Shinobi-Tech-Labs-Private-Limited/forgeon-streamer.git | head -3
git --version
python3 --version
ffmpeg -version | head -1
df -h

cd "$S"
ls -la
find . -maxdepth 3 \
  -not -path './sessions/*' \
  -not -path './env/*' \
  -not -path './.git/*' \
  -not -path '*/__pycache__/*' \
  -not -name '*.pyc' | sort
sha256sum app35_cam_sole_V13.py *.py templates/active/*.html
env/bin/python --version
env/bin/python -m pip freeze
du -sh sessions heartbeat_sessions 2>/dev/null
find sessions -maxdepth 1 -type d -name 'session_*' -printf '%TY-%Tm-%Td %f\n' | sort
```

Also inspect launch evidence without changing it:

```bash
systemctl list-units --all | grep -i -E 'cam|rig|app35'
crontab -l
ls -la ~/.config/autostart
ps -ef | grep '[a]pp35'
```

Verify existing backup artifacts without copying them again:

```bash
find "$B" -maxdepth 2 -type f -printf '%P\t%s bytes\n' | sort
tar tzf "$B/forgeon-rig-source-backup-2026-08-19.tar.gz" >/dev/null
sha256sum "$B/forgeon-rig-source-backup-2026-08-19.tar.gz"
find "$B/calibration_cam1" -type f | wc -l
test -d "$B/heartbeat_sessions"
test -d /media/shikhar/DATA/home/shikhar/Downloads/Cam_Stream/Cam_Stream/camera_app/sessions/session_2026-08-19_12-42-47
```

Report current files, hashes, launch evidence, backup status, and any missing
items. **STOP for operator review.**

## Step 2 — identify comparison baselines (strictly read-only)

- Inspect the existing source backup tarball and `legacy/` to locate the exact
  V12 application and prior template used as comparison baselines.
- Search for the original August 17 update ZIP or extracted update folder.
- Record all candidate paths, sizes, mtimes, and SHA-256 values in the report.
- Do not extract archives yet. Listing archive contents is allowed.
- If multiple candidates exist, ask the operator which one is authoritative.

## Step 3 — determine local dependency set (strictly read-only)

- Parse imports from `app35_cam_sole_V13.py` and its recursively imported local
  modules.
- Confirm which root modules, packages, templates, and static assets are
  runtime dependencies.
- Classify remaining code as legacy application, standalone tool, generated
  output, runtime data, or uncertain.
- Do not move or prune anything.

## Step 4 — define the repository manifest (strictly read-only)

Prepare a proposed take/do-not-take manifest for review in the Step 5 report.

Take:

- Active application, required helper modules, active templates/static files
- `codesharpnessmeasure/`
- Calibration source package and `calibration/config.yaml`
- `requirements.lock.txt` and a curated `requirements.txt`
- Relevant operational documentation
- Selected standalone tools, with their purpose documented
- Legacy source only if intentionally retained and documented

Do not take:

- `sessions/`, `heartbeat_sessions/`, `calibration_cam*/`
- `*.mp4`, `*.npz`, `*.jsonl`, logs, snapshots, and generated output
- `env/`, `.venv/`, caches, Flutter build output, IDE state
- Credentials, tokens, `rclone.conf`, or real `.env` files
- Backup archives and local Codex/agent state

## Step 5 — save hand-edit diffs (only new patch files may be created)

Before running, verify each proposed `~/rig-diff-*.patch` destination is absent.
Never overwrite an existing patch.

Use the exact August 17 baseline identified in Step 2. Save:

```bash
diff -u "$Z/app35_cam_sole_V12.py" \
  "$S/app35_cam_sole_V13.py" > ~/rig-diff-V12-vs-running.patch || test $? -eq 1

diff -u "$Z/templates/index35_cam_sole_V8.html" \
  "$S/templates/active/index35_cam_sole.html" > ~/rig-diff-template.patch || test $? -eq 1

diff -ru -x __pycache__ "$Z/codesharpnessmeasure" \
  "$S/codesharpnessmeasure" > ~/rig-diff-focus.patch || test $? -eq 1

diff -u "$Z/calibration/config.yaml" \
  "$S/calibration/config.yaml" > ~/rig-diff-cubeconfig.patch || test $? -eq 1
```

If the authoritative baseline exists only inside the backup tarball, extract
only the required baseline files into a new temporary directory created with
`mktemp -d`; do not extract over the working tree.

Validate that every patch is readable and report:

- Patch path, size, SHA-256
- Functions/routes/config values that changed
- Expected V13 changes versus undocumented hand edits
- Proposed take/do-not-take repository manifest
- Whether cube configuration also requires a separate Forgeon monorepo PR

**STOP for operator review. Nothing existing may have been altered yet.**

## Step 6 — preserve Electron history and create the feature branch

Only after Step 5 approval:

```bash
cd ~
git clone https://github.com/Shinobi-Tech-Labs-Private-Limited/forgeon-streamer.git
cd ~/forgeon-streamer

git tag electron-v0.1-wip origin/main
git branch archive/electron-tray origin/main
git push origin electron-v0.1-wip archive/electron-tray
git switch -c feat/rig-app-v13 origin/main
```

Before creating or pushing the tag/branch, confirm they do not already exist.
Never check out, commit to, or push directly to `main`.

## Step 7 — construct the rig repository on `feat/rig-app-v13`

- Remove the Electron working-tree files only from the feature branch. Their
  history remains on `archive/electron-tray` and `electron-v0.1-wip`.
- Copy files according to the approved Step 5 manifest.
- Rename the checked-out entry point to `app35_cam_sole.py` and update only the
  references required by the new checkout layout.
- Do not modify the original rig folder.
- Add `VERSION` containing `13`.
- Add curated `requirements.txt`; retain `requirements.lock.txt`.
- Add `calibration/SOURCE.md` with the verified upstream commit if known.
- Add sanitized example configuration containing placeholders only.
- Use the cleaned rig `.gitignore` policy.
- Write `README.md`, `README-INSTALL.md`, `CHANGELOG.md`, and `CLAUDE.md`.
- Record all hand edits discovered in Step 5.
- Run syntax/import tests and non-hardware tests. Report hardware-dependent
  tests that cannot safely run.

Review before commit:

```bash
git status --short
git diff --check
git diff --stat
git diff
```

Commit and push only the feature branch:

```bash
git add -A
git commit -m "feat: rig app V13 and supporting modules"
git push -u origin feat/rig-app-v13
```

## Step 8 — PR checkpoint

Prepare, but do not execute, the proposed PR command and body. Report:

- Branch and commit SHA
- Complete file list and repository size
- Hand edits preserved
- Files deliberately omitted
- Test results and known hardware/runtime gaps
- Calibration provenance status
- Confirmation that no secrets or runtime data are tracked
- Confirmation that `main` was never pushed directly

**STOP immediately before `gh pr create` for operator review.** The operator
creates or explicitly authorizes creation of the PR.

## Step 9 — switch the rig only after merge and approval

- Create `~/forgeon-streamer/.venv` and install from `requirements.txt`.
- Copy or recreate only required real machine-local configuration; keep it
  ignored.
- Decide explicitly whether sessions remain at the current path or use a
  configured external session root.
- Update the launcher only after recording its current state and receiving
  approval.
- Smoke-test focus, snapshots, recording, download, heartbeat, and upload.
- Keep the original source folder unchanged until one complete real session has
  succeeded from the checkout.

## Step 10 — recording retention (separate destructive approval)

- Do not rely on the incomplete `sessions.zip`.
- Build a manifest and SHA-256 set for the direct DATA session archive.
- Verify every retained file before proposing local deletion.
- Present exact deletion candidates, sizes, and retention cutoff.
- Delete nothing without a new explicit operator approval.
- Implement automatic retention/health behavior later on a separate feature
  branch and PR.

## Completion criteria

- `feat/rig-app-v13` contains the reviewed rig source and no secrets/runtime
  data.
- Electron history is preserved on the archive branch and tag.
- All `~/rig-diff-*.patch` files exist and their hashes are reported.
- Hand edits are documented in `CHANGELOG.md` and the PR body.
- The feature branch is pushed; `main` is untouched.
- The operator reviews before PR creation and before any launcher/data cleanup.
