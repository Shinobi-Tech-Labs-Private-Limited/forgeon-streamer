import asyncio
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
from datetime import datetime
from collections import deque
import struct
import time
from bleak import BleakScanner, BleakClient
from bleak.exc import BleakError
import pandas as pd
import math
import os

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

# Multi-sample BLE notification constants (must match firmware adc_manager.h)
SINGLE_SAMPLE_SIZE = 20   # 2B timestamp + 8×2B ADC + 2B CRC
SAMPLES_PER_NOTIFY = 12   # max samples packed per notification (12 × 20 = 240 bytes)

# Theme Colors (Dark Mode / Premium)
COLOR_BG = "#121212"
COLOR_SURFACE = "#1E1E1E"
COLOR_SURFACE_LIGHT = "#2C2C2C"
COLOR_ACCENT = "#03DAC6"  # Teal accent
COLOR_PRIMARY = "#BB86FC" # Purple primary
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
    """Calculate CRC-16-CCITT (0xFFFF initial value, polynomial 0x1021)"""
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
    """Simple ToolTip for widgets"""
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
    """Manages a single Insole Device (Left or Right)"""
    def __init__(self, address, name, manager):
        self.address = address
        self.name = name
        self.manager = manager  # Reference to InsoleManager
        self.client = None
        self.connected = False
        self.side = None  # 'Left' or 'Right', assigned later

        # State
        self.battery_voltage = 0
        self.is_charging = False
        self.is_streaming = False
        self.frequency_code = 0x0C # Default 200Hz

        # Device Information
        self.model_number = "--"
        self.manufacturer_name = "--"
        self.firmware_revision = "--"
        self.hardware_revision = "--"

        # Data
        self.packet_count = 0
        self.notify_count = 0
        self.crc_errors = 0
        self.sample_count = 0
        self.start_time = 0
        self.last_packet_time = 0
        self.channel_data = [0] * 8
        self.data_buffer = deque(maxlen=2000) # Store recent data points

        # Callbacks
        self.on_data_update = None
        self.on_status_update = None

    async def connect(self):
        try:
            self.client = BleakClient(self.address, timeout=20.0, disconnected_callback=self._on_disconnect)
            await self.client.connect()
            self.connected = True
            print(f"[{self.address}] Connected")

            # Subscribe to Status
            await self.client.start_notify(UUID_STATUS_CHAR, self._handle_status)

            # Read Device Information
            await self.read_device_info()

            return True
        except Exception as e:
            print(f"[{self.address}] Connection Failed: {e}")
            self.connected = False
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
        """Read Device Information Service characteristics"""
        if not self.connected or not self.client:
            return

        try:
            # Read Model Number String
            try:
                data = await self.client.read_gatt_char(UUID_MODEL_NUMBER)
                self.model_number = data.decode('utf-8').strip('\x00')
                print(f"[{self.address}] Model Number: {self.model_number}")
            except Exception as e:
                print(f"[{self.address}] Could not read Model Number: {e}")

            # Read Manufacturer Name String
            try:
                data = await self.client.read_gatt_char(UUID_MANUFACTURER_NAME)
                self.manufacturer_name = data.decode('utf-8').strip('\x00')
                print(f"[{self.address}] Manufacturer: {self.manufacturer_name}")
            except Exception as e:
                print(f"[{self.address}] Could not read Manufacturer Name: {e}")

            # Read Firmware Revision String
            try:
                data = await self.client.read_gatt_char(UUID_FIRMWARE_REVISION)
                self.firmware_revision = data.decode('utf-8').strip('\x00')
                print(f"[{self.address}] Firmware: {self.firmware_revision}")
            except Exception as e:
                print(f"[{self.address}] Could not read Firmware Revision: {e}")

            # Read Hardware Revision String
            try:
                data = await self.client.read_gatt_char(UUID_HARDWARE_REVISION)
                self.hardware_revision = data.decode('utf-8').strip('\x00')
                print(f"[{self.address}] Hardware: {self.hardware_revision}")
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
        self.notify_count = 0
        self.crc_errors = 0
        self.sample_count = 0
        self.start_time = time.time()
        await self.client.start_notify(UUID_ADC_CHAR, self._handle_adc_data)
        self.is_streaming = True

    async def stop_stream(self):
        if not self.connected: return
        try:
            await self.client.stop_notify(UUID_ADC_CHAR)
        except:
            pass
        self.is_streaming = False

    def _handle_status(self, sender, data):
        # Parse 8 bytes: [Charge, Stream, BattL, BattH, Freq, Res, Res, Res]
        if len(data) < 8: return
        self.is_charging = bool(data[0])
        self.is_streaming = bool(data[1])
        self.battery_voltage = struct.unpack('<H', data[2:4])[0]
        self.frequency_code = data[4]

        if self.on_status_update:
            self.on_status_update(self)

    def _handle_adc_data(self, sender, data):
        # Batched BLE notification: 1..12 samples × 20 bytes each
        # Each 20-byte sample: [Time(2), Ch0(2)...Ch7(2), CRC(2)]
        if len(data) < SINGLE_SAMPLE_SIZE or len(data) % SINGLE_SAMPLE_SIZE != 0:
            return  # corrupted / unexpected length

        num_samples = len(data) // SINGLE_SAMPLE_SIZE
        host_ts = time.time()
        self.notify_count += 1

        for i in range(num_samples):
            offset = i * SINGLE_SAMPLE_SIZE
            sample = data[offset : offset + SINGLE_SAMPLE_SIZE]

            # CRC Validation (per-sample)
            payload = sample[:18]
            received_crc = struct.unpack('<H', sample[18:20])[0]
            calculated_crc = calculate_crc16(payload)

            if received_crc != calculated_crc:
                self.crc_errors += 1
                continue  # skip this sample, try next

            # Parse Data
            timestamp_ms = struct.unpack('<H', sample[0:2])[0]
            channels = struct.unpack('<8H', sample[2:18])

            self.packet_count += 1
            self.last_packet_time = host_ts
            self.channel_data = list(channels)

            # Add to buffer for logging/graphing
            self.data_buffer.append({
                'timestamp': host_ts,
                'dev_ts': timestamp_ms,
                'channels': channels,
                'packet_id': self.packet_count
            })

        if self.on_data_update:
            self.on_data_update(self)

