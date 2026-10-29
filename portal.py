from __future__ import annotations

import asyncio
import ast
import importlib.util
import io
import json
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from rapidocr import RapidOCR

from app import Device, IoscpyConnection


ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"


class TouchBody(BaseModel):
    x: float
    y: float


class SwipeBody(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float
    duration: float = 0.4


class TextBody(BaseModel):
    text: str


class ActionBody(BaseModel):
    action: int


class AppBody(BaseModel):
    bundle_id: str


class ScriptBody(BaseModel):
    code: str


class FileBody(BaseModel):
    name: str
    content: str = ""


@dataclass
class SessionState:
    connected: bool = False
    connecting: bool = False
    error: str = ""
    daemon_version: str = ""
    jailbreak: str = ""


class DeviceSession:
    def __init__(self, device: Device, local_port: int):
        self.device = device
        self.local_port = local_port
        self.events: queue.Queue = queue.Queue(maxsize=8)
        self.connection = IoscpyConnection(self.events, local_port=local_port)
        self.state = SessionState()
        self.latest_frame: bytes | None = None
        self.frame_id = 0
        self.lock = threading.Lock()
        self.connect_lock = threading.Lock()
        self.desired_connected = False
        self.reconnect_thread: threading.Thread | None = None
        self.consumer = threading.Thread(target=self._consume, daemon=True)
        self.script_lock = threading.Lock()
        self.script_cancel = threading.Event()
        self.consumer.start()

    def _consume(self) -> None:
        while True:
            kind, value = self.events.get()
            if kind == "frame":
                _width, _height, data = value
                with self.lock:
                    self.latest_frame = data
                    self.frame_id += 1
            elif kind == "disconnected":
                self.state.connected = False
                self.state.error = str(value)
                if self.desired_connected:
                    self._schedule_reconnect()
            elif kind == "error":
                self.state.error = str(value)

    def connect(self) -> None:
        self.desired_connected = True
        with self.connect_lock:
            if self.state.connected or self.state.connecting:
                return
            self.state.connecting = True
            self.state.error = ""
            try:
                ack = self.connection.connect(self.device.udid)
                caps = ack.get("capabilities", {})
                self.state.connected = True
                self.state.daemon_version = ack.get("daemon_version", "")
                self.state.jailbreak = " / ".join(
                    part for part in (caps.get("jailbreak_layout", ""), caps.get("injection_framework", "")) if part
                )
            except Exception as exc:
                self.state.error = str(exc)
                self.connection.disconnect()
                if self.desired_connected:
                    self._schedule_reconnect()
                raise
            finally:
                self.state.connecting = False

    def _schedule_reconnect(self) -> None:
        if self.reconnect_thread and self.reconnect_thread.is_alive():
            return

        def reconnect() -> None:
            while self.desired_connected and not self.state.connected:
                time.sleep(1.5)
                if not self.desired_connected:
                    return
                try:
                    self.connect()
                except Exception:
                    time.sleep(1.5)

        self.reconnect_thread = threading.Thread(target=reconnect, daemon=True)
        self.reconnect_thread.start()

    def disconnect(self) -> None:
        self.desired_connected = False
        self.connection.disconnect()
        self.state.connected = False

    def snapshot(self) -> dict:
        result = asdict(self.device)
        result.update(asdict(self.state))
        result["local_port"] = self.local_port
        result["has_frame"] = self.latest_frame is not None
        return result


class Manager:
    def __init__(self):
        self.sessions: dict[str, DeviceSession] = {}
        self.lock = threading.Lock()
        self.ocr: RapidOCR | None = None
        self.ocr_lock = threading.Lock()
        self.logs: deque[dict] = deque(maxlen=500)
        self.log_id = 0
        self.log("system", "IOSTouch backend started")

    def log(self, level: str, message: str, device: str = "") -> None:
        self.log_id += 1
        self.logs.append(
            {
                "id": self.log_id,
                "time": time.strftime("%H:%M:%S"),
                "level": level,
                "message": message,
                "device": device,
            }
        )

    def refresh(self) -> list[DeviceSession]:
        devices = IoscpyConnection.list_devices()
        with self.lock:
            for device in devices:
                if device.udid not in self.sessions:
                    used = {session.local_port for session in self.sessions.values()}
                    port = 27183
                    while port in used:
                        port += 1
                    self.sessions[device.udid] = DeviceSession(device, port)
                else:
                    self.sessions[device.udid].device = device
            present = {device.udid for device in devices}
            for udid, session in self.sessions.items():
                if udid not in present and session.state.connected:
                    session.state.error = "Đã rút cáp USB"
                    session.disconnect()
            return [self.sessions[device.udid] for device in devices]

    def get(self, udid: str) -> DeviceSession:
        session = self.sessions.get(udid)
        if not session:
            self.refresh()
            session = self.sessions.get(udid)
        if not session:
            raise HTTPException(404, "Không tìm thấy iPhone")
        return session

    def run_ocr(self, session: DeviceSession, region=None) -> list[dict]:
        with session.lock:
            frame = session.latest_frame
        if not frame:
            raise HTTPException(409, "Chưa nhận được ảnh màn hình")
        from PIL import Image

        image = Image.open(io.BytesIO(frame)).convert("RGB")
        full_width, full_height = image.size
        offset_x = offset_y = 0
        if region is not None:
            if not isinstance(region, (list, tuple)) or len(region) != 4:
                raise ValueError("OCR region cần {x, y, width, height}")
            x, y, region_width, region_height = map(float, region)
            left = max(0, min(full_width - 1, round(x / 375 * full_width)))
            top = max(0, min(full_height - 1, round(y / 667 * full_height)))
            right = max(left + 1, min(full_width, round((x + region_width) / 375 * full_width)))
            bottom = max(top + 1, min(full_height, round((y + region_height) / 667 * full_height)))
            image = image.crop((left, top, right, bottom))
            offset_x, offset_y = left, top
        with self.ocr_lock:
            if self.ocr is None:
                self.ocr = RapidOCR()
            output = self.ocr(np.asarray(image))
        if output.boxes is None or output.txts is None:
            return []
        scores = output.scores if output.scores is not None else [0] * len(output.txts)
        result = []
        for box, text, score in zip(output.boxes, output.txts, scores):
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            result.append(
                {
                    "text": str(text),
                    "score": float(score),
                    "x": (min(xs) + offset_x) / full_width,
                    "y": (min(ys) + offset_y) / full_height,
                    "width": (max(xs) - min(xs)) / full_width,
                    "height": (max(ys) - min(ys)) / full_height,
                }
            )
        return result


manager = Manager()
app = FastAPI(title="IOSTouch")
app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")
SCRIPTS = ROOT / "scripts"
SCRIPTS.mkdir(exist_ok=True)
RUNTIME_FILES = SCRIPTS / "data"
RUNTIME_FILES.mkdir(exist_ok=True)
SCREENSHOTS = ROOT / "screenshots"
SCREENSHOTS.mkdir(exist_ok=True)


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "pid": __import__("os").getpid(),
        "lua": importlib.util.find_spec("lupa") is not None,
        "runtime": "Lua 5.x sandbox",
    }


