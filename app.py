from __future__ import annotations

import io
import ast
import json
import os
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk


MAGIC = 0x49435059  # ICPY
PROTOCOL_VERSION = 4
HEADER = struct.Struct(">IHHIQQI")
DEVICE_PORT = 27183
LOCAL_PORT = 27183

HELLO = 1
HELLO_ACK = 2
AUTHENTICATE = 5
START_STREAM = 10
STOP_STREAM = 11
VIDEO_FRAME = 12
INPUT_TOUCH = 20
INPUT_KEY = 21
INPUT_TEXT = 22
CLIPBOARD_SET = 31
SYSTEM_ACTION = 50
PING = 60
PONG = 61
ERROR = 70
LOG = 71


@dataclass
class Device:
    udid: str
    name: str
    model: str
    ios: str


class IoscpyConnection:
    def __init__(self, event_queue: queue.Queue, local_port: int = LOCAL_PORT):
        self.events = event_queue
        self.local_port = local_port
        self.sock: socket.socket | None = None
        self.forwarder: subprocess.Popen | None = None
        self.running = False
        self.sequence = 0
        self.write_lock = threading.Lock()
        self.reader: threading.Thread | None = None

    @staticmethod
    def list_devices() -> list[Device]:
        cmd = [sys.executable, "-m", "pymobiledevice3", "usbmux", "list"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Không đọc được thiết bị USB")
        raw = json.loads(result.stdout)
        return [
            Device(
                udid=item.get("Identifier") or item.get("UniqueDeviceID", ""),
                name=item.get("DeviceName", "iPhone"),
                model=item.get("ProductType", "?"),
                ios=item.get("ProductVersion", "?"),
            )
            for item in raw
            if item.get("ConnectionType") == "USB"
        ]

    def _start_forwarder(self, udid: str) -> None:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        cmd = [
            sys.executable,
            "-m",
            "pymobiledevice3",
            "usbmux",
            "forward",
            str(self.local_port),
            str(DEVICE_PORT),
            "--serial",
            udid,
        ]
        self.forwarder = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

    def connect(self, udid: str) -> dict:
        self.disconnect()
        self._start_forwarder(udid)
        deadline = time.monotonic() + 8
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.forwarder and self.forwarder.poll() is not None:
                self.disconnect()
                raise RuntimeError(f"Không mở được đường hầm USB cổng {self.local_port}")
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.local_port), timeout=1)
                break
            except OSError as exc:
                last_error = exc
                time.sleep(0.25)
        else:
            self.disconnect()
            raise RuntimeError(f"Agent ioscpy trên iPhone chưa trả lời: {last_error}")

        try:
            self.sock.settimeout(5)
            hello = json.dumps(
                {
                    "role": "host",
                    "host_version": "0.1.0",
                    "protocol_version": PROTOCOL_VERSION,
                    "nonce": os.urandom(16).hex(),
                }
            ).encode()
            self.send(HELLO, hello)
            msg_type, payload = self._read_frame()
            if msg_type == ERROR:
                raise RuntimeError(self._format_daemon_error(payload))
            if msg_type != HELLO_ACK:
                raise RuntimeError(f"Phản hồi agent không hợp lệ: {msg_type}")
            ack = json.loads(payload)
            self.send(AUTHENTICATE, ack["session_token"].encode())
        except Exception:
            self.disconnect()
            raise
        self.sock.settimeout(None)
        self.running = True
        self.reader = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader.start()
        self.send(START_STREAM, b"\x00")  # MJPEG is easiest to render reliably.
        return ack

    def disconnect(self) -> None:
        self.running = False
        if self.sock:
            try:
                self.send(STOP_STREAM)
            except OSError:
                pass
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None
        if self.forwarder and self.forwarder.poll() is None:
            self.forwarder.terminate()
            try:
                self.forwarder.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.forwarder.kill()
        self.forwarder = None

    def send(self, msg_type: int, payload: bytes = b"", stream_id: int = 0) -> None:
        if not self.sock:
            raise OSError("Chưa kết nối")
        with self.write_lock:
            self.sequence += 1
            header = HEADER.pack(
                MAGIC,
                PROTOCOL_VERSION,
                msg_type,
                0,
                stream_id,
                self.sequence,
                len(payload),
            )
            self.sock.sendall(header + payload)

    def _recv_exact(self, size: int) -> bytes:
        if not self.sock:
            raise OSError("Mất kết nối")
        result = bytearray()
        while len(result) < size:
            chunk = self.sock.recv(size - len(result))
            if not chunk:
                raise OSError("iPhone đã ngắt kết nối")
            result.extend(chunk)
        return bytes(result)

    def _read_frame(self) -> tuple[int, bytes]:
        fields = HEADER.unpack(self._recv_exact(HEADER.size))
        magic, version, msg_type, _flags, _stream, _seq, length = fields
        if magic != MAGIC or version != PROTOCOL_VERSION:
            raise RuntimeError("Sai phiên bản giao thức ioscpy")
        if length > 16 * 1024 * 1024:
            raise RuntimeError("Khung dữ liệu quá lớn")
        return msg_type, self._recv_exact(length)

    @staticmethod
    def _format_daemon_error(payload: bytes) -> str:
        try:
            data = json.loads(payload)
            return f"{data.get('message', 'Lỗi agent')} {data.get('suggestion', '')}".strip()
        except Exception:
            return payload.decode(errors="replace")

    def _reader_loop(self) -> None:
        try:
            while self.running:
                msg_type, payload = self._read_frame()
                if msg_type == VIDEO_FRAME and len(payload) >= 16:
                    width, height, flags, length = struct.unpack(">IIII", payload[:16])
                    data = payload[16 : 16 + length]
                    if not flags & 1:  # MJPEG
                        self.events.put(("frame", (width, height, data)))
                elif msg_type == PING:
                    self.send(PONG)
                elif msg_type == ERROR:
                    self.events.put(("error", self._format_daemon_error(payload)))
                elif msg_type == LOG:
                    self.events.put(("log", payload.decode(errors="replace")))
        except Exception as exc:
            if self.running:
                self.events.put(("disconnected", str(exc)))
        finally:
            self.running = False

    def touch(self, phase: int, x: float, y: float) -> None:
        payload = struct.pack(">BBff", phase, 0, min(max(x, 0), 1), min(max(y, 0), 1))
        self.send(INPUT_TOUCH, payload)

    def tap(self, x: float, y: float) -> None:
        self.touch(0, x, y)
        time.sleep(0.04)
        self.touch(2, x, y)

    def swipe(self, x1: float, y1: float, x2: float, y2: float, duration: float) -> None:
        duration = max(0.05, float(duration))
        steps = max(2, min(120, int(duration * 60)))
        self.touch(0, x1, y1)
        for index in range(1, steps):
            ratio = index / steps
            self.touch(1, x1 + (x2 - x1) * ratio, y1 + (y2 - y1) * ratio)
            time.sleep(duration / steps)
        self.touch(2, x2, y2)

    def type_text(self, value: str) -> None:
        self.send(INPUT_TEXT, value.encode("utf-8"))

    def copy_text(self, value: str, paste: bool = False) -> None:
        flags = b"\x01" if paste else b"\x00"
        self.send(CLIPBOARD_SET, flags + value.encode("utf-8"))

    def long_press(self, x: float, y: float, duration: float = 0.8) -> None:
        self.touch(0, x, y)
        time.sleep(max(0.1, min(float(duration), 10.0)))
        self.touch(2, x, y)

    def system_action(self, action: int) -> None:
        self.send(SYSTEM_ACTION, struct.pack(">H", action))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("IOSTouch")
        self.geometry("560x820")
        self.minsize(440, 620)
        self.events: queue.Queue = queue.Queue(maxsize=5)
        self.conn = IoscpyConnection(self.events)
        self.devices: list[Device] = []
        self.photo: ImageTk.PhotoImage | None = None
        self.source_image: Image.Image | None = None
        self.image_rect = (0, 0, 1, 1)
        self.press_point: tuple[float, float] | None = None
        self.connected = False
        self.script_window: tk.Toplevel | None = None
        self.script_editor: tk.Text | None = None
        self.script_log: tk.Text | None = None
        self.script_stop = threading.Event()
        self.script_running = False
        self.ocr_engine = None
        self.ocr_lock = threading.Lock()
        self._build_ui()
        self.after(100, self._poll_events)
        self.after(250, self.refresh_devices)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)
        self.device_box = ttk.Combobox(top, state="readonly")
        self.device_box.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(top, text="Quét USB", command=self.refresh_devices).pack(side=tk.LEFT, padx=4)
        self.connect_button = ttk.Button(top, text="Kết nối", command=self.toggle_connection)
        self.connect_button.pack(side=tk.LEFT)

        self.status = tk.StringVar(value="Đang quét iPhone...")
        ttk.Label(self, textvariable=self.status, padding=(8, 0)).pack(fill=tk.X)

        self.canvas = tk.Canvas(self, background="#111", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.canvas.bind("<Configure>", lambda _e: self._render())
        self.canvas.bind("<ButtonPress-1>", self._pointer_down)
        self.canvas.bind("<B1-Motion>", self._pointer_move)
        self.canvas.bind("<ButtonRelease-1>", self._pointer_up)

        actions = ttk.Frame(self, padding=(8, 0))
        actions.pack(fill=tk.X)
        for label, code in (("Home", 1), ("Back", 8), ("Khóa", 2), ("Đa nhiệm", 4)):
            ttk.Button(actions, text=label, command=lambda c=code: self._action(c)).pack(
                side=tk.LEFT, expand=True, fill=tk.X, padx=2
            )
        ttk.Button(actions, text="Code", command=self.open_code_editor).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2
        )

        typing = ttk.Frame(self, padding=8)
        typing.pack(fill=tk.X)
        self.text_entry = ttk.Entry(typing)
        self.text_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.text_entry.bind("<Return>", lambda _e: self.send_text())
        ttk.Button(typing, text="Nhập chữ", command=self.send_text).pack(side=tk.LEFT, padx=(4, 0))

    def refresh_devices(self) -> None:
        def work():
            try:
                devices = self.conn.list_devices()
                self.after(0, lambda: self._set_devices(devices))
            except Exception as exc:
                error = str(exc)
                self.after(0, lambda e=error: self.status.set(f"Lỗi USB: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _set_devices(self, devices: list[Device]) -> None:
        self.devices = devices
        labels = [f"{d.name} — {d.model} — iOS {d.ios}" for d in devices]
        self.device_box["values"] = labels
        if labels:
            self.device_box.current(0)
            self.status.set("Đã thấy iPhone qua USB")
        else:
            self.status.set("Không thấy iPhone; hãy mở khóa và bấm Trust")

    def toggle_connection(self) -> None:
        if self.connected:
            self.conn.disconnect()
            self._set_disconnected("Đã ngắt kết nối")
            return
        index = self.device_box.current()
        if index < 0:
            messagebox.showwarning("USB", "Chưa tìm thấy iPhone")
            return
        self.connect_button.config(state=tk.DISABLED)
        self.status.set("Đang mở đường hầm USB...")
        udid = self.devices[index].udid

        def work():
            try:
                ack = self.conn.connect(udid)
                self.after(0, lambda: self._set_connected(ack))
            except Exception as exc:
                self.conn.disconnect()
                error = str(exc)
                self.after(0, lambda e=error: self._connect_failed(e))

        threading.Thread(target=work, daemon=True).start()

    def _set_connected(self, ack: dict) -> None:
        self.connected = True
        self.connect_button.config(text="Ngắt", state=tk.NORMAL)
        caps = ack.get("capabilities", {})
        self.status.set(
            f"Đã kết nối — agent {ack.get('daemon_version', '?')} — "
            f"{caps.get('jailbreak_layout', '?')} / {caps.get('injection_framework', '?')}"
        )

    def _connect_failed(self, error: str) -> None:
        self.connect_button.config(text="Kết nối", state=tk.NORMAL)
        self.status.set("Chưa kết nối được agent trên iPhone")
        messagebox.showerror(
            "Thiếu agent iPhone",
            error
            + "\n\nCài ioscpy từ repo https://lautarovculic.github.io/ioscpy-repo/ "
            "trong Sileo, respring, rồi chạm màn hình iPhone một lần.",
        )

    def _set_disconnected(self, text: str) -> None:
        self.connected = False
        self.connect_button.config(text="Kết nối", state=tk.NORMAL)
        self.status.set(text)

    def _poll_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "frame":
                    _w, _h, data = value
                    try:
                        self.source_image = Image.open(io.BytesIO(data)).convert("RGB")
                        self._render()
                    except Exception:
                        pass
                elif kind == "disconnected":
                    self._set_disconnected(f"Mất kết nối: {value}")
                elif kind == "error":
                    self.status.set(f"Agent: {value}")
        except queue.Empty:
            pass
        self.after(50, self._poll_events)

    def _render(self) -> None:
        if self.source_image is None:
            return
        cw, ch = max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1)
        iw, ih = self.source_image.size
        scale = min(cw / iw, ch / ih)
        w, h = max(1, int(iw * scale)), max(1, int(ih * scale))
        x, y = (cw - w) // 2, (ch - h) // 2
        resized = self.source_image.resize((w, h), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(resized)
        self.canvas.delete("all")
        self.canvas.create_image(x, y, anchor=tk.NW, image=self.photo)
        self.image_rect = (x, y, w, h)

    def _normalise(self, x: int, y: int) -> tuple[float, float] | None:
        left, top, width, height = self.image_rect
        if not (left <= x <= left + width and top <= y <= top + height):
            return None
        return (x - left) / width, (y - top) / height

    def _pointer_down(self, event) -> None:
        point = self._normalise(event.x, event.y)
        if not self.connected or point is None:
            return
        self.press_point = point
        try:
            self.conn.touch(0, *point)
        except OSError as exc:
            self._set_disconnected(str(exc))

    def _pointer_move(self, event) -> None:
        point = self._normalise(event.x, event.y)
        if not self.connected or self.press_point is None or point is None:
            return
        try:
            self.conn.touch(1, *point)
        except OSError:
            pass

    def _pointer_up(self, event) -> None:
        point = self._normalise(event.x, event.y) or self.press_point
        if not self.connected or self.press_point is None or point is None:
            self.press_point = None
            return
        try:
            self.conn.touch(2, *point)
        finally:
            self.press_point = None

    def send_text(self) -> None:
        value = self.text_entry.get()
        if not self.connected or not value:
            return
        try:
            self.conn.type_text(value)
            self.text_entry.delete(0, tk.END)
        except OSError as exc:
            self._set_disconnected(str(exc))

    def _action(self, code: int) -> None:
        if self.connected:
            try:
                self.conn.system_action(code)
            except OSError as exc:
                self._set_disconnected(str(exc))

    def open_code_editor(self) -> None:
        if self.script_window and self.script_window.winfo_exists():
            self.script_window.lift()
            return
        window = tk.Toplevel(self)
        window.title("USB Script Editor")
        window.geometry("760x680")
        self.script_window = window

        toolbar = ttk.Frame(window, padding=6)
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="Mở .lua", command=self._open_script).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Lưu", command=self._save_script).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="Chạy", command=self._run_script).pack(side=tk.LEFT, padx=(12, 4))
        ttk.Button(toolbar, text="Dừng", command=self._stop_script).pack(side=tk.LEFT)
        ttk.Label(
            toolbar,
            text="API: tap, swipe, inputText, sleep, ocr, tapText, home, back, lock, log",
        ).pack(side=tk.LEFT, padx=12)
        ttk.Button(toolbar, text="OCR màn hình", command=self._ocr_screen_button).pack(side=tk.RIGHT)

        editor = tk.Text(window, undo=True, wrap=tk.NONE, font=("Consolas", 11))
        editor.pack(fill=tk.BOTH, expand=True, padx=6)
        editor.insert(
            "1.0",
            '-- Script mẫu (tọa độ iPhone 8: 375 x 667)\n'
            'log("Bắt đầu")\n'
            'tap(187, 333)\n'
            'sleep(1)\n'
            'swipe(187, 560, 187, 220, 0.5)\n'
            'sleep(1)\n'
            'log("Hoàn tất")\n',
        )
        self.script_editor = editor

        ttk.Label(window, text="Log", padding=(6, 4, 6, 0)).pack(fill=tk.X)
        log_box = tk.Text(window, height=8, state=tk.DISABLED, font=("Consolas", 10))
        log_box.pack(fill=tk.X, padx=6, pady=(0, 6))
        self.script_log = log_box

    def _open_script(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.script_window,
            title="Mở script",
            filetypes=(("Lua script", "*.lua"), ("Text", "*.txt"), ("Tất cả", "*.*")),
        )
        if not path or not self.script_editor:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                content = handle.read()
            self.script_editor.delete("1.0", tk.END)
            self.script_editor.insert("1.0", content)
            self._script_log_line(f"Đã mở: {path}")
        except Exception as exc:
            messagebox.showerror("Mở script", str(exc), parent=self.script_window)

    def _save_script(self) -> None:
        if not self.script_editor:
            return
        path = filedialog.asksaveasfilename(
            parent=self.script_window,
            title="Lưu script",
            defaultextension=".lua",
            filetypes=(("Lua script", "*.lua"), ("Text", "*.txt")),
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.script_editor.get("1.0", "end-1c"))
            self._script_log_line(f"Đã lưu: {path}")
        except Exception as exc:
            messagebox.showerror("Lưu script", str(exc), parent=self.script_window)

    def _run_script(self) -> None:
        if self.script_running:
            self._script_log_line("Script đang chạy")
            return
        if not self.connected:
            messagebox.showwarning("Script", "Hãy kết nối iPhone trước", parent=self.script_window)
            return
        if not self.script_editor:
            return
        code = self.script_editor.get("1.0", "end-1c")
        self.script_stop.clear()
        self.script_running = True
        self._script_log_line("--- RUN ---")
        threading.Thread(target=self._script_worker, args=(code,), daemon=True).start()

    def _stop_script(self) -> None:
        self.script_stop.set()
        self._script_log_line("Đã gửi yêu cầu dừng")

    @staticmethod
    def _strip_lua_comment(line: str) -> str:
        quote = None
        escaped = False
        for index, char in enumerate(line):
            if escaped:
                escaped = False
                continue
            if char == "\\" and quote:
                escaped = True
                continue
            if char in ("'", '"'):
                quote = None if quote == char else (char if quote is None else quote)
            if quote is None and line[index : index + 2] == "--":
                return line[:index]
        return line

    @staticmethod
    def _parse_literal_args(raw: str) -> tuple:
        if not raw.strip():
            return ()
        # Accept a simple Lua-style array such as {20, 80, 335, 220}.
        if "=" not in raw:
            raw = raw.replace("{", "[").replace("}", "]")
        tree = ast.parse(f"({raw},)", mode="eval")
        value = ast.literal_eval(tree.body)
        return value if isinstance(value, tuple) else (value,)

    def _script_worker(self, code: str) -> None:
        try:
            for line_number, original in enumerate(code.splitlines(), 1):
                if self.script_stop.is_set():
                    raise InterruptedError("Script đã dừng")
                line = self._strip_lua_comment(original).strip().rstrip(";")
                if not line:
                    continue
                match = re.fullmatch(r"([A-Za-z_]\w*)\s*\((.*)\)", line)
                if not match:
                    raise ValueError(f"Dòng {line_number}: chỉ hỗ trợ lệnh gọi hàm đơn giản")
                name, raw_args = match.groups()
                try:
                    args = self._parse_literal_args(raw_args)
                    self._execute_script_command(name, args)
                except InterruptedError:
                    raise
                except Exception as exc:
                    raise ValueError(f"Dòng {line_number}: {exc}") from exc
            self.after(0, lambda: self._script_log_line("Hoàn tất"))
        except InterruptedError as exc:
            error = str(exc)
            self.after(0, lambda e=error: self._script_log_line(e))
        except Exception as exc:
            error = str(exc)
            self.after(0, lambda e=error: self._script_log_line(f"LỖI: {e}"))
        finally:
            self.script_running = False

    def _execute_script_command(self, name: str, args: tuple) -> None:
        # N3.lua uses WDA point coordinates for an iPhone 8 (375x667).
        if name in ("tap", "safeTap"):
            if len(args) != 2:
                raise ValueError(f"{name} cần 2 tham số")
            self.conn.tap(float(args[0]) / 375.0, float(args[1]) / 667.0)
        elif name == "swipe":
            if len(args) not in (4, 5):
                raise ValueError("swipe cần 4 hoặc 5 tham số")
            duration = float(args[4]) if len(args) == 5 else 0.4
            self.conn.swipe(
                float(args[0]) / 375.0,
                float(args[1]) / 667.0,
                float(args[2]) / 375.0,
                float(args[3]) / 667.0,
                duration,
            )
        elif name == "inputText":
            if len(args) != 1:
                raise ValueError("inputText cần một chuỗi")
            self.conn.type_text(str(args[0]))
        elif name == "ocr":
            region = self._parse_ocr_region(args)
            results = self._perform_ocr(region)
            if not results:
                self.after(0, lambda: self._script_log_line("OCR: không tìm thấy chữ"))
            for item in results:
                line = (
                    f"OCR [{item['score']:.2f}] {item['text']} "
                    f"@ ({item['x']:.0f},{item['y']:.0f},{item['width']:.0f},{item['height']:.0f})"
                )
                self.after(0, lambda value=line: self._script_log_line(value))
        elif name == "tapText":
            if not args:
                raise ValueError("tapText cần nội dung cần tìm")
            target = str(args[0]).casefold()
            region = self._parse_ocr_region(args[1:])
            results = self._perform_ocr(region)
            found = next((item for item in results if target in item["text"].casefold()), None)
            if not found:
                raise ValueError(f"không tìm thấy chữ: {args[0]}")
            center_x = found["x"] + found["width"] / 2
            center_y = found["y"] + found["height"] / 2
            self.conn.tap(center_x / 375.0, center_y / 667.0)
            message = f"tapText: {found['text']} @ ({center_x:.0f},{center_y:.0f})"
            self.after(0, lambda value=message: self._script_log_line(value))
        elif name == "sleep":
            if len(args) != 1:
                raise ValueError("sleep cần số giây")
            if self.script_stop.wait(max(0, float(args[0]))):
                raise InterruptedError("Script đã dừng")
        elif name in ("log", "toast"):
            self.after(0, lambda text=" ".join(map(str, args)): self._script_log_line(text))
        elif name in ("home", "back", "lock", "appSwitcher"):
            if args:
                raise ValueError(f"{name} không nhận tham số")
            actions = {"home": 1, "lock": 2, "appSwitcher": 4, "back": 8}
            self.conn.system_action(actions[name])
        else:
            raise ValueError(f"API chưa hỗ trợ: {name}")

    @staticmethod
    def _parse_ocr_region(args: tuple) -> tuple[float, float, float, float] | None:
        if not args:
            return None
        values = args[0] if len(args) == 1 and isinstance(args[0], (list, tuple)) else args
        if len(values) != 4:
            raise ValueError("vùng OCR cần x, y, width, height")
        x, y, width, height = map(float, values)
        if width <= 0 or height <= 0:
            raise ValueError("kích thước vùng OCR phải lớn hơn 0")
        return x, y, width, height

    def _perform_ocr(self, region: tuple[float, float, float, float] | None) -> list[dict]:
        if self.source_image is None:
            raise ValueError("chưa nhận được ảnh màn hình")
        image = self.source_image.copy().convert("RGB")
        image_width, image_height = image.size
        logical_w, logical_h = 375.0, 667.0
        offset_x = offset_y = 0

        if region:
            x, y, width, height = region
            left = max(0, min(image_width, round(x / logical_w * image_width)))
            top = max(0, min(image_height, round(y / logical_h * image_height)))
            right = max(left + 1, min(image_width, round((x + width) / logical_w * image_width)))
            bottom = max(top + 1, min(image_height, round((y + height) / logical_h * image_height)))
            image = image.crop((left, top, right, bottom))
            offset_x, offset_y = left, top

        with self.ocr_lock:
            if self.ocr_engine is None:
                self.after(0, lambda: self._script_log_line("Đang khởi tạo RapidOCR lần đầu..."))
                from rapidocr import RapidOCR

                self.ocr_engine = RapidOCR()
            import numpy as np

            output = self.ocr_engine(np.asarray(image))

        boxes = getattr(output, "boxes", None)
        texts = getattr(output, "txts", None)
        scores = getattr(output, "scores", None)
        if boxes is None or texts is None:
            return []
        results = []
        score_values = scores if scores is not None else [0.0] * len(texts)
        for box, text, score in zip(boxes, texts, score_values):
            xs = [float(point[0]) + offset_x for point in box]
            ys = [float(point[1]) + offset_y for point in box]
            left, right = min(xs), max(xs)
            top, bottom = min(ys), max(ys)
            results.append(
                {
                    "text": str(text),
                    "score": float(score),
                    "x": left / image_width * logical_w,
                    "y": top / image_height * logical_h,
                    "width": (right - left) / image_width * logical_w,
                    "height": (bottom - top) / image_height * logical_h,
                }
            )
        return results

    def _ocr_screen_button(self) -> None:
        if self.script_running:
            self._script_log_line("Hãy đợi script hiện tại hoàn tất")
            return
        if not self.connected:
            messagebox.showwarning("OCR", "Hãy kết nối iPhone trước", parent=self.script_window)
            return
        self.script_stop.clear()
        self.script_running = True
        threading.Thread(target=self._script_worker, args=("ocr()",), daemon=True).start()

    def _script_log_line(self, text: str) -> None:
        if not self.script_log or not self.script_log.winfo_exists():
            return
        self.script_log.config(state=tk.NORMAL)
        self.script_log.insert(tk.END, str(text) + "\n")
        self.script_log.see(tk.END)
        self.script_log.config(state=tk.DISABLED)

    def _close(self) -> None:
        self.script_stop.set()
        self.conn.disconnect()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