class InsoleManager:
    """Coordinating two InsoleDevices"""
    def __init__(self, gui_callback=None):
        self.devices = [] # List of InsoleDevice
        self.left_device = None
        self.right_device = None
        self.gui_callback = gui_callback
        self.log_data = []
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
        for index, device in enumerate(self.devices):
            results.append(await device.connect())

            # Give the BLE stack a moment to settle before the next connection.
            if index < len(self.devices) - 1:
                await asyncio.sleep(1.0)

        return all(results)

    async def disconnect_device(self, device):
        await device.disconnect()
        self.remove_device(device)

    async def start_streaming_sync(self):
        # Enable notifications as close as possible
        tasks = []
        if self.left_device and self.left_device.connected:
            tasks.append(self.left_device.start_stream())
        if self.right_device and self.right_device.connected:
            tasks.append(self.right_device.start_stream())
        await asyncio.gather(*tasks)

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
        self.log_data = []
        self.logging_active = True

    def stop_logging(self, filename):
        self.logging_active = False
        if not self.log_data: return False

        try:
            # Convert to DataFrame
            rows = []
            for entry in self.log_data:
                row = {
                    'Timestamp': datetime.fromtimestamp(entry['timestamp']).strftime('%Y-%m-%d %H:%M:%S.%f'),
                    'Device': entry['device'],
                    'Device_TS_ms': entry['dev_ts'],
                    'Packet_ID': entry['packet_id']
                }
                for i, val in enumerate(entry['channels']):
                    row[f'Ch{i}'] = val
                rows.append(row)

            df = pd.DataFrame(rows)

            # Split by device for cleaner sheets
            with pd.ExcelWriter(filename) as writer:
                if not df.empty:
                    if 'Left' in df['Device'].values:
                        df[df['Device'] == 'Left'].to_excel(writer, sheet_name='Left', index=False)
                    if 'Right' in df['Device'].values:
                        df[df['Device'] == 'Right'].to_excel(writer, sheet_name='Right', index=False)
            return True
        except Exception as e:
            print(f"Log Save Error: {e}")
            return False

    def collect_log_data(self):
        # Called periodically to drain device buffers into main log
        if not self.logging_active: return

        for dev in [self.left_device, self.right_device]:
            if dev:
                while dev.data_buffer:
                    item = dev.data_buffer.popleft()
                    item['device'] = dev.side
                    self.log_data.append(item)