@app.get("/api/devices")
def devices():
    return [session.snapshot() for session in manager.refresh()]


@app.post("/api/devices/{udid}/connect")
async def connect_device(udid: str):
    session = manager.get(udid)
    try:
        await asyncio.to_thread(session.connect)
        manager.log("success", "Device connected", session.device.name)
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    return session.snapshot()


@app.post("/api/devices/{udid}/disconnect")
async def disconnect_device(udid: str):
    session = manager.get(udid)
    await asyncio.to_thread(session.disconnect)
    manager.log("info", "Device disconnected", session.device.name)
    return session.snapshot()


@app.get("/api/devices/{udid}/stream")
async def stream(udid: str):
    session = manager.get(udid)

    async def frames():
        last_id = -1
        while True:
            with session.lock:
                frame, frame_id = session.latest_frame, session.frame_id
            if frame and frame_id != last_id:
                last_id = frame_id
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            await asyncio.sleep(0.06)

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")


def require_connected(udid: str) -> DeviceSession:
    session = manager.get(udid)
    if not session.state.connected:
        raise HTTPException(409, "iPhone chưa kết nối agent")
    return session


@app.post("/api/devices/{udid}/tap")
async def tap(udid: str, body: TouchBody):
    session = require_connected(udid)
    await asyncio.to_thread(session.connection.tap, body.x, body.y)
    return {"ok": True}


@app.post("/api/devices/{udid}/swipe")
async def swipe(udid: str, body: SwipeBody):
    session = require_connected(udid)
    await asyncio.to_thread(
        session.connection.swipe, body.x1, body.y1, body.x2, body.y2, body.duration
    )
    return {"ok": True}


@app.post("/api/devices/{udid}/text")
async def input_text(udid: str, body: TextBody):
    session = require_connected(udid)
    await asyncio.to_thread(session.connection.type_text, body.text)
    return {"ok": True}


@app.post("/api/devices/{udid}/action")
async def action(udid: str, body: ActionBody):
    session = require_connected(udid)
    if body.action not in (1, 2, 3, 4, 8):
        raise HTTPException(400, "Action không hợp lệ")
    await asyncio.to_thread(session.connection.system_action, body.action)
    return {"ok": True}


def launch_app(session: DeviceSession, bundle_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,199}", bundle_id):
        raise ValueError("Bundle ID không hợp lệ")
    command = [
        sys.executable,
        "-m",
        "pymobiledevice3",
        "developer",
        "dvt",
        "launch",
        bundle_id,
        "--udid",
        session.device.udid,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or "Không mở được ứng dụng")
    message = result.stdout.strip() or f"Opened {bundle_id}"
    manager.log("success", message, session.device.name)
    return message


@app.post("/api/devices/{udid}/app/run")
async def run_app(udid: str, body: AppBody):
    session = require_connected(udid)
    try:
        message = await asyncio.to_thread(launch_app, session, body.bundle_id.strip())
    except Exception as exc:
        manager.log("error", str(exc), session.device.name)
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "message": message}


@app.post("/api/devices/{udid}/ocr")
async def ocr(udid: str):
    session = require_connected(udid)
    result = await asyncio.to_thread(manager.run_ocr, session)
    manager.log("info", f"OCR found {len(result)} text regions", session.device.name)
    return result


@app.get("/api/devices/{udid}/apps")
async def apps(udid: str):
    session = require_connected(udid)
    try:
        return await asyncio.to_thread(installed_apps, session)
    except Exception as exc:
        manager.log("error", str(exc), session.device.name)
        raise HTTPException(400, str(exc)) from exc


def safe_file(name: str) -> Path:
    clean = Path(name).name
    if clean != name or Path(clean).suffix.lower() not in (".lua", ".txt"):
        raise HTTPException(400, "Tên file không hợp lệ")
    return SCRIPTS / clean


