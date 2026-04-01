import { app, Notification } from 'electron';

// electron-updater is optional — only works in packaged builds
let autoUpdater: any = null;

const UPDATE_CHECK_INTERVAL_MS = 4 * 60 * 60 * 1000; // 4 hours
let updateCheckTimer: ReturnType<typeof setInterval> | null = null;

function getAutoUpdater(): any {
  if (autoUpdater) return autoUpdater;
  try {
    // electron-updater must be installed separately for production builds
    const { autoUpdater: updater } = require('electron-updater');
    autoUpdater = updater;
    return autoUpdater;
  } catch {
    return null;
  }
}

export function initUpdater(): void {
  const updater = getAutoUpdater();
  if (!updater) {
    console.log('electron-updater not available (dev mode)');
    return;
  }

  updater.autoDownload = true;
  updater.autoInstallOnAppQuit = true;

  updater.on('update-available', (info: any) => {
    console.log(`Update available: ${info.version}`);
    showNotification(
      'Update Available',
      `ForgeOn Streamer v${info.version} is downloading...`
    );
  });

  updater.on('update-downloaded', (info: any) => {
    console.log(`Update downloaded: ${info.version}`);
    showNotification(
      'Update Ready',
      `ForgeOn Streamer v${info.version} will be installed on next restart.`
    );
  });

  updater.on('error', (err: Error) => {
    console.error('Auto-updater error:', err.message);
  });

  // Check on startup (after a short delay)
  setTimeout(() => {
    checkForUpdates();
  }, 10000);

  // Check periodically
  updateCheckTimer = setInterval(() => {
    checkForUpdates();
  }, UPDATE_CHECK_INTERVAL_MS);
}

export function checkForUpdates(): void {
  const updater = getAutoUpdater();
  if (!updater) return;

  updater.checkForUpdates().catch((err: Error) => {
    console.error('Update check failed:', err.message);
  });
}

export function checkForUpdatesManual(): void {
  const updater = getAutoUpdater();
  if (!updater) {
    showNotification('Updates', 'Auto-updater not available in development mode.');
    return;
  }

  updater.checkForUpdates().then((result: any) => {
    if (!result || !result.updateInfo) {
      showNotification('Up to Date', 'You are running the latest version.');
    }
  }).catch((err: Error) => {
    showNotification('Update Error', `Failed to check for updates: ${err.message}`);
  });
}

export function stopUpdater(): void {
  if (updateCheckTimer) {
    clearInterval(updateCheckTimer);
    updateCheckTimer = null;
  }
}

function showNotification(title: string, body: string): void {
  if (Notification.isSupported()) {
    new Notification({ title, body }).show();
  }
}
