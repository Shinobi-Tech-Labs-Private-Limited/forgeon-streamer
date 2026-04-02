import { app } from 'electron';
import { TrayManager } from './tray';
import { PythonManager } from './python-manager';
import { initUpdater, stopUpdater } from './updater';
import { store } from './config';
import * as fs from 'fs';
import * as path from 'path';

// Ensure single instance
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
}

let trayManager: TrayManager | null = null;
let pythonManager: PythonManager | null = null;

// Set up logging to file
function setupLogging(): void {
  const logDir = app.getPath('logs');
  if (!fs.existsSync(logDir)) {
    fs.mkdirSync(logDir, { recursive: true });
  }
  const logPath = path.join(logDir, 'forgeon-streamer.log');
  const logStream = fs.createWriteStream(logPath, { flags: 'a' });

  const originalLog = console.log;
  const originalError = console.error;

  console.log = (...args: any[]) => {
    const msg = `[${new Date().toISOString()}] ${args.join(' ')}\n`;
    logStream.write(msg);
    originalLog.apply(console, args);
  };

  console.error = (...args: any[]) => {
    const msg = `[${new Date().toISOString()}] ERROR: ${args.join(' ')}\n`;
    logStream.write(msg);
    originalError.apply(console, args);
  };
}

app.on('ready', async () => {
  setupLogging();
  console.log('ForgeOn Streamer starting...');

  // Hide dock icon on macOS (tray-only app)
  if (process.platform === 'darwin') {
    app.dock.hide();
  }

  // Initialize python manager
  pythonManager = new PythonManager();

  // Forward python manager logs to console
  pythonManager.on('log', (msg: string) => console.log(msg));
  pythonManager.on('error', (msg: string) => console.error(msg));

  // Initialize tray
  trayManager = new TrayManager(pythonManager);
  trayManager.create();

  // Initialize auto-updater
  initUpdater();

  // Auto-start if configured
  const autoStart = store.get('autoStart', true);
  if (autoStart) {
    await pythonManager.start();
  }

  console.log('ForgeOn Streamer ready');
});

app.on('before-quit', async () => {
  console.log('ForgeOn Streamer shutting down...');
  stopUpdater();
  if (pythonManager) {
    await pythonManager.stop();
  }
  if (trayManager) {
    trayManager.destroy();
  }
});

// Prevent app from quitting when no windows are open (tray-only app)
app.on('window-all-closed', () => {
  // Do nothing — keep running as tray app
});
