import asyncio
import tkinter as tk
from tkinter import ttk, messagebox
import threading
from datetime import datetime
from collections import deque
import struct
import time
from bleak import BleakScanner, BleakClient
from bleak.exc import BleakError
import math
from threading import Lock
import platform
import json

# ==========================================
# CONSTANTS & CONFIGURATION
# ==========================================
UUID_ADC_SERVICE = "2a90f079-8412-4953-951c-cb3e2d27c8d4"
UUID_ADC_CHAR = "aa0a4d54-2b51-42f9-bbca-3b9304fbed92"

UUID_CMD_SERVICE = "e4b7f8d1-3c19-4f7a-9c8a-f2d79371b44e"
UUID_CMD_CHAR = "7d4a93e2-1b7e-41c5-a2ed-8f0cf19e68e3"

UUID_STATUS_SERVICE = "a3f1c8b2-7d44-4e9f-b2a1-c8f37d9a12ef"
UUID_STATUS_CHAR = "7d4a93e2-1b22-4a61-95b4-564f0a2c7703"

# Device Information Service (DIS)
UUID_DEVICE_INFO_SERVICE = "0000180a-0000-1000-8000-00805f9b34fb"  # 0x180A
UUID_MODEL_NUMBER = "00002a24-0000-1000-8000-00805f9b34fb"       # 0x2A24
UUID_MANUFACTURER_NAME = "00002a29-0000-1000-8000-00805f9b34fb"  # 0x2A29
UUID_FIRMWARE_REVISION = "00002a26-0000-1000-8000-00805f9b34fb"  # 0x2A26
UUID_HARDWARE_REVISION = "00002a27-0000-1000-8000-00805f9b34fb"  # 0x2A27

CMD_LED_TOGGLE = b'\x01'
CMD_FREQ_10HZ = b'\x0A'
CMD_FREQ_100HZ = b'\x0B'
CMD_FREQ_200HZ = b'\x0C'

# Theme Colors (Dark Mode / Premium)
COLOR_BG = "#121212"
COLOR_SURFACE = "#1E1E1E"
COLOR_SURFACE_LIGHT = "#2C2C2C"
COLOR_ACCENT = "#03DAC6"
COLOR_PRIMARY = "#BB86FC"
COLOR_TEXT = "#E0E0E0"
COLOR_TEXT_SEC = "#A0A0A0"
COLOR_SUCCESS = "#00C853"
COLOR_ERROR = "#CF6679"
COLOR_WARNING = "#FFB74D"
COLOR_BORDER = "#333333"

FONT_HEADER = ("Segoe UI", 18, "bold")
FONT_SUBHEADER = ("Segoe UI", 14, "bold")
FONT_BODY = ("Segoe UI", 11)
FONT_BODY_BOLD = ("Segoe UI", 11, "bold")
FONT_MONO = ("Consolas", 10)
FONT_SMALL = ("Segoe UI", 9)