@app.get("/api/files")
def list_files():
    rows = []
    for path in sorted(SCRIPTS.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.is_file() and path.suffix.lower() in (".lua", ".txt"):
            stat = path.stat()
            rows.append({"name": path.name, "size": stat.st_size, "modified": int(stat.st_mtime)})
    return rows


@app.get("/api/files/{name}")
def read_file(name: str):
    path = safe_file(name)
    if not path.exists():
        raise HTTPException(404, "Không tìm thấy file")
    return {"name": path.name, "content": path.read_text(encoding="utf-8-sig")}


@app.put("/api/files/{name}")
def save_file(name: str, body: FileBody):
    path = safe_file(name)
    path.write_text(body.content, encoding="utf-8")
    manager.log("success", f"Saved script {path.name}")
    return {"ok": True, "name": path.name}


@app.delete("/api/files/{name}")
def delete_file(name: str):
    path = safe_file(name)
    if path.exists():
        path.unlink()
        manager.log("warning", f"Deleted script {path.name}")
    return {"ok": True}


@app.get("/api/logs")
def logs():
    return list(manager.logs)


@app.delete("/api/logs")
def clear_logs():
    manager.logs.clear()
    manager.log("system", "Console cleared")
    return {"ok": True}


def normalize_lua_literals(raw: str) -> str:
    result: list[str] = []
    index = 0
    quote = ""
    while index < len(raw):
        char = raw[index]
        if quote:
            result.append(char)
            if char == "\\" and index + 1 < len(raw):
                index += 1
                result.append(raw[index])
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            result.append(char)
            index += 1
            continue
        if char.isalpha() or char == "_":
            end = index + 1
            while end < len(raw) and (raw[end].isalnum() or raw[end] == "_"):
                end += 1
            word = raw[index:end]
            result.append({"true": "True", "false": "False", "nil": "None"}.get(word, word))
            index = end
            continue
        result.append(char)
        index += 1
    return "".join(result)


def parse_args(raw: str) -> tuple:
    if not raw.strip():
        return ()
    region_table = re.fullmatch(
        r"\{\s*region\s*=\s*\{\s*([^{}]+?)\s*\}\s*\}", raw, flags=re.IGNORECASE
    )
    if region_table:
        values = ast.literal_eval(ast.parse(f"({region_table.group(1)},)", mode="eval").body)
        if len(values) != 4:
            raise ValueError("OCR region cần 4 giá trị")
        return ({"region": list(values)},)
    raw = normalize_lua_literals(raw)
    if "=" not in raw and ":" not in raw:
        raw = raw.replace("{", "[").replace("}", "]")
    return ast.literal_eval(ast.parse(f"({raw},)", mode="eval").body)


def safe_runtime_path(value: str) -> Path:
    relative = Path(str(value).replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts or not relative.name:
        raise ValueError("Đường dẫn file phải nằm trong scripts/data")
    if relative.suffix.lower() not in {".txt", ".json", ".csv", ".log"}:
        raise ValueError("Chỉ hỗ trợ file .txt, .json, .csv hoặc .log")
    root = RUNTIME_FILES.resolve()
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise ValueError("Đường dẫn file vượt ra ngoài scripts/data")
    return target


def runtime_text(value) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def read_runtime_file(name: str) -> str:
    path = safe_runtime_path(name)
    if not path.exists():
        raise FileNotFoundError(f"Không tìm thấy {path.name}")
    if path.stat().st_size > 1_000_000:
        raise ValueError("File vượt quá giới hạn 1 MB")
    return path.read_text(encoding="utf-8-sig")


def write_runtime_file(name: str, value, *, append: bool = False) -> Path:
    path = safe_runtime_path(name)
    text = runtime_text(value)
    if len(text.encode("utf-8")) > 1_000_000:
        raise ValueError("Dữ liệu ghi vượt quá giới hạn 1 MB")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def current_image(session: DeviceSession):
    from PIL import Image

    with session.lock:
        frame = session.latest_frame
    if not frame:
        raise ValueError("Chưa nhận được ảnh màn hình")
    return Image.open(io.BytesIO(frame)).convert("RGB")


def pixel_color(session: DeviceSession, x: float, y: float) -> int:
    image = current_image(session)
    px = max(0, min(image.width - 1, round(float(x) / 375 * image.width)))
    py = max(0, min(image.height - 1, round(float(y) / 667 * image.height)))
    red, green, blue = image.getpixel((px, py))[:3]
    return (red << 16) | (green << 8) | blue


def color_near(actual: int, expected: int, tolerance: int) -> bool:
    return all(
        abs(((actual >> shift) & 255) - ((expected >> shift) & 255)) <= tolerance
        for shift in (16, 8, 0)
    )


def find_text(session: DeviceSession, target: str, timeout: float = 0) -> dict | None:
    deadline = time.monotonic() + max(0, float(timeout))
    while True:
        found = next(
            (item for item in manager.run_ocr(session) if target.casefold() in item["text"].casefold()),
            None,
        )
        if found or time.monotonic() >= deadline:
            return found
        time.sleep(0.4)


def save_screenshot(session: DeviceSession, name: str, region=None) -> Path:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "screenshot")).strip("._")
    if not clean:
        clean = "screenshot"
    if not clean.lower().endswith(".png"):
        clean += ".png"
    image = current_image(session)
    if region is not None:
        if len(region) != 4:
            raise ValueError("Screenshot region cần x, y, width, height")
        x, y, width, height = map(float, region)
        left = round(x / 375 * image.width)
        top = round(y / 667 * image.height)
        right = round((x + width) / 375 * image.width)
        bottom = round((y + height) / 667 * image.height)
        image = image.crop((left, top, right, bottom))
    path = SCREENSHOTS / clean
    image.save(path, "PNG")
    manager.log("success", f"Screenshot saved: {path.name}", session.device.name)
    return path


def kill_app(session: DeviceSession, bundle_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,199}", bundle_id):
        raise ValueError("Bundle ID không hợp lệ")
    base = [sys.executable, "-m", "pymobiledevice3", "developer", "dvt"]
    lookup = subprocess.run(
        base + ["process-id-for-bundle-id", bundle_id, "--udid", session.device.udid],
        capture_output=True, text=True, timeout=15,
    )
    if lookup.returncode != 0 or not lookup.stdout.strip().isdigit():
        manager.log("info", f"App is not running: {bundle_id}", session.device.name)
        return
    stopped = subprocess.run(
        base + ["kill", lookup.stdout.strip(), "--udid", session.device.udid],
        capture_output=True, text=True, timeout=15,
    )
    if stopped.returncode != 0:
        raise RuntimeError((stopped.stderr or stopped.stdout).strip() or "Không tắt được app")
    manager.log("success", f"App killed: {bundle_id}", session.device.name)


