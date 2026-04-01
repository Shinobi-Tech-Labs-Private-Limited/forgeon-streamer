# ForgeOn Streamer

Tray-only Electron companion app that manages the ForgeOn camera streaming server (`app35_cam_sole.py`). It runs as a system tray icon with no main window, providing start/stop controls, health monitoring, and auto-restart for the Python Flask backend.

## Install

Download the latest release for your platform from the [GitHub Releases](https://github.com/shinobitecthlabs/forgeon-streamer/releases) page.

- **macOS**: `.dmg`
- **Windows**: `.exe` (NSIS installer)
- **Linux**: `.AppImage`

## Develop

```bash
# Install dependencies
npm install

# Symlink the Python streamer script
ln -s /path/to/forgeon/backend/app/app35_cam_sole.py python/app35_cam_sole.py

# Install Python dependencies
pip install -r python/requirements.txt

# Run in development
npm run dev
```

## Build

```bash
# Compile TypeScript and package for current platform
npm run package
```

Output will be in the `release/` directory.

## Architecture

```
forgeon-streamer/
  src/
    main.ts            # Electron entry point (no window, tray-only)
    config.ts          # Configuration constants + electron-store
    python-manager.ts  # Spawns/monitors the Python Flask process
    tray.ts            # System tray icon and context menu
    updater.ts         # Auto-update via electron-updater + GitHub Releases
  python/
    requirements.txt   # Python dependencies
    app35_cam_sole.py  # Symlinked from forgeon backend (not committed)
  assets/              # Tray icons and app icons
  electron-builder.yml # Packaging config for macOS/Windows/Linux
```

**Flow**: Electron starts -> hides dock (macOS) -> spawns Python process -> polls `/api/streamer/health` every 5s -> updates tray icon (green/yellow/red/gray) -> auto-restarts on crash (up to 3 attempts with exponential backoff).
