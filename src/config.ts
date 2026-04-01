import Store from 'electron-store';

export interface StreamerConfig {
  port: number;
  cameraUrls: string[];
  autoStart: boolean;
}

const store = new Store<StreamerConfig>({
  defaults: {
    port: 5000,
    cameraUrls: [],
    autoStart: true,
  },
});

export { store };

export const STREAMER_PORT: number = store.get('port', 5000);
export const FALLBACK_PORT: number = 5050;
export const HEALTH_URL = (port: number): string =>
  `http://localhost:${port}/api/streamer/health`;
export const PYTHON_SCRIPT = 'app35_cam_sole.py';
export const MAX_RESTART_ATTEMPTS = 3;
export const HEALTH_CHECK_INTERVAL_MS = 5000;