# ==========================================
# UTILITIES
# ==========================================
def calculate_crc16(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc = crc << 1
            crc &= 0xFFFF
    return crc

class ToolTip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip_window = None
        widget.bind("<Enter>", self.show_tip)
        widget.bind("<Leave>", self.hide_tip)

    def show_tip(self, event=None):
        x, y, _, _ = self.widget.bbox("insert")
        x += self.widget.winfo_rootx() + 25
        y += self.widget.winfo_rooty() + 20
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(tw, text=self.text, justify=tk.LEFT,
                         background="#333333", foreground="#FFFFFF",
                         relief=tk.SOLID, borderwidth=1,
                         font=("Segoe UI", 8))
        label.pack(ipadx=1)

    def hide_tip(self, event=None):
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None

# ==========================================
# BLE BACKEND
# ==========================================
class InsoleDevice:
    def __init__(self, address, name, manager):
        self.address = address
        self.name = name
        self.manager = manager
        self.client = None
        self.connected = False
        self.side = None

        self.battery_voltage = 0
        self.is_charging = False
        self.is_streaming = False
        self.frequency_code = 0x0C

        self.model_number = "--"
        self.manufacturer_name = "--"
        self.firmware_revision = "--"
        self.hardware_revision = "--"

        self.packet_count = 0
        self.crc_errors = 0
        self.sample_count = 0
        self.start_time = 0
        self.last_packet_time = 0
        self.channel_data = [0] * 8

        self._raw_buffer = bytearray()
        self._buffer_lock = Lock()

        self.on_data_update = None
        self.on_status_update = None

    async def connect(self):
        if self.connected and self.client:
            return True

        retries = [0.0, 0.8, 1.5, 2.5]
        last_err = None

        for delay in retries:
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                if self.client:
                    try:
                        await self.client.disconnect()
                    except Exception:
                        pass
                self.client = BleakClient(self.address, timeout=20.0, disconnected_callback=self._on_disconnect)
                await self.client.connect()
                self.connected = True
                print(f"[{self.address}] Connected")

                await self.client.start_notify(UUID_STATUS_CHAR, self._handle_status)
                await self.read_device_info()
                return True
            except (BleakError, Exception) as e:
                last_err = e
                self.connected = False
                emsg = str(e)
                if "InProgress" in emsg or "Operation already in progress" in emsg:
                    continue
                break

        print(f"[{self.address}] Connection Failed: {last_err}")
        return False

    async def disconnect(self):
        if self.client and self.connected:
            try:
                await self.client.disconnect()
            except Exception as e:
                print(f"Error disconnecting {self.address}: {e}")

    def _on_disconnect(self, client):
        self.connected = False
        print(f"[{self.address}] Disconnected")
        if self.manager:
            self.manager.handle_device_disconnect(self)

    async def read_device_info(self):
        if not self.connected or not self.client:
            return
        try:
            try:
                data = await self.client.read_gatt_char(UUID_MODEL_NUMBER)
                self.model_number = data.decode('utf-8').strip('\x00')
            except Exception as e:
                print(f"[{self.address}] Could not read Model Number: {e}")
            try:
                data = await self.client.read_gatt_char(UUID_MANUFACTURER_NAME)
                self.manufacturer_name = data.decode('utf-8').strip('\x00')
            except Exception as e:
                print(f"[{self.address}] Could not read Manufacturer Name: {e}")
            try:
                data = await self.client.read_gatt_char(UUID_FIRMWARE_REVISION)
                self.firmware_revision = data.decode('utf-8').strip('\x00')
            except Exception as e:
                print(f"[{self.address}] Could not read Firmware Revision: {e}")
            try:
                data = await self.client.read_gatt_char(UUID_HARDWARE_REVISION)
                self.hardware_revision = data.decode('utf-8').strip('\x00')
            except Exception as e:
                print(f"[{self.address}] Could not read Hardware Revision: {e}")
        except Exception as e:
            print(f"[{self.address}] Error reading device info: {e}")

    async def toggle_led(self):
        if not self.connected: return
        await self.client.write_gatt_char(UUID_CMD_CHAR, CMD_LED_TOGGLE, response=False)

    async def set_frequency(self, freq_cmd):
        if not self.connected: return
        await self.client.write_gatt_char(UUID_CMD_CHAR, freq_cmd, response=False)

    async def start_stream(self):
        if not self.connected: return
        self.packet_count = 0
        self.crc_errors = 0
        self.start_time = time.time()
        with self._buffer_lock:
            self._raw_buffer = bytearray()
        await self.client.start_notify(UUID_ADC_CHAR, self._handle_adc_data)
        self.is_streaming = True

    async def stop_stream(self):
        if not self.connected: return
        try:
            await self.client.stop_notify(UUID_ADC_CHAR)
        except:
            pass
        self.is_streaming = False

    def get_raw_data_and_clear(self):
        with self._buffer_lock:
            data = bytes(self._raw_buffer)
            self._raw_buffer = bytearray()
        return data

    def _handle_status(self, sender, data):
        if len(data) < 8: return
        self.is_charging = bool(data[0])
        self.is_streaming = bool(data[1])
        self.battery_voltage = struct.unpack('<H', data[2:4])[0]
        self.frequency_code = data[4]
        if self.on_status_update:
            self.on_status_update(self)

    def _handle_adc_data(self, sender, data):
        if len(data) != 20:
            return
        self.packet_count += 1
        current_time = time.time()
        self.last_packet_time = current_time

        self.channel_data[0] = struct.unpack_from('<H', data, 2)[0]
        self.channel_data[1] = struct.unpack_from('<H', data, 4)[0]
        self.channel_data[2] = struct.unpack_from('<H', data, 6)[0]
        self.channel_data[3] = struct.unpack_from('<H', data, 8)[0]
        self.channel_data[4] = struct.unpack_from('<H', data, 10)[0]
        self.channel_data[5] = struct.unpack_from('<H', data, 12)[0]
        self.channel_data[6] = struct.unpack_from('<H', data, 14)[0]
        self.channel_data[7] = struct.unpack_from('<H', data, 16)[0]

        with self._buffer_lock:
            self._raw_buffer.extend(struct.pack('<d', current_time))
            self._raw_buffer.extend(data)

class InsoleManager:
    def __init__(self, gui_callback=None):
        self.devices = []
        self.left_device = None
        self.right_device = None
        self.gui_callback = gui_callback
        self.logging_active = False

    def add_device(self, device):
        self.devices.append(device)

    def remove_device(self, device):
        if device in self.devices:
            self.devices.remove(device)
        if self.left_device == device: self.left_device = None
        if self.right_device == device: self.right_device = None

    def get_device_by_side(self, side):
        if side == 'Left': return self.left_device
        if side == 'Right': return self.right_device
        return None

    def assign_side(self, device, side):
        if side == 'Left':
            self.left_device = device
            device.side = 'Left'
        elif side == 'Right':
            self.right_device = device
            device.side = 'Right'

    def handle_device_disconnect(self, device):
        if self.gui_callback:
            self.gui_callback('disconnect', device)

    async def connect_all(self):
        results = []
        for d in self.devices:
            try:
                ok = await d.connect()
            except Exception as e:
                print(f"[{d.address}] Connection Failed in connect_all: {e}")
                ok = False
            results.append(bool(ok))
            await asyncio.sleep(0.15)
        return all(results)

    async def disconnect_device(self, device):
        await device.disconnect()
        self.remove_device(device)

    async def start_streaming_sync(self):
        if self.left_device and self.left_device.connected:
            print(f"[LEFT] Enabling notifications...")
            await self.left_device.start_stream()
            await asyncio.sleep(0.5)
        if self.right_device and self.right_device.connected:
            print(f"[RIGHT] Enabling notifications...")
            await self.right_device.start_stream()
            await asyncio.sleep(0.2)
        print("Both devices streaming")

    async def stop_streaming_sync(self):
        tasks = []
        if self.left_device: tasks.append(self.left_device.stop_stream())
        if self.right_device: tasks.append(self.right_device.stop_stream())
        await asyncio.gather(*tasks)

    async def set_frequency_sync(self, freq_cmd):
        tasks = []
        if self.left_device: tasks.append(self.left_device.set_frequency(freq_cmd))
        if self.right_device: tasks.append(self.right_device.set_frequency(freq_cmd))
        await asyncio.gather(*tasks)

    def start_logging(self):
        self.logging_active = True

    def stop_logging(self, filename):
        self.logging_active = False
        try:
            output = {"Left": [], "Right": []}
            packet_id = 0

            for dev in [self.left_device, self.right_device]:
                if not dev or not dev.side:
                    continue
                raw_data = dev.get_raw_data_and_clear()
                if len(raw_data) == 0:
                    continue

                record_size = 28
                num_records = len(raw_data) // record_size
                print(f"[{dev.side}] Processing {num_records} packets from binary buffer...")

                for i in range(num_records):
                    offset = i * record_size
                    record = raw_data[offset:offset + record_size]
                    timestamp = struct.unpack('<d', record[0:8])[0]
                    dev_ts = struct.unpack('<H', record[8:10])[0]
                    channels = struct.unpack('<8H', record[10:26])

                    packet_id += 1
                    entry = {
                        "Timestamp": datetime.fromtimestamp(timestamp).isoformat(),
                        "Device_TS_ms": dev_ts,
                        "Packet_ID": packet_id,
                        "Channels": {f"Ch{j}": int(val) for j, val in enumerate(channels)}
                    }
                    output[dev.side].append(entry)

            if not output["Left"] and not output["Right"]:
                print("No data to save")
                return False

            with open(filename, "w", encoding="utf-8") as f:
                json.dump(output, f, indent=2)

            print(f"Saved to {filename}")
            return True
        except Exception as e:
            print(f"Log Save Error: {e}")
            import traceback
            traceback.print_exc()
            return False

# ==========================================
# MODERN GUI
# ==========================================
class ModernGUI:
    def __init__(self, root, loop):
        self.root = root
        self.loop = loop
        self.manager = InsoleManager(self.on_manager_event)

        self.setup_window()
        try:
            self.create_styles()
        except Exception as e:
            print(f"[UI] Style initialization error: {e}")
            try:
                messagebox.showwarning("Style Error", f"Falling back to defaults.\n{e}")
            except Exception:
                pass
        try:
            self.create_layout()
        except Exception as e:
            print(f"[UI] Layout initialization error: {e}")
            try:
                messagebox.showerror("Layout Error", f"Could not create layout.\n{e}")
            except Exception:
                pass

        self.scanning = False
        self.is_streaming = False
        self.start_time = None

        self.update_ui_loop()

    def setup_window(self):
        self.root.title("GDPS Smart Insole System v2.1 (JSON Logging)")
        self.root.geometry("1400x900")
        self.root.configure(bg=COLOR_BG)
        try:
            self._debug_banner = tk.Frame(self.root, bg="#ffefc1")
            tk.Label(self._debug_banner, text="UI initializing...", bg="#ffefc1", fg="#000000").pack(padx=8, pady=4)
            self._debug_banner.pack(fill=tk.X)
        except Exception as e:
            print(f"[UI] Debug banner error: {e}")

    def create_styles(self):
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except tk.TclError:
            try:
                style.theme_use('aqua')
            except tk.TclError:
                try:
                    style.theme_use('default')
                except tk.TclError:
                    pass

        style.configure("TFrame", background=COLOR_BG)
        style.configure("Card.TFrame", background=COLOR_SURFACE, relief="flat")
        style.configure("Panel.TFrame", background=COLOR_SURFACE_LIGHT, relief="flat")

        style.configure("TNotebook", background=COLOR_BG, borderwidth=0)
        style.configure("TNotebook.Tab",
                        background=COLOR_SURFACE,
                        foreground=COLOR_TEXT,
                        padding=[20, 10],
                        font=FONT_BODY_BOLD,
                        borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", COLOR_ACCENT)],
                  foreground=[("selected", "black")])

        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT, font=FONT_BODY)
        style.configure("Header.TLabel", font=FONT_HEADER, foreground=COLOR_ACCENT, background=COLOR_BG)
        style.configure("SubHeader.TLabel", font=FONT_SUBHEADER, foreground=COLOR_TEXT, background=COLOR_SURFACE)
        style.configure("Card.TLabel", background=COLOR_SURFACE, foreground=COLOR_TEXT)
        style.configure("Panel.TLabel", background=COLOR_SURFACE_LIGHT, foreground=COLOR_TEXT)
        style.configure("Status.TLabel", background=COLOR_SURFACE, foreground=COLOR_TEXT_SEC, font=FONT_MONO)
        style.configure("Value.TLabel", background=COLOR_SURFACE_LIGHT, foreground=COLOR_ACCENT, font=("Consolas", 12, "bold"))

        style.configure("TButton",
                        background=COLOR_SURFACE_LIGHT,
                        foreground=COLOR_TEXT,
                        borderwidth=0,
                        font=FONT_BODY)
        style.map("TButton",
                  background=[('active', COLOR_ACCENT)],
                  foreground=[('active', 'black')])

        style.configure("Action.TButton", background=COLOR_PRIMARY, foreground="white", font=("Segoe UI", 11, "bold"))
        style.map("Action.TButton", background=[('active', "#9965f4")])

        style.configure("Stop.TButton", background=COLOR_ERROR, foreground="white", font=("Segoe UI", 11, "bold"))
        style.map("Stop.TButton", background=[('active', "#b00020")])

        style.configure("Destructive.TButton", background="#444444", foreground=COLOR_ERROR, font=("Segoe UI", 10))
        style.map("Destructive.TButton", background=[('active', "#555555")])

    def create_layout(self):
        self.main_container = ttk.Frame(self.root)
        self.main_container.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        self.create_header()

        self.notebook = ttk.Notebook(self.main_container)
        self.notebook.pack(fill=tk.BOTH, expand=True, pady=20)

        self.tab_connection = ttk.Frame(self.notebook, style="TFrame")
        self.notebook.add(self.tab_connection, text="  CONNECTION  ")
        self.create_connection_tab(self.tab_connection)

        self.tab_monitor = ttk.Frame(self.notebook, style="TFrame")
        self.notebook.add(self.tab_monitor, text="  MONITOR & STREAM  ")
        self.create_monitor_tab(self.tab_monitor)

        try:
            if hasattr(self, "_debug_banner") and self._debug_banner.winfo_exists():
                self._debug_banner.destroy()
        except Exception:
            pass

    def create_header(self):
        header_frame = ttk.Frame(self.main_container)
        header_frame.pack(fill=tk.X)
        ttk.Label(header_frame, text="GDPS Smart Insole System", style="Header.TLabel").pack(side=tk.LEFT)
        self.status_frame = ttk.Frame(header_frame)
        self.status_frame.pack(side=tk.RIGHT)
        self.lbl_global_status = ttk.Label(self.status_frame, text="System Ready", style="Status.TLabel", background=COLOR_BG)
        self.lbl_global_status.pack()

    def create_connection_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)

        scan_card = ttk.Frame(parent, style="Card.TFrame", padding=20)
        scan_card.grid(row=0, column=0, sticky="nsew", padx=(0, 10), pady=10)
        ttk.Label(scan_card, text="Device Scanner", style="SubHeader.TLabel").pack(anchor="w", pady=(0, 10))
        self.btn_scan = ttk.Button(scan_card, text="Start Scan", command=self.toggle_scan, style="Action.TButton")
        self.btn_scan.pack(fill=tk.X, pady=5)

        list_frame = ttk.Frame(scan_card, style="Card.TFrame")
        list_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        self.device_list = tk.Listbox(list_frame, bg="#2D2D2D", fg="white", selectbackground=COLOR_ACCENT, relief="flat", font=FONT_MONO, height=15)
        self.device_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.device_list.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.device_list.config(yscrollcommand=scrollbar.set)
        ttk.Button(scan_card, text="Add Selected to Pool", command=self.add_device_to_pool).pack(fill=tk.X, pady=5)

        pool_card = ttk.Frame(parent, style="Card.TFrame", padding=20)
        pool_card.grid(row=0, column=1, sticky="nsew", padx=(10, 0), pady=10)
        ttk.Label(pool_card, text="Connection Pool", style="SubHeader.TLabel").pack(anchor="w", pady=(0, 10))
        self.pool_container = ttk.Frame(pool_card, style="Card.TFrame")
        self.pool_container.pack(fill=tk.BOTH, expand=True)
        self.pool_rows_frame = ttk.Frame(self.pool_container, style="Card.TFrame")
        self.pool_rows_frame.pack(fill=tk.X)
        self.btn_connect_all = ttk.Button(pool_card, text="Connect All Devices", command=self.connect_devices, state=tk.DISABLED, style="Action.TButton")
        self.btn_connect_all.pack(fill=tk.X, pady=10)

        self.id_frame = ttk.Frame(pool_card, style="Card.TFrame")
        self.id_frame.pack(fill=tk.BOTH, expand=True, pady=10)
        ttk.Label(self.id_frame, text="Identification & Assignment", style="SubHeader.TLabel").pack(anchor="w")
        ttk.Label(self.id_frame, text="Blink LED to identify, then assign Left/Right side.", style="Card.TLabel").pack(anchor="w", pady=(0, 10))
        self.id_container = ttk.Frame(self.id_frame, style="Card.TFrame")
        self.id_container.pack(fill=tk.X)

        self.device_info_frame = ttk.Frame(pool_card, style="Panel.TFrame", padding=15)
        self.device_info_frame.pack(fill=tk.BOTH, expand=True, pady=10)
        ttk.Label(self.device_info_frame, text="Device Information", style="SubHeader.TLabel").pack(anchor="w", pady=(0, 10))
        self.device_info_container = ttk.Frame(self.device_info_frame, style="Panel.TFrame")
        self.device_info_container.pack(fill=tk.BOTH, expand=True)

    def refresh_pool_ui(self):
        for widget in self.pool_rows_frame.winfo_children():
            widget.destroy()
        if not self.manager.devices:
            ttk.Label(self.pool_rows_frame, text="No devices in pool.", style="Status.TLabel").pack(pady=10)
            self.btn_connect_all.config(state=tk.DISABLED)
            return
        self.btn_connect_all.config(state=tk.NORMAL)
        for i, dev in enumerate(self.manager.devices):
            row = ttk.Frame(self.pool_rows_frame, style="Panel.TFrame", padding=10)
            row.pack(fill=tk.X, pady=2)
            status_txt = "Connected" if dev.connected else "Not Connected"
            color = COLOR_SUCCESS if dev.connected else COLOR_TEXT_SEC
            ttk.Label(row, text=f"{dev.name or 'Unknown'}", style="Panel.TLabel", font=FONT_BODY_BOLD).pack(side=tk.LEFT)
            ttk.Label(row, text=f" ({dev.address})", style="Panel.TLabel").pack(side=tk.LEFT)
            btn_del = ttk.Button(row, text="Remove", style="Destructive.TButton", command=lambda d=dev: self.remove_device_from_pool(d))
            btn_del.pack(side=tk.RIGHT)
            lbl_stat = ttk.Label(row, text=status_txt, style="Panel.TLabel", foreground=color)
            lbl_stat.pack(side=tk.RIGHT, padx=10)

    def remove_device_from_pool(self, device):
        if device.connected:
            self.run_async(self.manager.disconnect_device(device))
        else:
            self.manager.remove_device(device)
        self.root.after(100, self.refresh_pool_ui)
        self.root.after(100, self.setup_identification_ui)

    def create_monitor_tab(self, parent):
        control_bar = ttk.Frame(parent, style="Card.TFrame", padding=15)
        control_bar.pack(fill=tk.X, pady=(0, 20))

        self.btn_stream = ttk.Button(control_bar, text="START STREAMING", command=self.toggle_streaming, style="Action.TButton")
        self.btn_stream.pack(side=tk.LEFT, padx=(0, 20))

        ttk.Label(control_bar, text="Frequency:", style="Card.TLabel").pack(side=tk.LEFT)
        self.freq_var = tk.StringVar(value="200Hz")
        self.freq_combo = ttk.Combobox(control_bar, textvariable=self.freq_var, values=["10Hz", "100Hz", "200Hz"], state="readonly", width=8)
        self.freq_combo.pack(side=tk.LEFT, padx=(5, 20))
        self.freq_combo.bind("<<ComboboxSelected>>", self.change_frequency)

        self.chk_log = tk.BooleanVar(value=False)
        ttk.Checkbutton(control_bar, text="Log to JSON", variable=self.chk_log, style="TCheckbutton").pack(side=tk.LEFT)

        ttk.Button(control_bar, text="Disconnect All", command=self.disconnect_all, style="Destructive.TButton").pack(side=tk.RIGHT)

        content = ttk.Frame(parent)
        content.pack(fill=tk.BOTH, expand=True)
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)

        self.panel_left = self.create_device_panel(content, "LEFT INSOLE")
        self.panel_left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.panel_right = self.create_device_panel(content, "RIGHT INSOLE")
        self.panel_right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))

    def create_device_panel(self, parent, title):
        frame = ttk.Frame(parent, style="Card.TFrame", padding=15)
        header = ttk.Frame(frame, style="Card.TFrame")
        header.pack(fill=tk.X, pady=(0, 15))
        ttk.Label(header, text=title, style="SubHeader.TLabel").pack(side=tk.LEFT)

        status_frame = ttk.Frame(frame, style="Panel.TFrame", padding=10)
        status_frame.pack(fill=tk.X, pady=(0, 15))

        def create_stat_box(p, label, row, col):
            f = ttk.Frame(p, style="Panel.TFrame")
            f.grid(row=row, column=col, sticky="ew", padx=5, pady=5)
            p.columnconfigure(col, weight=1)
            ttk.Label(f, text=label, style="Status.TLabel", font=("Segoe UI", 9)).pack(anchor="w")
            l_val = ttk.Label(f, text="--", style="Value.TLabel")
            l_val.pack(anchor="w")
            return l_val

        lbl_batt = create_stat_box(status_frame, "Battery", 0, 0)
        lbl_chg = create_stat_box(status_frame, "Charging", 0, 1)
        lbl_freq = create_stat_box(status_frame, "Freq Code", 1, 0)

        viz_frame = ttk.Frame(frame, style="Card.TFrame")
        viz_frame.pack(fill=tk.BOTH, expand=True)

        canvases = []
        labels = []
        for i in range(8):
            row = ttk.Frame(viz_frame, style="Card.TFrame")
            row.pack(fill=tk.X, pady=3)
            ttk.Label(row, text=f"Ch{i}", width=4, style="Card.TLabel", font=("Consolas", 10, "bold")).pack(side=tk.LEFT)
            canvas = tk.Canvas(row, height=18, bg="#2D2D2D", highlightthickness=0)
            canvas.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
            val_lbl = ttk.Label(row, text="0", width=5, anchor="e", style="Card.TLabel", font=("Consolas", 10))
            val_lbl.pack(side=tk.RIGHT)
            canvases.append(canvas)
            labels.append(val_lbl)

        frame.widgets = {
            'batt': lbl_batt,
            'chg': lbl_chg,
            'freq': lbl_freq,
            'canvases': canvases,
            'labels': labels
        }
        return frame

    # ==========================================
    # LOGIC & EVENTS
    # ==========================================
    def toggle_scan(self):
        if self.scanning: return
        self.scanning = True
        self.btn_scan.config(text="Scanning...", state=tk.DISABLED)
        self.device_list.delete(0, tk.END)

        async def scan():
            try:
                devices = await BleakScanner.discover(timeout=5.0)
                for d in devices:
                    if d.name:
                        self.root.after(0, lambda dev=d: self.device_list.insert(tk.END, f"{dev.name} | {dev.address}"))
            except Exception as e:
                print(f"Scan error: {e}")
            finally:
                self.root.after(0, self._scan_complete)

        self.run_async(scan())

    def _scan_complete(self):
        self.scanning = False
        self.btn_scan.config(text="Start Scan", state=tk.NORMAL)

    def add_device_to_pool(self):
        sel = self.device_list.curselection()
        if not sel: return
        item = self.device_list.get(sel[0])
        name, address = item.split(" | ")
        for d in self.manager.devices:
            if d.address == address: return
        self.manager.add_device(InsoleDevice(address, name, self.manager))
        self.refresh_pool_ui()

    def connect_devices(self):
        self.btn_connect_all.config(text="Connecting...", state=tk.DISABLED)
        async def connect():
            success = await self.manager.connect_all()
            self.root.after(0, lambda: self._connect_complete(success))
        self.run_async(connect())

    def _connect_complete(self, success):
        self.refresh_pool_ui()
        if success:
            messagebox.showinfo("Success", "Devices connected! Please identify them.")
            self.setup_identification_ui()
            self.update_device_info_display()
        else:
            messagebox.showerror("Error", "Failed to connect to one or both devices.")
            self.btn_connect_all.config(text="Connect All Devices", state=tk.NORMAL)

    def setup_identification_ui(self):
        for widget in self.id_container.winfo_children():
            widget.destroy()
        if not self.manager.devices: return
        for i, device in enumerate(self.manager.devices):
            if not device.connected: continue
            row = ttk.Frame(self.id_container, style="Panel.TFrame", padding=10)
            row.pack(fill=tk.X, pady=5)
            lbl = ttk.Label(row, text=f"{device.name}", style="Panel.TLabel", width=20)
            lbl.pack(side=tk.LEFT)
            btn_blink = ttk.Button(row, text="Blink LED", command=lambda d=device: self.run_async(d.toggle_led()))
            btn_blink.pack(side=tk.LEFT, padx=10)
            side_var = tk.StringVar(value=device.side if device.side else "Select Side")
            combo = ttk.Combobox(row, textvariable=side_var, values=["Left", "Right"], state="readonly", width=10)
            combo.pack(side=tk.LEFT, padx=10)
            def on_assign(event, d=device, v=side_var):
                self.manager.assign_side(d, v.get())
                self.check_assignments()
                self.update_device_info_display()
            combo.bind("<<ComboboxSelected>>", on_assign)

    def check_assignments(self):
        if self.manager.left_device and self.manager.right_device:
            self.lbl_global_status.config(text="Ready to Stream")

    def update_device_info_display(self):
        for widget in self.device_info_container.winfo_children():
            widget.destroy()
        connected_devices = [d for d in self.manager.devices if d.connected]
        if not connected_devices:
            return
        self.device_info_container.columnconfigure(0, weight=1)
        self.device_info_container.columnconfigure(1, weight=1)
        col = 0
        if self.manager.left_device and self.manager.left_device.connected:
            left_frame = ttk.Frame(self.device_info_container, style="Card.TFrame", padding=10)
            left_frame.grid(row=0, column=col, sticky="nsew", padx=(0 if col==0 else 5, 5 if col==0 else 0))
            ttk.Label(left_frame, text="LEFT INSOLE", style="Status.TLabel", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 5))
            self._create_device_info_rows(left_frame, self.manager.left_device)
            col += 1
        if self.manager.right_device and self.manager.right_device.connected:
            right_frame = ttk.Frame(self.device_info_container, style="Card.TFrame", padding=10)
            right_frame.grid(row=0, column=col, sticky="nsew", padx=(0 if col==0 else 5, 5 if col==0 else 0))
            ttk.Label(right_frame, text="RIGHT INSOLE", style="Status.TLabel", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 5))
            self._create_device_info_rows(right_frame, self.manager.right_device)
            col += 1
        for device in connected_devices:
            if device.side is None:
                if col >= 2:
                    col = 0
                    row = 1
                else:
                    row = 0
                unassigned_frame = ttk.Frame(self.device_info_container, style="Card.TFrame", padding=10)
                unassigned_frame.grid(row=row, column=col, sticky="nsew", padx=(0 if col==0 else 5, 5 if col==0 else 0), pady=(5 if row>0 else 0, 0))
                ttk.Label(unassigned_frame, text=f"{device.name} (Not Assigned)", style="Status.TLabel", font=("Segoe UI", 10, "bold"), foreground=COLOR_WARNING).pack(anchor="w", pady=(0, 5))
                self._create_device_info_rows(unassigned_frame, device)
                col += 1

    def _create_device_info_rows(self, parent, device):
        def create_info_row(p, label, value):
            f = ttk.Frame(p, style="Card.TFrame")
            f.pack(fill=tk.X, pady=2)
            ttk.Label(f, text=label + ":", style="Card.TLabel", font=("Segoe UI", 9), width=15).pack(side=tk.LEFT)
            ttk.Label(f, text=value, style="Card.TLabel", font=("Segoe UI", 9, "bold"), foreground=COLOR_ACCENT).pack(side=tk.LEFT, fill=tk.X, expand=True)
        create_info_row(parent, "Model Number", device.model_number)
        create_info_row(parent, "Manufacturer", device.manufacturer_name)
        create_info_row(parent, "Firmware Rev", device.firmware_revision)
        create_info_row(parent, "Hardware Rev", device.hardware_revision)

    def disconnect_all(self):
        async def disc():
            await self.manager.stop_streaming_sync()
            for d in list(self.manager.devices):
                await d.disconnect()
            self.root.after(0, self.refresh_pool_ui)
            self.root.after(0, self.setup_identification_ui)
            self.root.after(0, self.update_device_info_display)
        self.run_async(disc())

    def toggle_streaming(self):
        if not self.is_streaming:
            self.is_streaming = True
            self.btn_stream.config(text="STOP STREAMING", style="Stop.TButton")
            if self.chk_log.get():
                fname = f"GDPS_Log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                self.manager.start_logging()
                self.current_log_file = fname
            self.run_async(self.manager.start_streaming_sync())
        else:
            self.is_streaming = False
            self.btn_stream.config(text="START STREAMING", style="Action.TButton")
            async def stop_seq():
                await self.manager.stop_streaming_sync()
                if self.manager.logging_active:
                    self.root.after(0, lambda: messagebox.showinfo("Log Saved", "Saving JSON file..."))
                    success = self.manager.stop_logging(self.current_log_file)
                    msg = f"Log saved to {self.current_log_file}" if success else "Log save failed (no data?)"
                    self.root.after(0, lambda: messagebox.showinfo("Logging", msg))
            self.run_async(stop_seq())

    def change_frequency(self, event):
        val = self.freq_var.get()
        cmd = CMD_FREQ_200HZ
        if val == "10Hz": cmd = CMD_FREQ_10HZ
        elif val == "100Hz": cmd = CMD_FREQ_100HZ
        self.run_async(self.manager.set_frequency_sync(cmd))

    def on_manager_event(self, event_type, data):
        if event_type == 'disconnect':
            self.root.after(0, lambda: messagebox.showwarning("Disconnected", f"Device {data.address} disconnected!"))
            self.root.after(0, self.refresh_pool_ui)

    def update_ui_loop(self):
        if self.is_streaming:
            self._update_panel(self.manager.left_device, self.panel_left)
            self._update_panel(self.manager.right_device, self.panel_right)
        self._update_status_display(self.manager.left_device, self.panel_left)
        self._update_status_display(self.manager.right_device, self.panel_right)
        self.root.after(100, self.update_ui_loop)

    def _update_status_display(self, device, panel):
        if not device or not device.connected:
            panel.widgets['batt'].config(text="--")
            panel.widgets['chg'].config(text="--")
            panel.widgets['freq'].config(text="--")
            return
        panel.widgets['batt'].config(text=f"{device.battery_voltage} mV")
        panel.widgets['chg'].config(text="Yes" if device.is_charging else "No")
        panel.widgets['freq'].config(text=f"0x{device.frequency_code:02X}")

    def _update_panel(self, device, panel):
        if not device: return
        for i, val in enumerate(device.channel_data):
            canvas = panel.widgets['canvases'][i]
            label = panel.widgets['labels'][i]
            norm_val = max(0, min(4095, val))
            disp_val = 4095 - norm_val
            w = canvas.winfo_width()
            h = canvas.winfo_height()
            bar_w = (disp_val / 4095) * w
            if disp_val < 2048:
                r = 0
                g = int((disp_val / 2048) * 255)
                b = int(((2048 - disp_val) / 2048) * 255)
            else:
                r = int(((disp_val - 2048) / 2047) * 255)
                g = int(((4095 - disp_val) / 2047) * 255)
                b = 0
            color = f"#{r:02x}{g:02x}{b:02x}"
            canvas.delete("all")
            canvas.create_rectangle(0, 0, bar_w, h, fill=color, outline="")
            label.config(text=str(val))

    def run_async(self, coro):
        asyncio.run_coroutine_threadsafe(coro, self.loop)

# ==========================================
# MAIN ENTRY
# ==========================================
def run_asyncio_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

def main():
    root = tk.Tk()

    # Surface any Tk callback exceptions to the console for debugging
    def _tk_exception(exc, val, tb):
        import traceback
        print("[TK CALLBACK EXCEPTION]")
        traceback.print_exception(exc, val, tb)
    try:
        root.report_callback_exception = _tk_exception
    except Exception:
        pass

    def start_app():
        loop = asyncio.new_event_loop()
        root._loop = loop
        t = threading.Thread(target=run_asyncio_loop, args=(loop,), daemon=True)
        t.start()

        root._app = ModernGUI(root, loop)
        try:
            root.update_idletasks()
            root.update()
        except Exception:
            pass

    root.after(0, start_app)

    try:
        root.mainloop()
    finally:
        try:
            if hasattr(root, "_loop"):
                root._loop.call_soon_threadsafe(root._loop.stop)
        except Exception:
            pass

if __name__ == "__main__":
    main()
