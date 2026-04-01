import { Tray, Menu, MenuItemConstructorOptions, nativeImage, shell, app } from 'electron';
import * as path from 'path';
import * as os from 'os';
import { PythonManager } from './python-manager';
import { store } from './config';

type TrayStatus = 'running' | 'starting' | 'stopped' | 'error';

export class TrayManager {
  private tray: Tray | null = null;
  private pythonManager: PythonManager;
  private status: TrayStatus = 'stopped';
  private cameraCount = 0;

  constructor(pythonManager: PythonManager) {
    this.pythonManager = pythonManager;
    this.bindEvents();
  }

  create(): void {
    const icon = this.createStatusIcon('stopped');
    this.tray = new Tray(icon);
    this.tray.setToolTip('ForgeOn Streamer - Stopped');
    this.updateMenu();
  }

  destroy(): void {
    if (this.tray) {
      this.tray.destroy();
      this.tray = null;
    }
  }

  private bindEvents(): void {
    this.pythonManager.on('started', () => {
      this.status = 'starting';
      this.updateTray();
    });

    this.pythonManager.on('stopped', () => {
      this.status = 'stopped';
      this.cameraCount = 0;
      this.updateTray();
    });

    this.pythonManager.on('healthy', (data: { cameras: number }) => {
      this.status = 'running';
      this.cameraCount = data.cameras;
      this.updateTray();
    });

    this.pythonManager.on('unhealthy', () => {
      const st = this.pythonManager.getStatus();
      this.status = st.running ? 'starting' : 'stopped';
      this.updateTray();
    });

    this.pythonManager.on('error', () => {
      this.status = 'error';
      this.updateTray();
    });
  }

  private updateTray(): void {
    if (!this.tray) return;
    const icon = this.createStatusIcon(this.status);
    this.tray.setImage(icon);
    this.tray.setToolTip(this.getTooltip());
    this.updateMenu();
  }

  private getTooltip(): string {
    switch (this.status) {
      case 'running':
        return `ForgeOn Streamer - ${this.cameraCount} camera${this.cameraCount !== 1 ? 's' : ''} online`;
      case 'starting':
        return 'ForgeOn Streamer - Starting...';
      case 'error':
        return 'ForgeOn Streamer - Error';
      default:
        return 'ForgeOn Streamer - Stopped';
    }
  }

  private updateMenu(): void {
    if (!this.tray) return;

    const isRunning = this.status === 'running' || this.status === 'starting';
    const autoStart = store.get('autoStart', true);

    const template: MenuItemConstructorOptions[] = [
      {
        label: this.getStatusLabel(),
        enabled: false,
      },
      { type: 'separator' },
      {
        label: isRunning ? 'Stop Server' : 'Start Server',
        click: async () => {
          if (isRunning) {
            await this.pythonManager.stop();
          } else {
            await this.pythonManager.start();
          }
        },
      },
      {
        label: 'Restart Server',
        enabled: isRunning,
        click: async () => {
          await this.pythonManager.restart();
        },
      },
      { type: 'separator' },
      {
        label: 'View Logs',
        click: () => {
          const logPath = path.join(app.getPath('logs'), 'forgeon-streamer.log');
          shell.openPath(logPath);
        },
      },
      { type: 'separator' },
      {
        label: 'Start on Login',
        type: 'checkbox',
        checked: autoStart,
        click: (menuItem) => {
          store.set('autoStart', menuItem.checked);
          app.setLoginItemSettings({
            openAtLogin: menuItem.checked,
            openAsHidden: true,
          });
        },
      },
      {
        label: 'Check for Updates',
        click: () => {
          // Handled by updater module
          const { checkForUpdatesManual } = require('./updater');
          checkForUpdatesManual();
        },
      },
      { type: 'separator' },
      {
        label: 'Quit',
        click: () => {
          app.quit();
        },
      },
    ];

    const menu = Menu.buildFromTemplate(template);
    this.tray.setContextMenu(menu);
  }

  private getStatusLabel(): string {
    switch (this.status) {
      case 'running':
        return `ForgeOn Streamer - ${this.cameraCount} camera${this.cameraCount !== 1 ? 's' : ''} online`;
      case 'starting':
        return 'ForgeOn Streamer - Starting...';
      case 'error':
        return 'ForgeOn Streamer - Error';
      default:
        return 'ForgeOn Streamer - Stopped';
    }
  }

  private createStatusIcon(status: TrayStatus): Electron.NativeImage {
    // Create a 16x16 colored circle as tray icon
    const size = 16;
    const colors: Record<TrayStatus, string> = {
      running: '#22c55e',   // green
      starting: '#eab308',  // yellow
      error: '#ef4444',     // red
      stopped: '#9ca3af',   // gray
    };

    const color = colors[status];

    // Create icon using data URL canvas (SVG approach for cross-platform)
    const svg = `
      <svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
        <circle cx="${size / 2}" cy="${size / 2}" r="${size / 2 - 1}" fill="${color}" />
      </svg>
    `;

    const dataUrl = `data:image/svg+xml;base64,${Buffer.from(svg).toString('base64')}`;
    return nativeImage.createFromDataURL(dataUrl);
  }
}