# ==========================================
# MODERN GUI
# ==========================================
class ModernGUI:
    def __init__(self, root, loop):
        self.root = root
        self.loop = loop
        self.manager = InsoleManager(self.on_manager_event)

        self.setup_window()
        self.create_styles()
        self.create_layout()

        # State
        self.scanning = False
        self.is_streaming = False
        self.start_time = None

        # Start periodic UI updates
        self.update_ui_loop()

    def setup_window(self):
        self.root.title("GDPS Smart Insole System v2.0")
        self.root.geometry("1400x900")
        self.root.configure(bg=COLOR_BG)
        # self.root.state('zoomed') # Maximize on Windows

    def create_styles(self):
        style = ttk.Style()
        style.theme_use('clam')

        # Frames
        style.configure("TFrame", background=COLOR_BG)
        style.configure("Card.TFrame", background=COLOR_SURFACE, relief="flat")
        style.configure("Panel.TFrame", background=COLOR_SURFACE_LIGHT, relief="flat")

        # Notebook (Tabs)
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

        # Labels
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT, font=FONT_BODY)
        style.configure("Header.TLabel", font=FONT_HEADER, foreground=COLOR_ACCENT, background=COLOR_BG)
        style.configure("SubHeader.TLabel", font=FONT_SUBHEADER, foreground=COLOR_TEXT, background=COLOR_SURFACE)
        style.configure("Card.TLabel", background=COLOR_SURFACE, foreground=COLOR_TEXT)
        style.configure("Panel.TLabel", background=COLOR_SURFACE_LIGHT, foreground=COLOR_TEXT)
        style.configure("Status.TLabel", background=COLOR_SURFACE, foreground=COLOR_TEXT_SEC, font=FONT_MONO)
        style.configure("Value.TLabel", background=COLOR_SURFACE_LIGHT, foreground=COLOR_ACCENT, font=("Consolas", 12, "bold"))

        # Buttons
        style.configure("TButton",
                        background=COLOR_SURFACE_LIGHT,
                        foreground=COLOR_TEXT,
                        borderwidth=0,
                        focuscolor=COLOR_ACCENT,
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
        # Main Container
        self.main_container = ttk.Frame(self.root)
        self.main_container.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        # 1. Header
        self.create_header()

        # 2. Tabs
        self.notebook = ttk.Notebook(self.main_container)
        self.notebook.pack(fill=tk.BOTH, expand=True, pady=20)

        # Tab 1: Connection
        self.tab_connection = ttk.Frame(self.notebook, style="TFrame")
        self.notebook.add(self.tab_connection, text="  CONNECTION  ")
        self.create_connection_tab(self.tab_connection)

        # Tab 2: Monitor
        self.tab_monitor = ttk.Frame(self.notebook, style="TFrame")
        self.notebook.add(self.tab_monitor, text="  MONITOR & STREAM  ")

        # Tab 3: Data Processing
        self.tab_data_processing = ttk.Frame(self.notebook, style="TFrame")
        self.notebook.add(self.tab_data_processing, text="  DATA PROCESSING  ")
        self.create_data_processing_tab(self.tab_data_processing)
        self.create_monitor_tab(self.tab_monitor)

    # ==========================================
    # DATA PROCESSING TAB
    # ==========================================
    def create_data_processing_tab(self, parent):
        """Create the Data Processing tab for loading and analyzing Excel data"""

        # Constants for calculations
        self.VREF = 3300  # mV
        self.R1_FIXED = 51000  # 51K ohms (R1 fixed resistor)

        # Top: File Selection Bar
        file_bar = ttk.Frame(parent, style="Card.TFrame", padding=15)
        file_bar.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(file_bar, text="Select Excel File:", style="Card.TLabel").pack(side=tk.LEFT, padx=(0, 10))

        self.file_path_var = tk.StringVar(value="No file selected")
        self.lbl_file_path = ttk.Label(file_bar, textvariable=self.file_path_var, style="Status.TLabel", width=60)
        self.lbl_file_path.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_browse = ttk.Button(file_bar, text="Browse...", command=self.browse_excel_file, style="Action.TButton")
        self.btn_browse.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_load = ttk.Button(file_bar, text="Load & Process", command=self.load_and_process_file, state=tk.DISABLED, style="Action.TButton")
        self.btn_load.pack(side=tk.LEFT)

        # Sheet selection
        ttk.Label(file_bar, text="Sheet:", style="Card.TLabel").pack(side=tk.LEFT, padx=(20, 5))
        self.sheet_var = tk.StringVar(value="Left")
        self.sheet_combo = ttk.Combobox(file_bar, textvariable=self.sheet_var, values=["Left", "Right"], state="readonly", width=10)
        self.sheet_combo.pack(side=tk.LEFT, padx=(0, 10))

        # Export button
        self.btn_export = ttk.Button(file_bar, text="Export Processed", command=self.export_processed_data, state=tk.DISABLED)
        self.btn_export.pack(side=tk.RIGHT)

        # Info panel showing formulas
        info_frame = ttk.Frame(parent, style="Panel.TFrame", padding=10)
        info_frame.pack(fill=tk.X, pady=(0, 10))

        formula_text = (
            f"Vref = {self.VREF} mV  |  R1 (Fixed) = {self.R1_FIXED/1000:.0f} KΩ  |  "
            "R_derv = R1 × (ADC / (4095 - ADC))  |  "
            "R_calc = 6.6258 × e^(0.0477 × R_derv)  |  "
            "Weight = 0.651 + (14509670 - 0.651) / (1 + (R/2.61)^5.11)"
        )
        ttk.Label(info_frame, text=formula_text, style="Panel.TLabel", font=FONT_SMALL).pack(anchor="w")

        # Main Content: Data Table with scrollbars
        table_frame = ttk.Frame(parent, style="Card.TFrame", padding=10)
        table_frame.pack(fill=tk.BOTH, expand=True)

        # Create Treeview for data display
        columns = ["Timestamp", "Device_TS_ms", "Packet_ID",
                   "Ch0_ADC", "Ch0_R_derv", "Ch0_R_calc", "Ch0_Weight",
                   "Ch1_ADC", "Ch1_R_derv", "Ch1_R_calc", "Ch1_Weight",
                   "Ch2_ADC", "Ch2_R_derv", "Ch2_R_calc", "Ch2_Weight",
                   "Ch3_ADC", "Ch3_R_derv", "Ch3_R_calc", "Ch3_Weight",
                   "Ch4_ADC", "Ch4_R_derv", "Ch4_R_calc", "Ch4_Weight",
                   "Ch5_ADC", "Ch5_R_derv", "Ch5_R_calc", "Ch5_Weight",
                   "Ch6_ADC", "Ch6_R_derv", "Ch6_R_calc", "Ch6_Weight",
                   "Ch7_ADC", "Ch7_R_derv", "Ch7_R_calc", "Ch7_Weight"]

        # Treeview with horizontal and vertical scrollbars
        tree_container = ttk.Frame(table_frame)
        tree_container.pack(fill=tk.BOTH, expand=True)

        self.data_tree = ttk.Treeview(tree_container, columns=columns, show="headings", height=20)

        # Configure column headings and widths
        for col in columns:
            self.data_tree.heading(col, text=col)
            if "Timestamp" in col:
                self.data_tree.column(col, width=150, minwidth=100)
            elif "Weight" in col:
                self.data_tree.column(col, width=80, minwidth=60)
            elif "R_calc" in col or "R_derv" in col:
                self.data_tree.column(col, width=70, minwidth=50)
            elif "ADC" in col:
                self.data_tree.column(col, width=60, minwidth=50)
            else:
                self.data_tree.column(col, width=80, minwidth=60)

        # Scrollbars
        v_scroll = ttk.Scrollbar(tree_container, orient="vertical", command=self.data_tree.yview)
        h_scroll = ttk.Scrollbar(tree_container, orient="horizontal", command=self.data_tree.xview)
        self.data_tree.configure(yscrollcommand=v_scroll.set, xscrollcommand=h_scroll.set)

        # Grid layout for treeview and scrollbars
        self.data_tree.grid(row=0, column=0, sticky="nsew")
        v_scroll.grid(row=0, column=1, sticky="ns")
        h_scroll.grid(row=1, column=0, sticky="ew")

        tree_container.grid_rowconfigure(0, weight=1)
        tree_container.grid_columnconfigure(0, weight=1)

        # Configure treeview style
        style = ttk.Style()
        style.configure("Treeview",
                       background="#2D2D2D",
                       foreground=COLOR_TEXT,
                       fieldbackground="#2D2D2D",
                       font=FONT_MONO)
        style.configure("Treeview.Heading",
                       background=COLOR_SURFACE_LIGHT,
                       foreground=COLOR_ACCENT,
                       font=FONT_SMALL)

        # Status bar
        self.processing_status = tk.StringVar(value="Ready - Select an Excel file to begin")
        status_bar = ttk.Frame(parent, style="Card.TFrame", padding=5)
        status_bar.pack(fill=tk.X)
        ttk.Label(status_bar, textvariable=self.processing_status, style="Status.TLabel").pack(anchor="w")

        # Store processed data for export
        self.processed_df = None

    def browse_excel_file(self):
        """Open file dialog to select Excel file"""
        file_path = filedialog.askopenfilename(
            title="Select Excel File",
            filetypes=[("Excel files", "*.xlsx *.xls"), ("All files", "*.*")]
        )
        if file_path:
            self.file_path_var.set(file_path)
            self.btn_load.config(state=tk.NORMAL)

            # Try to get available sheets
            try:
                xl = pd.ExcelFile(file_path)
                sheets = xl.sheet_names
                self.sheet_combo.config(values=sheets)
                if sheets:
                    self.sheet_var.set(sheets[0])
            except Exception as e:
                print(f"Error reading sheets: {e}")

    def calculate_resistance_derived(self, adc_value):
        """
        Calculate derived resistance from ADC value using voltage divider formula.
        R2 = R1 × (Vadc / (Vref - Vadc))
        Since ADC is 12-bit: Vadc = (ADC/4095) × Vref
        Simplified: R2 = R1 × (ADC / (4095 - ADC))
        """
        if adc_value >= 4095 or adc_value <= 0:
            return 0  # Invalid ADC value

        try:
            r_derv = self.R1_FIXED * (adc_value / (4095 - adc_value))
            return r_derv / 1000  # Convert to KΩ
        except:
            return 0

    def calculate_resistance_calibrated(self, r_derv):
        """
        Apply calibration factor to get corrected resistance.
        Rcalc = 6.6258 × e^(0.0477 × Rderv)
        """
        if r_derv <= 0:
            return 0
        try:
            r_calc = 6.6258 * math.exp(0.0477 * r_derv)
            return r_calc
        except:
            return 0

    def calculate_weight(self, resistance):
        """
        Calculate weight from resistance using sigmoidal formula.
        w = 0.651049 + (14509670 - 0.651049) / (1 + (R/2.610584)^5.113386)
        """
        if resistance <= 0:
            return 0
        try:
            numerator = 14509670 - 0.651049
            denominator = 1 + pow(resistance / 2.610584, 5.113386)
            weight = 0.651049 + (numerator / denominator)
            return weight
        except:
            return 0

    def load_and_process_file(self):
        """Load Excel file and process data with resistance and weight calculations"""
        file_path = self.file_path_var.get()
        sheet_name = self.sheet_var.get()

        if not file_path or file_path == "No file selected":
            messagebox.showerror("Error", "Please select an Excel file first.")
            return

        self.processing_status.set("Loading file...")
        self.root.update_idletasks()

        try:
            # Load Excel file
            df = pd.read_excel(file_path, sheet_name=sheet_name)

            self.processing_status.set(f"Processing {len(df)} rows...")
            self.root.update_idletasks()

            # Clear existing data
            for item in self.data_tree.get_children():
                self.data_tree.delete(item)

            # Process each row and add calculated values
            processed_rows = []

            for idx, row in df.iterrows():
                processed_row = {
                    'Timestamp': row.get('Timestamp', ''),
                    'Device_TS_ms': row.get('Device_TS_ms', ''),
                    'Packet_ID': row.get('Packet_ID', '')
                }

                # Process each channel (Ch0-Ch7)
                for ch in range(8):
                    adc_col = f'Ch{ch}'
                    adc_value = row.get(adc_col, 0)

                    if pd.isna(adc_value):
                        adc_value = 0
                    else:
                        adc_value = int(adc_value)

                    # Calculate derived resistance
                    r_derv = self.calculate_resistance_derived(adc_value)

                    # Calculate calibrated resistance
                    r_calc = self.calculate_resistance_calibrated(r_derv)

                    # Calculate weight
                    weight = self.calculate_weight(r_calc)

                    processed_row[f'Ch{ch}_ADC'] = adc_value
                    processed_row[f'Ch{ch}_R_derv'] = f"{r_derv:.2f}"
                    processed_row[f'Ch{ch}_R_calc'] = f"{r_calc:.2f}"
                    processed_row[f'Ch{ch}_Weight'] = f"{weight:.2f}"

                processed_rows.append(processed_row)

                # Insert into treeview
                values = [
                    processed_row['Timestamp'],
                    processed_row['Device_TS_ms'],
                    processed_row['Packet_ID']
                ]
                for ch in range(8):
                    values.extend([
                        processed_row[f'Ch{ch}_ADC'],
                        processed_row[f'Ch{ch}_R_derv'],
                        processed_row[f'Ch{ch}_R_calc'],
                        processed_row[f'Ch{ch}_Weight']
                    ])

                self.data_tree.insert("", "end", values=values)

                # Update status every 100 rows
                if idx % 100 == 0:
                    self.processing_status.set(f"Processing row {idx + 1} of {len(df)}...")
                    self.root.update_idletasks()

            # Store processed data for export
            self.processed_df = pd.DataFrame(processed_rows)
            self.btn_export.config(state=tk.NORMAL)

            self.processing_status.set(f"Loaded {len(df)} rows from '{sheet_name}' sheet. Ready for export.")

        except Exception as e:
            messagebox.showerror("Error", f"Failed to load file: {str(e)}")
            self.processing_status.set(f"Error: {str(e)}")

    def export_processed_data(self):
        """Export processed data to a new Excel file"""
        if self.processed_df is None or self.processed_df.empty:
            messagebox.showerror("Error", "No processed data to export.")
            return

        # Ask for save location
        file_path = filedialog.asksaveasfilename(
            title="Save Processed Data",
            defaultextension=".xlsx",
            filetypes=[("Excel files", "*.xlsx"), ("All files", "*.*")],
            initialfile=f"Processed_Data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        )

        if file_path:
            try:
                self.processed_df.to_excel(file_path, index=False)
                messagebox.showinfo("Success", f"Data exported to:\n{file_path}")
                self.processing_status.set(f"Exported to: {os.path.basename(file_path)}")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to export: {str(e)}")

    def create_header(self):
        header_frame = ttk.Frame(self.main_container)
        header_frame.pack(fill=tk.X)

        ttk.Label(header_frame, text="GDPS Smart Insole System", style="Header.TLabel").pack(side=tk.LEFT)

        # Global Status
        self.status_frame = ttk.Frame(header_frame)
        self.status_frame.pack(side=tk.RIGHT)

        self.lbl_global_status = ttk.Label(self.status_frame, text="System Ready", style="Status.TLabel", background=COLOR_BG)
        self.lbl_global_status.pack()

    # ==========================================
    # TAB 1: CONNECTION
    # ==========================================
    def create_connection_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)

        # LEFT: Scanner
        scan_card = ttk.Frame(parent, style="Card.TFrame", padding=20)
        scan_card.grid(row=0, column=0, sticky="nsew", padx=(0, 10), pady=10)

        ttk.Label(scan_card, text="Device Scanner", style="SubHeader.TLabel").pack(anchor="w", pady=(0, 10))

        self.btn_scan = ttk.Button(scan_card, text="Start Scan", command=self.toggle_scan, style="Action.TButton")
        self.btn_scan.pack(fill=tk.X, pady=5)

        # Listbox
        list_frame = ttk.Frame(scan_card, style="Card.TFrame")
        list_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.device_list = tk.Listbox(list_frame, bg="#2D2D2D", fg="white", selectbackground=COLOR_ACCENT, relief="flat", font=FONT_MONO, height=15)
        self.device_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.device_list.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.device_list.config(yscrollcommand=scrollbar.set)

        ttk.Button(scan_card, text="Add Selected to Pool", command=self.add_device_to_pool).pack(fill=tk.X, pady=5)

        # RIGHT: Connection Pool
        pool_card = ttk.Frame(parent, style="Card.TFrame", padding=20)
        pool_card.grid(row=0, column=1, sticky="nsew", padx=(10, 0), pady=10)

        ttk.Label(pool_card, text="Connection Pool", style="SubHeader.TLabel").pack(anchor="w", pady=(0, 10))

        self.pool_container = ttk.Frame(pool_card, style="Card.TFrame")
        self.pool_container.pack(fill=tk.BOTH, expand=True)

        # We will dynamically add rows here for devices in pool
        self.pool_rows_frame = ttk.Frame(self.pool_container, style="Card.TFrame")
        self.pool_rows_frame.pack(fill=tk.X)

        self.btn_connect_all = ttk.Button(pool_card, text="Connect All Devices", command=self.connect_devices, state=tk.DISABLED, style="Action.TButton")
        self.btn_connect_all.pack(fill=tk.X, pady=10)

        # Identification Section (Visible after connection)
        self.id_frame = ttk.Frame(pool_card, style="Card.TFrame")
        self.id_frame.pack(fill=tk.BOTH, expand=True, pady=10)

        ttk.Label(self.id_frame, text="Identification & Assignment", style="SubHeader.TLabel").pack(anchor="w")
        ttk.Label(self.id_frame, text="Blink LED to identify, then assign Left/Right side.", style="Card.TLabel").pack(anchor="w", pady=(0, 10))

        self.id_container = ttk.Frame(self.id_frame, style="Card.TFrame")
        self.id_container.pack(fill=tk.X)

        # Device Information Section (Visible after connection)
        self.device_info_frame = ttk.Frame(pool_card, style="Panel.TFrame", padding=15)
        self.device_info_frame.pack(fill=tk.BOTH, expand=True, pady=10)

        ttk.Label(self.device_info_frame, text="Device Information", style="SubHeader.TLabel").pack(anchor="w", pady=(0, 10))

        # Container for device info panels (left and right)
        self.device_info_container = ttk.Frame(self.device_info_frame, style="Panel.TFrame")
        self.device_info_container.pack(fill=tk.BOTH, expand=True)

    def refresh_pool_ui(self):
        # Clear existing
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

    # ==========================================
    # TAB 2: MONITOR
    # ==========================================
    def create_monitor_tab(self, parent):
        # Top: Controls
        control_bar = ttk.Frame(parent, style="Card.TFrame", padding=15)
        control_bar.pack(fill=tk.X, pady=(0, 20))

        # Stream Button
        self.btn_stream = ttk.Button(control_bar, text="START STREAMING", command=self.toggle_streaming, style="Action.TButton")
        self.btn_stream.pack(side=tk.LEFT, padx=(0, 20))

        # Frequency
        ttk.Label(control_bar, text="Frequency:", style="Card.TLabel").pack(side=tk.LEFT)
        self.freq_var = tk.StringVar(value="200Hz")
        self.freq_combo = ttk.Combobox(control_bar, textvariable=self.freq_var, values=["10Hz", "100Hz", "200Hz"], state="readonly", width=8)
        self.freq_combo.pack(side=tk.LEFT, padx=(5, 20))
        self.freq_combo.bind("<<ComboboxSelected>>", self.change_frequency)

        # Logging
        self.chk_log = tk.BooleanVar(value=False)
        ttk.Checkbutton(control_bar, text="Log to Excel", variable=self.chk_log, style="TCheckbutton").pack(side=tk.LEFT)

        # Disconnect All
        ttk.Button(control_bar, text="Disconnect All", command=self.disconnect_all, style="Destructive.TButton").pack(side=tk.RIGHT)

        # Main Content: Split Left/Right
        content = ttk.Frame(parent)
        content.pack(fill=tk.BOTH, expand=True)

        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)

        # Left Insole Panel
        self.panel_left = self.create_device_panel(content, "LEFT INSOLE")
        self.panel_left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        # Right Insole Panel
        self.panel_right = self.create_device_panel(content, "RIGHT INSOLE")
        self.panel_right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))

    def create_device_panel(self, parent, title):
        frame = ttk.Frame(parent, style="Card.TFrame", padding=15)

        # Header
        header = ttk.Frame(frame, style="Card.TFrame")
        header.pack(fill=tk.X, pady=(0, 15))
        ttk.Label(header, text=title, style="SubHeader.TLabel").pack(side=tk.LEFT)

        # 1. System Status Grid
        status_frame = ttk.Frame(frame, style="Panel.TFrame", padding=10)
        status_frame.pack(fill=tk.X, pady=(0, 15))

        # Grid layout for status
        # Battery | Charging | Freq | Packets

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
        lbl_freq = create_stat_box(status_frame, "Freq Code", 0, 2)
        lbl_rate = create_stat_box(status_frame, "Packet Rate", 0, 3)

        lbl_samples = create_stat_box(status_frame, "Samples recv", 1, 0)
        lbl_expected = create_stat_box(status_frame, "Expected smpl", 1, 1)
        lbl_time = create_stat_box(status_frame, "Elapsed time", 1, 2)

        # 2. Visualization (Bars)
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
            'rate': lbl_rate,
            'samples': lbl_samples,
            'expected': lbl_expected,
            'time': lbl_time,
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

        # Check duplicates
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
            # Update device info display after connection
            self.update_device_info_display()
        else:
            messagebox.showerror("Error", "Failed to connect to one or both devices.")
            self.btn_connect_all.config(text="Connect All Devices", state=tk.NORMAL)

    def setup_identification_ui(self):
        # Clear previous
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

            # Selection
            side_var = tk.StringVar(value=device.side if device.side else "Select Side")
            combo = ttk.Combobox(row, textvariable=side_var, values=["Left", "Right"], state="readonly", width=10)
            combo.pack(side=tk.LEFT, padx=10)

            def on_assign(event, d=device, v=side_var):
                self.manager.assign_side(d, v.get())
                self.check_assignments()
                # Update device info display when assignment changes
                self.update_device_info_display()

            combo.bind("<<ComboboxSelected>>", on_assign)

    def check_assignments(self):
        if self.manager.left_device and self.manager.right_device:
            self.lbl_global_status.config(text="Ready to Stream")
            # Automatically switch to Monitor tab?
            # self.notebook.select(self.tab_monitor)

    def update_device_info_display(self):
        """Update the device information display in the connection tab"""
        # Clear existing
        for widget in self.device_info_container.winfo_children():
            widget.destroy()

        # Get all connected devices
        connected_devices = [d for d in self.manager.devices if d.connected]

        if not connected_devices:
            return

        # Display devices in columns
        self.device_info_container.columnconfigure(0, weight=1)
        self.device_info_container.columnconfigure(1, weight=1)

        col = 0
        # First show assigned devices
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

        # Show unassigned devices
        for device in connected_devices:
            if device.side is None:  # Not assigned
                if col >= 2:  # If we already have 2 columns, start a new row
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
        """Create device info rows for a device"""
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
            # Start
            self.is_streaming = True
            self.btn_stream.config(text="STOP STREAMING", style="Stop.TButton")

            if self.chk_log.get():
                fname = f"GDPS_Log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
                self.manager.start_logging()
                self.current_log_file = fname

            self.run_async(self.manager.start_streaming_sync())
        else:
            # Stop
            self.is_streaming = False
            self.btn_stream.config(text="START STREAMING", style="Action.TButton")

            async def stop_seq():
                await self.manager.stop_streaming_sync()
                if self.manager.logging_active:
                    self.root.after(0, lambda: messagebox.showinfo("Log Saved", "Saving Excel file..."))
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
        # Update Visualizations
        if self.is_streaming:
            self.manager.collect_log_data()
            self._update_panel(self.manager.left_device, self.panel_left)
            self._update_panel(self.manager.right_device, self.panel_right)

        # Also update status if connected (even if not streaming, if status notify is on)
        # But status notify is usually on after connect.
        self._update_status_display(self.manager.left_device, self.panel_left)
        self._update_status_display(self.manager.right_device, self.panel_right)

        self.root.after(50, self.update_ui_loop)

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

        # Update Packet Rate and other metrics
        if hasattr(device, 'start_time') and 'rate' in panel.widgets:
            elapsed = time.time() - device.start_time
            rate = device.notify_count / elapsed if elapsed > 0 else 0

            # Formulate expected frequency from frequency_code
            # freq_mode_0 = 0x0A (10Hz), freq_mode_1 = 0x0B (100Hz), freq_mode_2 = 0x0C (200Hz)
            expected_rate = 200 # Default fallback
            if device.frequency_code == 0x0A: expected_rate = 10
            elif device.frequency_code == 0x0B: expected_rate = 100
            elif device.frequency_code == 0x0C: expected_rate = 200

            expected_samples = int(expected_rate * elapsed) if elapsed > 0 else 0

            # Format time elapsed (MM:SS)
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)

            panel.widgets['rate'].config(text=f"{rate:.1f} pkt/s")
            panel.widgets['samples'].config(text=f"{device.packet_count}")
            panel.widgets['expected'].config(text=f"{expected_samples}")
            panel.widgets['time'].config(text=f"{mins:02d}:{secs:02d}")

        # Update Bars
        for i, val in enumerate(device.channel_data):
            canvas = panel.widgets['canvases'][i]
            label = panel.widgets['labels'][i]

            # Value 0-4095. 0 is Max Pressure (Full Bar), 4095 is Min (Empty)
            # Invert for display: 4095-val
            norm_val = max(0, min(4095, val))
            disp_val = 4095 - norm_val

            w = canvas.winfo_width()
            h = canvas.winfo_height()

            bar_w = (disp_val / 4095) * w

            # Color Gradient
            # Low (Blue) -> Med (Green) -> High (Red)
            if disp_val < 2048:
                # Blue -> Green
                r = 0
                g = int((disp_val / 2048) * 255)
                b = int(((2048 - disp_val) / 2048) * 255)
            else:
                # Green -> Red
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

    # Create Asyncio Loop in separate thread
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=run_asyncio_loop, args=(loop,), daemon=True)
    t.start()

    app = ModernGUI(root, loop)

    try:
        root.mainloop()
    finally:
        loop.call_soon_threadsafe(loop.stop)

if __name__ == "__main__":
    main()
