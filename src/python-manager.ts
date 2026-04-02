import { ChildProcess, spawn } from 'child_process';
import { EventEmitter } from 'events';
import * as path from 'path';
import * as fs from 'fs';
import * as net from 'net';
import * as http from 'http';
import { app } from 'electron';
import {
  PYTHON_SCRIPT,
  STREAMER_PORT,
  FALLBACK_PORT,
  HEALTH_URL,
  MAX_RESTART_ATTEMPTS,
  HEALTH_CHECK_INTERVAL_MS,
  store,
} from './config';

export interface PythonStatus {
  running: boolean;
  healthy: boolean;
  cameras: number;
  restartCount: number;
  port: number;
}

export class PythonManager extends EventEmitter {
  private process: ChildProcess | null = null;
  private healthInterval: ReturnType<typeof setInterval> | null = null;
  private restartCount = 0;
  private healthy = false;
  private cameras = 0;
  private activePort: number = STREAMER_PORT;
  private stopping = false;

  async start(): Promise<void> {
    if (this.process) {
      this.emit('log', 'Python process already running');
      return;
    }

    this.stopping = false;

    // Find an available port
    const portFree = await this.checkPortAvailable(STREAMER_PORT);
    this.activePort = portFree ? STREAMER_PORT : FALLBACK_PORT;

    if (!portFree) {
      const fallbackFree = await this.checkPortAvailable(FALLBACK_PORT);
      if (!fallbackFree) {
        this.emit('error', `Both ports ${STREAMER_PORT} and ${FALLBACK_PORT} are in use`);
        return;
      }
      this.emit('log', `Port ${STREAMER_PORT} in use, falling back to ${FALLBACK_PORT}`);
    }

    const pythonPath = this.findPython();
    if (!pythonPath) {
      this.emit('error', 'Python interpreter not found');
      return;
    }

    const scriptPath = this.findScript();
    if (!scriptPath) {
      this.emit('error', `Script ${PYTHON_SCRIPT} not found`);
      return;
    }

    const env: Record<string, string> = {
      ...process.env as Record<string, string>,
      PORT: String(this.activePort),
    };

    // Add bundled site-packages to PYTHONPATH if running from packaged app
    const bundledSitePackages = path.join(this.getResourcesPath(), 'python', 'site-packages');
    if (fs.existsSync(bundledSitePackages)) {
      const existingPythonPath = env.PYTHONPATH || '';
      env.PYTHONPATH = existingPythonPath
        ? `${bundledSitePackages}:${existingPythonPath}`
        : bundledSitePackages;
    }

    this.emit('log', `Starting Python: ${pythonPath} ${scriptPath} on port ${this.activePort}`);

    this.process = spawn(pythonPath, [scriptPath], {
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    this.process.stdout?.on('data', (data: Buffer) => {
      this.emit('log', `[stdout] ${data.toString().trim()}`);
    });

    this.process.stderr?.on('data', (data: Buffer) => {
      this.emit('log', `[stderr] ${data.toString().trim()}`);
    });

    this.process.on('exit', (code, signal) => {
      this.emit('log', `Python process exited: code=${code}, signal=${signal}`);
      this.process = null;
      this.healthy = false;
      this.cameras = 0;
      this.stopHealthCheck();
      this.emit('stopped');

      if (!this.stopping && this.restartCount < MAX_RESTART_ATTEMPTS) {
        this.restartCount++;
        const delay = Math.min(1000 * Math.pow(2, this.restartCount - 1), 30000);
        this.emit('log', `Auto-restart attempt ${this.restartCount}/${MAX_RESTART_ATTEMPTS} in ${delay}ms`);
        setTimeout(() => this.start(), delay);
      } else if (this.restartCount >= MAX_RESTART_ATTEMPTS) {
        this.emit('error', `Max restart attempts (${MAX_RESTART_ATTEMPTS}) reached`);
      }
    });

    this.process.on('error', (err) => {
      this.emit('error', `Failed to start Python: ${err.message}`);
      this.process = null;
    });

    this.emit('started');
    this.startHealthCheck();
  }

  async stop(): Promise<void> {
    this.stopping = true;
    this.stopHealthCheck();

    if (!this.process) {
      this.emit('log', 'No Python process to stop');
      return;
    }

    this.emit('log', 'Stopping Python process (SIGTERM)...');
    this.process.kill('SIGTERM');

    await new Promise<void>((resolve) => {
      const timeout = setTimeout(() => {
        if (this.process) {
          this.emit('log', 'SIGTERM timeout, sending SIGKILL...');
          this.process.kill('SIGKILL');
        }
        resolve();
      }, 5000);

      if (this.process) {
        this.process.once('exit', () => {
          clearTimeout(timeout);
          resolve();
        });
      } else {
        clearTimeout(timeout);
        resolve();
      }
    });

    this.process = null;
    this.healthy = false;
    this.cameras = 0;
    this.restartCount = 0;
    this.emit('stopped');
  }

  async restart(): Promise<void> {
    this.restartCount = 0;
    await this.stop();
    await this.start();
  }

  getStatus(): PythonStatus {
    return {
      running: this.process !== null,
      healthy: this.healthy,
      cameras: this.cameras,
      restartCount: this.restartCount,
      port: this.activePort,
    };
  }

  findPython(): string | null {
    const resourcesPath = this.getResourcesPath();

    // 1. Check bundled Python in resources (platform-specific paths)
    const bundledCandidates = process.platform === 'win32'
      ? [
          path.join(resourcesPath, 'python', 'runtime', 'python.exe'),
          path.join(resourcesPath, 'python', 'python.exe'),
        ]
      : [
          path.join(resourcesPath, 'python', 'runtime', 'bin', 'python3'),
          path.join(resourcesPath, 'python', 'bin', 'python3'),
        ];

    for (const candidate of bundledCandidates) {
      if (fs.existsSync(candidate)) {
        this.emit('log', `Using bundled Python: ${candidate}`);
        return candidate;
      }
    }

    // 2. Fall back to system python3 then python
    const systemCandidates = process.platform === 'win32'
      ? ['python', 'python3']
      : ['python3', 'python'];

    for (const cmd of systemCandidates) {
      try {
        const { execSync } = require('child_process');
        const whichCmd = process.platform === 'win32' ? 'where' : 'which';
        execSync(`${whichCmd} ${cmd}`, { stdio: 'ignore' });
        this.emit('log', `Using system Python: ${cmd}`);
        return cmd;
      } catch {
        // not found, try next
      }
    }

    return null;
  }

  private findScript(): string | null {
    // Check bundled location first
    const bundledScript = path.join(this.getResourcesPath(), 'python', PYTHON_SCRIPT);
    if (fs.existsSync(bundledScript)) {
      return bundledScript;
    }

    // Check project python/ directory (dev mode)
    const devScript = path.join(__dirname, '..', 'python', PYTHON_SCRIPT);
    if (fs.existsSync(devScript)) {
      return devScript;
    }

    return null;
  }

  private getResourcesPath(): string {
    if (app.isPackaged) {
      return process.resourcesPath;
    }
    return path.join(__dirname, '..');
  }

  async checkPortAvailable(port: number): Promise<boolean> {
    return new Promise((resolve) => {
      const server = net.createServer();
      server.once('error', () => resolve(false));
      server.once('listening', () => {
        server.close(() => resolve(true));
      });
      server.listen(port, '127.0.0.1');
    });
  }

  async healthCheck(): Promise<boolean> {
    return new Promise((resolve) => {
      const url = HEALTH_URL(this.activePort);
      const req = http.get(url, { timeout: 3000 }, (res) => {
        let data = '';
        res.on('data', (chunk: Buffer) => {
          data += chunk.toString();
        });
        res.on('end', () => {
          try {
            const json = JSON.parse(data);
            this.cameras = json.cameras_online ?? 0;
            this.healthy = true;
            this.emit('healthy', { cameras: this.cameras });
            resolve(true);
          } catch {
            this.healthy = false;
            this.emit('unhealthy', 'Invalid health response');
            resolve(false);
          }
        });
      });

      req.on('error', () => {
        this.healthy = false;
        this.emit('unhealthy', 'Connection failed');
        resolve(false);
      });

      req.on('timeout', () => {
        req.destroy();
        this.healthy = false;
        this.emit('unhealthy', 'Health check timeout');
        resolve(false);
      });
    });
  }

  private startHealthCheck(): void {
    this.stopHealthCheck();
    this.healthInterval = setInterval(() => {
      this.healthCheck();
    }, HEALTH_CHECK_INTERVAL_MS);
    // Run first check after a brief delay to let server start
    setTimeout(() => this.healthCheck(), 2000);
  }

  private stopHealthCheck(): void {
    if (this.healthInterval) {
      clearInterval(this.healthInterval);
      this.healthInterval = null;
    }
  }
}
