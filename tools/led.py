"""
LED Camera Sync Bridge Server (Python edition)

Bridges a web browser to the JDY-31B Bluetooth Classic SPP module that
controls the camera-sync LED rig.

Browser  <--WebSocket-->  this script  <--COM port (Bluetooth SPP)-->  JDY-31

The web app (index.html) is identical to the Node version; only this
backend script changes.

Requirements:
    pip install websockets pyserial

Usage:
    python server.py             (auto-picks COM14, then COM15 if 14 fails)
    python server.py COM14       (explicit COM port)

Then open  http://localhost:3000  in any browser.
"""

import asyncio
import json
import sys
import time
import threading
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("[FATAL] 'pyserial' is not installed.")
    print("        Run:  pip install pyserial")
    sys.exit(1)

try:
    import websockets
except ImportError:
    print("[FATAL] 'websockets' is not installed.")
    print("        Run:  pip install websockets")
    sys.exit(1)


# =============================================================
# Configuration
# =============================================================
HTTP_PORT = 3000          # web app served here
WS_PORT   = 3001          # WebSocket lives on a separate port
BAUD      = 9600          # JDY-31 default; the Arduino firmware also uses 9600

# COM ports the script will try (in order) when nothing is passed on CLI.
# Edit this list if your Bluetooth COM ports are different.
DEFAULT_COMS = ["COM14", "COM15"]


# =============================================================
# State
# =============================================================
serial_conn = None           # active pyserial.Serial object, or None
serial_lock = threading.Lock()
current_com = None
ws_clients  = set()
shutdown    = False


def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(level, message):
    line = f"[{ts()}] [{level.upper()}] {message}"
    print(line, flush=True)
    asyncio.run_coroutine_threadsafe(
        broadcast({"type": "log", "level": level, "message": message}),
        main_loop,
    )


async def broadcast(obj):
    """Send a JSON message to every connected browser."""
    if not ws_clients:
        return
    msg = json.dumps(obj)
    dead = []
    for client in ws_clients:
        try:
            await client.send(msg)
        except Exception:
            dead.append(client)
    for d in dead:
        ws_clients.discard(d)


async def send_status():
    await broadcast({
        "type": "status",
        "connected": serial_conn is not None and serial_conn.is_open,
        "address": current_com,
    })


# =============================================================
# Serial port (Bluetooth SPP) handling
# =============================================================
def list_bluetooth_ports():
    """Return a list of dicts describing all available COM ports."""
    devices = []
    for port in list_ports.comports():
        name = port.description or "(no name)"
        devices.append({
            "name": name,
            "address": port.device,   # "COM14" etc.
        })
    return devices


def serial_reader_thread():
    """Read bytes from the JDY-31 and emit each newline-terminated
    line as a websocket 'rx' message to the browser."""
    global serial_conn
    buf = bytearray()
    while not shutdown:
        sc = serial_conn
        if sc is None or not sc.is_open:
            time.sleep(0.05)
            continue
        try:
            data = sc.read(64)  # blocks up to the configured timeout
        except (OSError, serial.SerialException) as e:
            log("error", f"Serial read error: {e}")
            close_serial()
            continue
        if not data:
            continue
        buf.extend(data)
        # Split out complete lines on \r or \n
        while True:
            idx = -1
            for i, b in enumerate(buf):
                if b in (0x0A, 0x0D):  # \n or \r
                    idx = i
                    break
            if idx < 0:
                break
            line = bytes(buf[:idx])
            del buf[:idx + 1]
            if not line:
                continue
            try:
                text = line.decode("utf-8", errors="replace").strip()
            except Exception:
                text = repr(line)
            if text:
                asyncio.run_coroutine_threadsafe(
                    broadcast({"type": "rx", "data": text}),
                    main_loop,
                )


def open_serial(port_name: str) -> bool:
    """Try to open the given COM port. Returns True on success."""
    global serial_conn, current_com
    with serial_lock:
        if serial_conn is not None and serial_conn.is_open:
            close_serial_locked()
        try:
            sc = serial.Serial(
                port=port_name,
                baudrate=BAUD,
                timeout=0.2,         # blocking read with timeout
                write_timeout=2.0,
            )
            time.sleep(0.3)
            serial_conn = sc
            current_com = port_name
            log("info", f"Opened {port_name} at {BAUD} baud")
            return True
        except (OSError, serial.SerialException) as e:
            log("error", f"Could not open {port_name}: {e}")
            return False