def validate_bundle_id(bundle_id: str) -> str:
    bundle_id = bundle_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,199}", bundle_id):
        raise ValueError("Bundle ID không hợp lệ")
    return bundle_id


def clear_app(session: DeviceSession, bundle_id: str) -> None:
    bundle_id = validate_bundle_id(bundle_id)
    if bundle_id.lower().startswith("com.apple."):
        raise ValueError("appClear chặn ứng dụng hệ thống com.apple.* để tránh làm hỏng iOS")

    kill_app(session, bundle_id)
    errors: list[str] = []
    cleared = 0
    for remote_path in ("Documents", "Library", "tmp"):
        command = [
            sys.executable,
            "-m",
            "pymobiledevice3",
            "apps",
            "rm",
            bundle_id,
            remote_path,
            "--udid",
            session.device.udid,
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            cleared += 1
        else:
            detail = (result.stderr or result.stdout).strip().splitlines()
            errors.append(f"{remote_path}: {detail[-1] if detail else 'không truy cập được'}")
    if not cleared:
        raise RuntimeError(
            "Không thể xóa container app. App/agent hiện tại có thể không cho phép House Arrest: "
            + "; ".join(errors)
        )
    manager.log(
        "success",
        f"App data cleared: {bundle_id} ({cleared}/3 folders)",
        session.device.name,
    )


def validate_url(value: str, *, http_only: bool = False) -> str:
    value = value.strip()
    if len(value) > 4096:
        raise ValueError("URL quá dài")
    scheme = urllib.parse.urlsplit(value).scheme.lower()
    if http_only and scheme not in {"http", "https"}:
        raise ValueError("HTTP API chỉ hỗ trợ URL http:// hoặc https://")
    if not http_only and (
        not re.fullmatch(r"[a-z][a-z0-9+.-]{1,31}", scheme)
        or scheme in {"file", "javascript", "data"}
    ):
        raise ValueError("URL scheme không hợp lệ hoặc không an toàn")
    return value


def open_url(session: DeviceSession, value: str) -> None:
    value = validate_url(value)
    result = None
    if urllib.parse.urlsplit(value).scheme.lower() in {"http", "https"}:
        command = [
            sys.executable,
            "-m",
            "pymobiledevice3",
            "webinspector",
            "launch",
            value,
            "--timeout",
            "5",
            "--udid",
            session.device.udid,
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if result is None or result.returncode != 0:
        fallback = [
            sys.executable,
            "-m",
            "pymobiledevice3",
            "developer",
            "dvt",
            "launch",
            f"com.apple.mobilesafari -u {json.dumps(value)}",
            "--udid",
            session.device.udid,
        ]
        result = subprocess.run(fallback, capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            detail
            or "Không mở được URL. Hãy bật Safari > Advanced > Web Inspector và Remote Automation."
        )
    manager.log("success", f"Opened URL: {value}", session.device.name)


def get_serial_number(session: DeviceSession) -> str:
    command = [
        sys.executable,
        "-m",
        "pymobiledevice3",
        "lockdown",
        "info",
        "--udid",
        session.device.udid,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "Không đọc được Serial Number")
    try:
        serial = str(json.loads(result.stdout).get("SerialNumber", "")).strip()
    except json.JSONDecodeError as exc:
        raise RuntimeError("Lockdown trả về dữ liệu không hợp lệ") from exc
    if not serial:
        raise RuntimeError("Thiết bị không trả về Serial Number")
    return serial


def installed_apps(session: DeviceSession) -> list[dict]:
    command = [
        sys.executable,
        "-m",
        "pymobiledevice3",
        "apps",
        "list",
        "--type",
        "User",
        "--udid",
        session.device.udid,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "Không đọc được danh sách ứng dụng")
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Danh sách ứng dụng trả về dữ liệu không hợp lệ") from exc
    rows = []
    source = raw.items() if isinstance(raw, dict) else (("", item) for item in raw)
    for key, value in source:
        if not isinstance(value, dict):
            continue
        bundle_id = str(
            value.get("CFBundleIdentifier")
            or value.get("BundleIdentifier")
            or value.get("bundleIdentifier")
            or key
        ).strip()
        if not bundle_id:
            continue
        name = str(
            value.get("CFBundleDisplayName")
            or value.get("CFBundleName")
            or value.get("DisplayName")
            or bundle_id
        ).strip()
        rows.append({"name": name, "bundle_id": bundle_id})
    return sorted(rows, key=lambda item: (item["name"].lower(), item["bundle_id"].lower()))


def http_request(method: str, value: str, payload=None, headers=None, timeout: float = 15) -> tuple[int, str]:
    value = validate_url(value, http_only=True)
    if headers is None:
        headers = {}
    if not isinstance(headers, dict):
        raise ValueError("headers phải là table/dictionary")
    timeout = max(1.0, min(float(timeout), 60.0))
    clean_headers = {str(key): str(item) for key, item in headers.items()}
    data = None
    if method == "POST":
        if isinstance(payload, (dict, list, tuple)):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            clean_headers.setdefault("Content-Type", "application/json; charset=utf-8")
        elif isinstance(payload, bytes):
            data = payload
        else:
            data = str(payload if payload is not None else "").encode("utf-8")
            clean_headers.setdefault("Content-Type", "text/plain; charset=utf-8")
        if len(data) > 1_000_000:
            raise ValueError("HTTP body vượt quá 1 MB")
    request = urllib.request.Request(value, data=data, headers=clean_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(65_537)
            if len(raw) > 65_536:
                raw = raw[:65_536] + b"\n...[truncated]"
            charset = response.headers.get_content_charset() or "utf-8"
            return int(response.status), raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read(65_536)
        return int(exc.code), raw.decode("utf-8", errors="replace")


def _deny_lua_attribute_access(_obj, attr_name, _is_setting):
    raise AttributeError(f"Python attribute access is blocked in Lua: {attr_name}")


class LuaAutomationRuntime:
    """A bounded Lua runtime that exposes only the IOSTouch automation API."""

    MAX_SOURCE_BYTES = 512_000
    MAX_MEMORY_BYTES = 64 * 1024 * 1024
    MAX_INCLUDE_DEPTH = 10

    def __init__(self, session: DeviceSession):
        try:
            from lupa import LuaError, LuaRuntime
        except ImportError as exc:
            raise RuntimeError(
                "Thiếu Lua runtime. Hãy chạy lại start.cmd để cài dependency lupa."
            ) from exc

        self.session = session
        self.lua_error = LuaError
        self.include_stack: list[Path] = []
        self.lua = LuaRuntime(
            unpack_returned_tuples=True,
            register_eval=False,
            register_builtins=False,
            attribute_filter=_deny_lua_attribute_access,
            max_memory=self.MAX_MEMORY_BYTES,
        )
        self.globals = self.lua.globals()
        self._install_api()
        self.lua.execute(
            """
            local __steps = 0
            local __isCancelled = __cancelled
            debug.sethook(function()
              __steps = __steps + 1
              if __steps > 2500 then
                error("script exceeded the instruction limit", 2)
              end
              if __isCancelled() then
                error("script stopped by user", 2)
              end
            end, "", 10000)

            function waitUntil(timeout, interval, predicate)
              timeout = tonumber(timeout) or 10
              interval = tonumber(interval) or 0.25
              local deadline = monotonic() + math.max(0, timeout)
              repeat
                local ok, value = pcall(predicate)
                if ok and value then return value end
                sleep(interval)
              until monotonic() >= deadline
              return nil
            end

            function retry(times, delay, callback)
              times = math.max(1, math.floor(tonumber(times) or 1))
              delay = tonumber(delay) or 0.25
              local lastError = "retry failed"
              for attempt = 1, times do
                local ok, value = pcall(callback, attempt)
                if ok then return value end
                lastError = value
                if attempt < times then sleep(delay) end
              end
              error(lastError, 2)
            end

            function assertTrue(value, message)
              if not value then error(message or "assertTrue failed", 2) end
              return value
            end

            function assertEqual(actual, expected, message)
              if actual ~= expected then
                error(message or ("expected " .. tostring(expected) .. ", got " .. tostring(actual)), 2)
              end
              return actual
            end

            print = log
            __cancelled = nil
            python = nil
            os = nil
            io = nil
            package = nil
            require = nil
            dofile = nil
            loadfile = nil
            load = nil
            loadstring = nil
            collectgarbage = nil
            debug = nil
            """
        )

    def _install_api(self) -> None:
        api = {
            "tap": self.tap,
            "safeTap": self.tap,
            "doubleTap": self.double_tap,
            "longPress": self.long_press,
            "swipe": self.swipe,
            "inputText": self.input_text,
            "inputtext": self.input_text,
            "copyText": self.copy_text,
            "setClipboardText": self.copy_text,
            "writePasteboard": self.copy_text,
            "setClipboard": self.copy_text,
            "readFile": self.read_file,
            "writeFile": self.write_file,
            "appendFile": self.append_file,
            "jsonEncode": self.json_encode,
            "jsonDecode": self.json_decode,
            "sleep": self.sleep,
            "home": lambda: self._action(1),
            "pressHome": lambda: self._action(1),
            "lock": lambda: self._action(2),
            "rotate": lambda: self._action(3),
            "appSwitcher": lambda: self._action(4),
            "back": lambda: self._action(8),
            "log": self.log,
            "toast": self.toast,
            "appRun": self.app_run,
            "openApp": self.app_run,
            "appKill": self.app_kill,
            "appClear": self.app_clear,
            "appclear": self.app_clear,
            "openUrl": self.open_url,
            "openURL": self.open_url,
            "openurl": self.open_url,
            "getSN": self.get_sn,
            "getsn": self.get_sn,
            "httpGet": self.http_get,
            "httpget": self.http_get,
            "httpPost": self.http_post,
            "httppost": self.http_post,
            "getScreenResolution": self.get_screen_resolution,
            "getColor": self.get_color,
            "waitForColor": self.wait_for_color,
            "ocr": self.ocr,
            "findText": self.find_text,
            "waitForText": self.wait_for_text,
            "tapText": self.tap_text,
            "screenshot": self.screenshot,
            "include": self.include,
            "now": lambda: int(time.time()),
            "monotonic": time.monotonic,
            "uuid": lambda: str(uuid.uuid4()),
            "__cancelled": self.session.script_cancel.is_set,
        }
        for name, callback in api.items():
            self.globals[name] = callback

        json_api = self.lua.table()
        json_api["encode"] = self.json_encode
        json_api["decode"] = self.json_decode
        self.globals["json"] = json_api

        http_api = self.lua.table()
        http_api["get"] = self.http_get
        http_api["post"] = self.http_post
        self.globals["http"] = http_api

        self.globals["device"] = self._to_lua(
            {
                "name": self.session.device.name,
                "udid": self.session.device.udid,
                "model": self.session.device.model,
                "ios": self.session.device.ios,
            }
        )
        self.globals["SCREEN_WIDTH"] = 375
        self.globals["SCREEN_HEIGHT"] = 667

    def _check_cancelled(self) -> None:
        if self.session.script_cancel.is_set():
            raise RuntimeError("Script stopped by user")

    def _plain(self, value, depth: int = 0):
        if depth > 20:
            raise ValueError("Lua table nested too deeply")
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        module = type(value).__module__
        if module.startswith("lupa.") and hasattr(value, "items"):
            items = [(self._plain(k, depth + 1), self._plain(v, depth + 1)) for k, v in value.items()]
            integer_keys = [int(k) for k, _ in items if isinstance(k, (int, float)) and float(k).is_integer()]
            if len(integer_keys) == len(items) and sorted(integer_keys) == list(range(1, len(items) + 1)):
                by_key = {int(key): item for key, item in items}
                return [by_key[index] for index in range(1, len(items) + 1)]
            return {key: item for key, item in items}
        if isinstance(value, dict):
            return {self._plain(k, depth + 1): self._plain(v, depth + 1) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._plain(item, depth + 1) for item in value]
        return str(value)

    def _to_lua(self, value, depth: int = 0):
        if depth > 20:
            raise ValueError("Data nested too deeply")
        if isinstance(value, dict):
            table = self.lua.table()
            for key, item in value.items():
                table[key] = self._to_lua(item, depth + 1)
            return table
        if isinstance(value, (list, tuple)):
            table = self.lua.table()
            for index, item in enumerate(value, 1):
                table[index] = self._to_lua(item, depth + 1)
            return table
        return value

    def _format(self, value) -> str:
        value = self._plain(value)
        if value is None:
            return "nil"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return str(value)

    def tap(self, x, y):
        self._check_cancelled()
        self.session.connection.tap(float(x) / 375, float(y) / 667)
        return True

    def double_tap(self, x, y):
        self.tap(x, y)
        self.sleep(0.12)
        self.tap(x, y)
        return True

    def long_press(self, x, y, duration=0.8):
        self._check_cancelled()
        self.session.connection.long_press(float(x) / 375, float(y) / 667, float(duration))
        return True

    def swipe(self, x1, y1, x2, y2, duration=0.4):
        self._check_cancelled()
        self.session.connection.swipe(
            float(x1) / 375,
            float(y1) / 667,
            float(x2) / 375,
            float(y2) / 667,
            max(0.05, min(float(duration), 10.0)),
        )
        return True

    def input_text(self, value):
        self._check_cancelled()
        self.session.connection.type_text(str(value))
        return True

    def copy_text(self, value):
        self._check_cancelled()
        self.session.connection.copy_text(str(value))
        return True

    def read_file(self, name):
        content = read_runtime_file(str(name))
        manager.log("script", f"readFile({name}): {content[:65_536]}", self.session.device.name)
        return content

    def _write_file(self, name, value, *, append: bool):
        path = write_runtime_file(str(name), self._plain(value), append=append)
        relative = path.relative_to(RUNTIME_FILES).as_posix()
        manager.log("success", f"File saved: scripts/data/{relative}", self.session.device.name)
        return relative

    def write_file(self, name, value):
        return self._write_file(name, value, append=False)

    def append_file(self, name, value):
        return self._write_file(name, value, append=True)

    def json_encode(self, value):
        encoded = json.dumps(self._plain(value), ensure_ascii=False, separators=(",", ":"))
        manager.log("script", f"JSON: {encoded}", self.session.device.name)
        return encoded

    def json_decode(self, value):
        decoded = json.loads(str(value))
        return self._to_lua(decoded)

    def sleep(self, seconds):
        remaining = max(0.0, min(float(seconds), 300.0))
        while remaining > 0:
            self._check_cancelled()
            chunk = min(0.05, remaining)
            time.sleep(chunk)
            remaining -= chunk
        return True

    def _action(self, action: int):
        self._check_cancelled()
        self.session.connection.system_action(action)
        return True

    def log(self, *values):
        manager.log("script", " ".join(self._format(value) for value in values), self.session.device.name)
        return True

    def toast(self, *values):
        manager.log("toast", " ".join(self._format(value) for value in values), self.session.device.name)
        return True

    def app_run(self, bundle_id):
        return launch_app(self.session, str(bundle_id))

    def app_kill(self, bundle_id):
        kill_app(self.session, str(bundle_id))
        return True

    def app_clear(self, bundle_id):
        clear_app(self.session, str(bundle_id))
        return True

    def open_url(self, value):
        open_url(self.session, str(value))
        return True

    def get_sn(self):
        serial = get_serial_number(self.session)
        manager.log("script", f"Serial Number: {serial}", self.session.device.name)
        return serial

    def http_get(self, value, headers=None, timeout=15):
        if isinstance(headers, (int, float)):
            timeout, headers = headers, None
        status, response = http_request(
            "GET", str(value), headers=self._plain(headers) if headers is not None else None, timeout=timeout
        )
        manager.log("script", f"HTTP GET {status}: {response}", self.session.device.name)
        return status, response

    def http_post(self, value, payload="", headers=None, timeout=15):
        if isinstance(headers, (int, float)):
            timeout, headers = headers, None
        status, response = http_request(
            "POST",
            str(value),
            self._plain(payload),
            self._plain(headers) if headers is not None else None,
            timeout,
        )
        manager.log("script", f"HTTP POST {status}: {response}", self.session.device.name)
        return status, response

    def get_screen_resolution(self):
        result = {"width": 375, "height": 667}
        manager.log("script", "Screen: 375x667 points", self.session.device.name)
        return self._to_lua(result)

    def get_color(self, x, y):
        color = pixel_color(self.session, float(x), float(y))
        manager.log("script", f"Color: 0x{color:06X}", self.session.device.name)
        return color

    def wait_for_color(self, x, y, expected, timeout=10, tolerance=12):
        deadline = time.monotonic() + max(0, float(timeout))
        expected = int(expected)
        while True:
            self._check_cancelled()
            actual = pixel_color(self.session, float(x), float(y))
            if color_near(actual, expected, int(tolerance)):
                manager.log("script", f"Color matched: 0x{actual:06X}", self.session.device.name)
                return actual
            if time.monotonic() >= deadline:
                raise TimeoutError(f"waitForColor timeout; last=0x{actual:06X}")
            self.sleep(0.2)

    def ocr(self, options=None):
        region = None
        if options is not None:
            options = self._plain(options)
            if isinstance(options, dict):
                region = options.get("region")
            elif isinstance(options, list):
                region = options
            else:
                raise ValueError("ocr nhận {region={x,y,w,h}} hoặc {x,y,w,h}")
        found = manager.run_ocr(self.session, region)
        manager.log("script", f"OCR: {len(found)} regions", self.session.device.name)
        return self._to_lua(found)

    def _find_text(self, target, timeout=0):
        deadline = time.monotonic() + max(0, float(timeout))
        while True:
            self._check_cancelled()
            found = next(
                (
                    item
                    for item in manager.run_ocr(self.session)
                    if str(target).casefold() in item["text"].casefold()
                ),
                None,
            )
            if found or time.monotonic() >= deadline:
                return found
            self.sleep(0.35)

    def find_text(self, target, timeout=0):
        found = self._find_text(target, timeout)
        if not found:
            return None
        return self._to_lua(found)

    def wait_for_text(self, target, timeout=10):
        found = self._find_text(target, timeout)
        if not found:
            raise TimeoutError(f"Text not found: {target}")
        return self._to_lua(found)

    def tap_text(self, target, timeout=10):
        found = self._find_text(target, timeout)
        if not found:
            raise TimeoutError(f"Text not found: {target}")
        x = found["x"] + found["width"] / 2
        y = found["y"] + found["height"] / 2
        self.session.connection.tap(x, y)
        return self._to_lua(found)

    def screenshot(self, *args):
        name = str(args[0]) if args else time.strftime("screenshot_%Y%m%d_%H%M%S")
        region = None
        if len(args) == 2:
            region = self._plain(args[1])
        elif len(args) == 5:
            region = [self._plain(value) for value in args[1:5]]
        elif len(args) not in (0, 1):
            raise ValueError("screenshot(name), screenshot(name, region) hoặc screenshot(name,x,y,w,h)")
        return str(save_screenshot(self.session, name, region))

    def include(self, name):
        relative = Path(str(name).replace("\\", "/"))
        if relative.suffix.lower() != ".lua" or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("include chỉ nhận file .lua bên trong thư mục scripts")
        root = SCRIPTS.resolve()
        path = (root / relative).resolve()
        if root not in path.parents or not path.is_file():
            raise FileNotFoundError(f"Không tìm thấy script: {relative}")
        if path in self.include_stack:
            raise RuntimeError(f"Circular include: {relative}")
        if len(self.include_stack) >= self.MAX_INCLUDE_DEPTH:
            raise RuntimeError("include nested too deeply")
        if path.stat().st_size > self.MAX_SOURCE_BYTES:
            raise ValueError("Included script exceeds 512 KB")
        self.include_stack.append(path)
        try:
            return self.lua.execute(path.read_text(encoding="utf-8-sig"))
        finally:
            self.include_stack.pop()

    def run(self, code: str) -> None:
        if len(code.encode("utf-8")) > self.MAX_SOURCE_BYTES:
            raise ValueError("Script exceeds 512 KB")
        self.lua.execute(code)


def run_legacy_script(session: DeviceSession, code: str) -> None:
    manager.log("info", "Script started", session.device.name)
    for line_number, source in enumerate(code.splitlines(), 1):
        line = source.split("--", 1)[0].strip().rstrip(";")
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z_]\w*)\s*\((.*)\)", line)
        if not match:
            raise ValueError(f"Line {line_number}: unsupported Lua statement")
        name, raw = match.groups()
        args = parse_args(raw)
        if name in ("tap", "safeTap") and len(args) == 2:
            session.connection.tap(float(args[0]) / 375, float(args[1]) / 667)
        elif name == "doubleTap" and len(args) == 2:
            session.connection.tap(float(args[0]) / 375, float(args[1]) / 667)
            time.sleep(0.12)
            session.connection.tap(float(args[0]) / 375, float(args[1]) / 667)
        elif name == "longPress" and len(args) in (2, 3):
            duration = float(args[2]) if len(args) == 3 else 0.8
            session.connection.long_press(float(args[0]) / 375, float(args[1]) / 667, duration)
        elif name == "swipe" and len(args) in (4, 5):
            duration = float(args[4]) if len(args) == 5 else .4
            session.connection.swipe(
                float(args[0]) / 375, float(args[1]) / 667,
                float(args[2]) / 375, float(args[3]) / 667, duration,
            )
        elif name in ("inputText", "inputtext") and len(args) == 1:
            session.connection.type_text(str(args[0]))
        elif name in ("copyText", "setClipboardText", "writePasteboard", "setClipboard") and len(args) == 1:
            session.connection.copy_text(str(args[0]))
        elif name == "readFile" and len(args) == 1:
            content = read_runtime_file(str(args[0]))
            shown = content if len(content) <= 65_536 else content[:65_536] + "\n...[truncated]"
            manager.log("script", f"readFile({args[0]}): {shown}", session.device.name)
        elif name in ("writeFile", "appendFile") and len(args) == 2:
            path = write_runtime_file(str(args[0]), args[1], append=name == "appendFile")
            relative = path.relative_to(RUNTIME_FILES).as_posix()
            manager.log("success", f"{name}: scripts/data/{relative}", session.device.name)
        elif name == "jsonEncode" and len(args) == 1:
            encoded = json.dumps(args[0], ensure_ascii=False, separators=(",", ":"))
            manager.log("script", f"JSON: {encoded}", session.device.name)
        elif name == "jsonDecode" and len(args) == 1:
            decoded = json.loads(str(args[0]))
            manager.log("script", f"JSON: {json.dumps(decoded, ensure_ascii=False)}", session.device.name)
        elif name == "sleep" and len(args) == 1:
            time.sleep(max(0, min(float(args[0]), 60)))
        elif name in ("home", "pressHome", "back", "lock", "appSwitcher") and not args:
            session.connection.system_action({"home": 1, "pressHome": 1, "lock": 2, "appSwitcher": 4, "back": 8}[name])
        elif name == "log":
            manager.log("script", " ".join(map(str, args)), session.device.name)
        elif name == "toast":
            manager.log("toast", " ".join(map(str, args)), session.device.name)
        elif name in ("appRun", "openApp") and len(args) == 1:
            launch_app(session, str(args[0]))
        elif name == "appKill" and len(args) == 1:
            kill_app(session, str(args[0]))
        elif name in ("appClear", "appclear") and len(args) == 1:
            clear_app(session, str(args[0]))
        elif name in ("openUrl", "openURL", "openurl") and len(args) == 1:
            open_url(session, str(args[0]))
        elif name in ("getSN", "getsn") and not args:
            serial = get_serial_number(session)
            manager.log("script", f"Serial Number: {serial}", session.device.name)
        elif name in ("httpGet", "httpget") and len(args) in (1, 2, 3):
            headers, timeout = None, 15
            if len(args) == 2:
                if isinstance(args[1], (int, float)):
                    timeout = args[1]
                else:
                    headers = args[1]
            elif len(args) == 3:
                headers, timeout = args[1], args[2]
            status, response = http_request("GET", str(args[0]), headers=headers, timeout=timeout)
            manager.log("script", f"HTTP GET {status}: {response}", session.device.name)
        elif name in ("httpPost", "httppost") and len(args) in (1, 2, 3, 4):
            payload = args[1] if len(args) >= 2 else ""
            headers = args[2] if len(args) == 3 else None
            timeout = 15
            if len(args) == 4:
                headers, timeout = args[2], args[3]
            status, response = http_request("POST", str(args[0]), payload, headers, timeout)
            manager.log("script", f"HTTP POST {status}: {response}", session.device.name)
        elif name == "getScreenResolution" and not args:
            manager.log("script", "Screen: 375x667 points", session.device.name)
        elif name == "getColor" and len(args) == 2:
            color = pixel_color(session, float(args[0]), float(args[1]))
            manager.log("script", f"Color: 0x{color:06X}", session.device.name)
        elif name == "waitForColor" and len(args) in (3, 4, 5):
            x, y, expected = float(args[0]), float(args[1]), int(args[2])
            timeout = float(args[3]) if len(args) >= 4 else 10.0
            tolerance = int(args[4]) if len(args) == 5 else 12
            deadline = time.monotonic() + max(0, timeout)
            while True:
                actual = pixel_color(session, x, y)
                if color_near(actual, expected, tolerance):
                    manager.log("script", f"Color matched: 0x{actual:06X}", session.device.name)
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"waitForColor timeout; last=0x{actual:06X}")
                time.sleep(0.2)
        elif name == "ocr" and len(args) in (0, 1):
            region = None
            if args:
                options = args[0]
                if isinstance(options, dict):
                    region = options.get("region")
                elif isinstance(options, (list, tuple)):
                    region = options
                else:
                    raise ValueError("ocr nhận {region={x,y,w,h}} hoặc {x,y,w,h}")
            found = manager.run_ocr(session, region)
            manager.log("script", f"OCR: {len(found)} regions", session.device.name)
        elif name in ("findText", "waitForText", "tapText") and len(args) in (1, 2):
            timeout = float(args[1]) if len(args) == 2 else (10.0 if name == "waitForText" else 0.0)
            found = find_text(session, str(args[0]), timeout)
            if not found:
                if name == "findText":
                    manager.log("script", f"Text not found: {args[0]}", session.device.name)
                else:
                    raise TimeoutError(f"Text not found: {args[0]}")
            else:
                x = found["x"] + found["width"] / 2
                y = found["y"] + found["height"] / 2
                manager.log("script", f"Text found: {found['text']} @ ({x*375:.0f},{y*667:.0f})", session.device.name)
                if name == "tapText":
                    session.connection.tap(x, y)
        elif name == "screenshot" and len(args) in (0, 1, 2, 5):
            screenshot_name = str(args[0]) if args else time.strftime("screenshot_%Y%m%d_%H%M%S")
            region = None
            if len(args) == 2:
                region = args[1]
            elif len(args) == 5:
                region = args[1:5]
            save_screenshot(session, screenshot_name, region)
        else:
            raise ValueError(f"Line {line_number}: unsupported call {name}")
    manager.log("success", "Script completed", session.device.name)


def run_script(session: DeviceSession, code: str) -> None:
    if not session.script_lock.acquire(blocking=False):
        raise RuntimeError("Một script khác đang chạy trên thiết bị này")
    session.script_cancel.clear()
    runtime: LuaAutomationRuntime | None = None
    manager.log("info", "Lua script started", session.device.name)
    try:
        runtime = LuaAutomationRuntime(session)
        runtime.run(code)
        manager.log("success", "Lua script completed", session.device.name)
    except Exception as exc:
        if runtime is not None and isinstance(exc, runtime.lua_error):
            message = str(exc).replace("[string \"<python>\"]", "script")
            raise ValueError(f"Lua error: {message}") from exc
        raise
    finally:
        session.script_cancel.clear()
        session.script_lock.release()


@app.post("/api/devices/{udid}/script")
async def execute_script(udid: str, body: ScriptBody):
    session = require_connected(udid)
    try:
        await asyncio.to_thread(run_script, session, body.code)
    except Exception as exc:
        manager.log("error", str(exc), session.device.name)
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.post("/api/devices/{udid}/script/stop")
def stop_script(udid: str):
    session = manager.get(udid)
    running = session.script_lock.locked()
    session.script_cancel.set()
    if running:
        manager.log("warning", "Stopping Lua script...", session.device.name)
    return {"ok": True, "running": running}


@app.on_event("shutdown")
def shutdown():
    for session in manager.sessions.values():
        session.disconnect()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