def close_serial_locked():
    """Caller must hold serial_lock."""
    global serial_conn, current_com
    if serial_conn is not None:
        try:
            serial_conn.close()
        except Exception:
            pass
    serial_conn = None
    current_com = None


def close_serial():
    with serial_lock:
        was_open = serial_conn is not None
        close_serial_locked()
    if was_open:
        log("info", "Serial port closed")
        asyncio.run_coroutine_threadsafe(send_status(), main_loop)


def write_serial(text: str):
    sc = serial_conn
    if sc is None or not sc.is_open:
        log("error", "Cannot send: no serial port open")
        return
    try:
        payload = (text + "\r\n").encode("utf-8")
        sc.write(payload)
        sc.flush()
        log("info", f"TX: {text}")
    except (OSError, serial.SerialException) as e:
        log("error", f"Write error: {e}")
        close_serial()


# =============================================================
# WebSocket handler
# =============================================================
async def ws_handler(websocket):
    ws_clients.add(websocket)
    log("info", "Browser connected to WebSocket")
    await send_status()
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except Exception:
                log("error", "Bad JSON from browser")
                continue

            mtype = msg.get("type")

            if mtype == "scan":
                devices = list_bluetooth_ports()
                await websocket.send(json.dumps({
                    "type": "devices",
                    "devices": devices,
                }))
                log("info", f"Listed {len(devices)} COM port(s)")

            elif mtype == "connect":
                address = msg.get("address", "")
                log("info", f"Connect request: {address}")
                if open_serial(address):
                    await send_status()
                else:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": f"Could not open {address}. "
                                   f"Make sure the JDY-31 is powered on and not "
                                   f"in use by another app. If this is the wrong "
                                   f"port, try the other Bluetooth COM port.",
                    }))

            elif mtype == "disconnect":
                close_serial()

            elif mtype == "send":
                data = msg.get("data", "")
                if isinstance(data, str):
                    write_serial(data)

            else:
                log("error", f"Unknown msg type: {mtype}")

    except websockets.ConnectionClosed:
        pass
    except Exception as e:
        log("error", f"WS handler error: {e}")
    finally:
        ws_clients.discard(websocket)
        log("info", "Browser disconnected from WebSocket")


# =============================================================
# HTTP server (serves the static web app)
# =============================================================
class WebAppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        public_dir = os.path.join(os.path.dirname(__file__), "public")
        super().__init__(*args, directory=public_dir, **kwargs)

    def log_message(self, fmt, *args):
        # Silence the default per-request stdout spam
        return


def run_http():
    server = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), WebAppHandler)
    print(f"  HTTP   http://localhost:{HTTP_PORT}    (open this in a browser)")
    server.serve_forever()


# =============================================================
# Main
# =============================================================
async def main():
    global main_loop
    main_loop = asyncio.get_running_loop()

    # ---- Start HTTP server in a background thread
    http_thread = threading.Thread(target=run_http, daemon=True)
    http_thread.start()

    # ---- Start serial reader thread
    reader = threading.Thread(target=serial_reader_thread, daemon=True)
    reader.start()

    # ---- Optional: auto-open a COM port given on CLI, or try defaults
    if len(sys.argv) > 1:
        wanted = sys.argv[1]
        print(f"\nAuto-connecting to {wanted}...")
        if not open_serial(wanted):
            print(f"  Failed to auto-open {wanted}. "
                  "You can still pick a port in the browser.")
    else:
        print("\nAvailable COM ports right now:")
        for p in list_bluetooth_ports():
            print(f"  - {p['address']}: {p['name']}")
        print()

    # ---- Start WebSocket server
    print(f"  WS     ws://localhost:{WS_PORT}/ws")
    print("\nPress Ctrl+C to stop.\n")

    async with websockets.serve(ws_handler, "127.0.0.1", WS_PORT):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    print("=" * 50)
    print("  LED Camera Sync Bridge Server (Python)")
    print("=" * 50)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[shutdown] Closing...")
        shutdown = True
        close_serial()
