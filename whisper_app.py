import atexit
import ctypes
import io
import json
import logging
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import comtypes
import httpx
import keyring
import numpy as np
import pyperclip
import sounddevice as sd
import soundfile as sf
import winsound
from pynput import keyboard, mouse
from pycaw.pycaw import AudioUtilities
from ui import MainWindow, TrayIcon

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "app.log"
HOTKEYS_PATH = ROOT / "hotkeys.json"
OVERLAY_POSITION_PATH = ROOT / "overlay.json"
AUDIO_CONFIG_PATH = ROOT / "audio.json"
SHOW_FLAG_PATH = ROOT / "show.flag"
SERVICE = "whisper-local"
USERNAME = "deepseek_api_key"
ASR_URL = "https://api.deepseek.com/v1/tools/asr"
CHAT_URL = "https://api.deepseek.com/chat/completions"
CHUNK_SECONDS = 60
SAMPLE_RATE = 16000
BEEP_ENABLED = True
BEEP_OUTPUT_DEVICE = None

SYSTEM_PROMPT = """You are a speech-to-text correction engine.
Current local date and time: {now}

Your job is to correct the raw transcript using the meaning of the whole sentence,
not just the sound of individual words.

Rules:
1. Return only the corrected text. No explanations or quotes.
2. Output only English or Hinglish. Hinglish must be written in Latin letters.
   Never output Devanagari or any non-Latin script. If the transcript is in
   Hindi or Devanagari, transliterate it into Latin Hinglish.
3. Preserve the speaker's language mix and meaning. Do not translate Hinglish
   into pure English.
4. If the speaker clearly dictates steps or a list using words like first,
   second, then, next, pehle, phir, uske baad, aur phir, or enumerates items,
   format it as a clean numbered list. Keep normal prose otherwise.
   Example: "pehle X karo, phir Y karo" -> "1. X karo\n2. Y karo".
5. If a word or phrase does not make sense in the sentence, replace it with the
   most likely intended words. Use grammar and context, not sound alone.
6. Fix common ASR confusions when the literal version is incoherent. Examples:
   - "Fiverr limit" in a duration/reset context -> "five-hour limit"
   - "3:09" when the current time is 9:03 and the sentence is about the current time -> "9:03"
   - "mike" when talking about a microphone -> "mic"
   - the abbreviation "AI" must not become "I"
7. Use the current local time above to correct obviously wrong clock times.
   If both readings are plausible, keep the original.
8. Prefer ordinary phrases over brand names unless the sentence clearly refers to
   that brand. Do not invent brands, names, facts, or new content.
9. Preserve names, numbers, commands, code, file paths, and technical terms when
   they are coherent. Fix their capitalization when obvious.
10. Make minimal grammar, punctuation, and filler-word corrections. Do not rewrite
    or restructure sentences unnecessarily.
11. If the entire transcript is unintelligible, return an empty string.
"""

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
if sys.stderr is not None:
    sys.stderr = open(LOG_PATH, "a", encoding="utf-8", buffering=1)


def beep(frequency: int, duration: int = 60) -> None:
    if not BEEP_ENABLED:
        return
    try:
        if BEEP_OUTPUT_DEVICE is not None:
            rate = 44100
            samples = max(1, int(rate * duration / 1000))
            t = np.linspace(0, duration / 1000, samples, endpoint=False)
            tone = 0.18 * np.sin(2 * np.pi * frequency * t)
            sd.play(tone, rate, device=BEEP_OUTPUT_DEVICE)
            return
    except Exception:
        logging.exception("Could not play beep on selected speaker")
    try:
        winsound.Beep(frequency, duration)
    except Exception:
        pass


def get_work_area() -> wintypes.RECT:
    rect = wintypes.RECT()
    try:
        if ctypes.windll.user32.SystemParametersInfoW(
            0x0030, 0, ctypes.byref(rect), 0
        ):
            return rect
    except Exception:
        logging.exception("Could not read work area")
    return wintypes.RECT(
        0,
        0,
        ctypes.windll.user32.GetSystemMetrics(0),
        ctypes.windll.user32.GetSystemMetrics(1),
    )


def get_foreground_window() -> int:
    try:
        return int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return 0


def set_foreground_window(hwnd: int) -> None:
    if not hwnd:
        return
    try:
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        logging.exception("SetForegroundWindow failed")


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def has_text_caret(hwnd: int) -> bool:
    if not hwnd:
        return False
    try:
        pid = wintypes.DWORD()
        thread_id = ctypes.windll.user32.GetWindowThreadProcessId(
            hwnd, ctypes.byref(pid)
        )
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if not ctypes.windll.user32.GetGUIThreadInfo(
            thread_id, ctypes.byref(info)
        ):
            return False
        return bool(info.hwndCaret)
    except Exception:
        logging.exception("Could not inspect text caret")
        return False


def window_class_name(hwnd: int) -> str:
    try:
        buffer = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, buffer, 256)
        return buffer.value
    except Exception:
        return ""


def is_probable_text_target(hwnd: int) -> bool:
    if has_text_caret(hwnd):
        return True
    class_name = window_class_name(hwnd)
    return class_name in {
        "Chrome_WidgetWin_1",
        "MozillaWindowClass",
        "ApplicationFrameWindow",
        "Notepad",
        "VSCode",
    }


def same_binding(a: dict | None, b: dict | None) -> bool:
    return bool(
        a
        and b
        and a.get("type") == b.get("type")
        and str(a.get("value", "")).lower() == str(b.get("value", "")).lower()
    )


def load_hotkeys() -> dict:
    defaults = {
        "toggle": {"type": "keyboard", "value": "f8"},
        "hold": {"type": "keyboard", "value": "f9"},
    }
    try:
        data = json.loads(HOTKEYS_PATH.read_text(encoding="utf-8"))
        for name in ("toggle", "hold"):
            if name not in data or "type" not in data[name] or "value" not in data[name]:
                data[name] = defaults[name]
        return data
    except Exception:
        logging.exception("Could not read hotkeys.json; using defaults")
        return defaults


def normalize_key(key) -> str | None:
    value = getattr(key, "char", None)
    if value is not None:
        return str(value).lower()
    value = getattr(key, "name", None)
    if value is not None:
        return str(value).lower()
    return None


def pick_input() -> int | None:
    for index, device in enumerate(sd.query_devices()):
        name = device["name"].lower()
        if device["max_input_channels"] > 0 and (
            "earpods" in name or "headset" in name
        ):
            return index
    default = sd.default.device
    if default and default[0] is not None and int(default[0]) >= 0:
        return int(default[0])
    return None


def default_audio_config() -> dict:
    default = sd.default.device
    output = None
    if default and default[1] is not None and int(default[1]) >= 0:
        output = int(default[1])
    return {"input_device": pick_input(), "output_device": output}


def load_audio_config() -> dict:
    config = default_audio_config()
    try:
        saved = json.loads(AUDIO_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(saved.get("input_device"), int):
            config["input_device"] = saved["input_device"]
        if isinstance(saved.get("output_device"), int):
            config["output_device"] = saved["output_device"]
    except Exception:
        pass
    return config


class Overlay:
    IDLE_W = 60
    IDLE_H = 18
    ACTIVE_W = 112
    ACTIVE_H = 28

    def __init__(self, on_confirm=None, on_cancel=None, on_show=None) -> None:
        self.on_confirm = on_confirm
        self.on_cancel = on_cancel
        self.on_show = on_show
        self.state = "idle"
        self.orientation = "horizontal"
        self.edge = None
        self.current_w = self.IDLE_W
        self.current_h = self.IDLE_H
        self.dragging = False
        self.drag_offset = (0, 0)
        self.bars = []
        self.bar_positions = []
        self.animating = False

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.92)
        self.root.configure(bg="#080808")

        self.canvas = tk.Canvas(
            self.root,
            width=self.IDLE_W,
            height=self.IDLE_H,
            bg="#080808",
            highlightthickness=0,
            bd=0,
            cursor="fleur",
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Double-Button-1>", self.on_double_click)

        work = get_work_area()
        x = work.right - self.IDLE_W - 16
        y = work.bottom - self.IDLE_H - 16
        self.edge = "right"
        self.orientation = "vertical"
        self.current_w, self.current_h = self.IDLE_H, self.IDLE_W
        try:
            saved = json.loads(OVERLAY_POSITION_PATH.read_text(encoding="utf-8"))
            x = int(saved["x"])
            y = int(saved["y"])
            self.edge = saved.get("edge", "right")
            self.orientation = saved.get("orientation", "vertical")
            if self.edge in ("left", "right"):
                self.orientation = "vertical"
            else:
                self.orientation = "horizontal"
        except Exception:
            pass
        if self.orientation == "vertical":
            self.current_w, self.current_h = self.IDLE_H, self.IDLE_W
        else:
            self.current_w, self.current_h = self.IDLE_W, self.IDLE_H
        x = max(work.left, min(work.right - self.current_w, x))
        y = max(work.top, min(work.bottom - self.current_h, y))
        if self.edge == "right":
            x = work.right - self.current_w
        elif self.edge == "left":
            x = work.left
        elif self.edge == "bottom":
            y = work.bottom - self.current_h
        elif self.edge == "top":
            y = work.top
        self.root.geometry(
            f"{self.current_w}x{self.current_h}+{x}+{y}"
        )

        self.state_queue: queue.Queue[str] = queue.Queue()
        self.root.after(50, self.apply_no_activate)
        self.root.after(60, self.poll_state)
        self.root.after(3000, self.keep_on_top_loop)

    def apply_no_activate(self) -> None:
        try:
            hwnd = self.root.winfo_id()
            parent = ctypes.windll.user32.GetParent(hwnd)
            if parent:
                hwnd = parent
            ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, -20, ex_style | 0x08000000 | 0x00000080
            )
            ctypes.windll.user32.SetWindowPos(
                hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010
            )
        except Exception:
            logging.exception("Overlay no-activate setup failed")

    def keep_on_top(self) -> None:
        try:
            hwnd = self.root.winfo_id()
            parent = ctypes.windll.user32.GetParent(hwnd)
            if parent:
                hwnd = parent
            ctypes.windll.user32.SetWindowPos(
                hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010
            )
        except Exception:
            logging.exception("Could not keep overlay on top")

    def keep_on_top_loop(self) -> None:
        self.keep_on_top()
        self.root.after(3000, self.keep_on_top_loop)

    def set_state(self, state: str) -> None:
        self.state_queue.put(state)

    def poll_state(self) -> None:
        try:
            while True:
                state = self.state_queue.get_nowait()
                self.state = state
                if state == "error":
                    self.root.after(1800, lambda: self.set_state("idle"))
                self.draw()
        except queue.Empty:
            pass
        self.root.after(60, self.poll_state)

    def resize_keep_center(self, width: int, height: int) -> None:
        cx = self.root.winfo_x() + self.current_w // 2
        cy = self.root.winfo_y() + self.current_h // 2
        work = get_work_area()
        x = max(work.left, min(work.right - width, cx - width // 2))
        y = max(work.top, min(work.bottom - height, cy - height // 2))
        if self.edge == "left":
            x = work.left
        elif self.edge == "right":
            x = work.right - width
        elif self.edge == "top":
            y = work.top
        elif self.edge == "bottom":
            y = work.bottom - height
        self.current_w = width
        self.current_h = height
        self.canvas.config(width=width, height=height)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def rounded_rect(self, x1, y1, x2, y2, radius, fill, outline):
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        self.canvas.create_polygon(
            points,
            smooth=True,
            splinesteps=24,
            fill=fill,
            outline=outline,
            width=1,
        )

    def draw(self) -> None:
        self.canvas.delete("all")
        self.bars = []
        self.bar_positions = []
        vertical = self.orientation == "vertical"

        if self.state == "idle":
            width, height = (self.IDLE_H, self.IDLE_W) if vertical else (self.IDLE_W, self.IDLE_H)
            self.resize_keep_center(width, height)
            self.rounded_rect(1, 1, width - 1, height - 1, 9, "#101010", "#5a5a5a")
            return

        if self.state == "error":
            width, height = (22, 90) if vertical else (90, 22)
            self.resize_keep_center(width, height)
            self.rounded_rect(1, 1, width - 1, height - 1, 11, "#1a0d0d", "#ef4444")
            cx, cy = width / 2, height / 2
            self.canvas.create_line(cx - 5, cy, cx + 5, cy, fill="#ef4444", width=2)
            self.canvas.create_line(cx, cy - 5, cx, cy + 5, fill="#ef4444", width=2)
            return

        width, height = (self.ACTIVE_H, self.ACTIVE_W) if vertical else (self.ACTIVE_W, self.ACTIVE_H)
        self.resize_keep_center(width, height)
        self.rounded_rect(1, 1, width - 1, height - 1, 14, "#1b1b1b", "#5a5a5a")

        if self.state == "listening":
            if vertical:
                self.draw_cancel_button(width / 2, 16)
                self.draw_confirm_button(width / 2, height - 16)
            else:
                self.draw_cancel_button(16, height / 2)
                self.draw_confirm_button(width - 16, height / 2)
            bar_color = "#f4f4f4"
        else:
            bar_color = "#f59e0b"

        count = 8
        if vertical:
            start = 32
            end = height - 32
            gap = (end - start) / (count - 1)
            for index in range(count):
                y = start + index * gap
                self.bar_positions.append((y, "vertical"))
                self.bars.append(
                    self.canvas.create_line(
                        width / 2 - 3,
                        y,
                        width / 2 + 3,
                        y,
                        fill=bar_color,
                        width=2,
                        capstyle="round",
                    )
                )
        else:
            start = 32
            end = width - 32
            gap = (end - start) / (count - 1)
            for index in range(count):
                x = start + index * gap
                self.bar_positions.append((x, "horizontal"))
                self.bars.append(
                    self.canvas.create_line(
                        x,
                        height / 2 - 3,
                        x,
                        height / 2 + 3,
                        fill=bar_color,
                        width=2,
                        capstyle="round",
                    )
                )

        if not self.animating:
            self.animating = True
            self.animate()

    def draw_cancel_button(self, cx: float, cy: float) -> None:
        self.canvas.create_oval(
            cx - 9, cy - 9, cx + 9, cy + 9,
            fill="#3a3a3a", outline=""
        )
        self.canvas.create_line(cx - 3, cy - 3, cx + 3, cy + 3, fill="#ffffff", width=2)
        self.canvas.create_line(cx - 3, cy + 3, cx + 3, cy - 3, fill="#ffffff", width=2)

    def draw_confirm_button(self, cx: float, cy: float) -> None:
        self.canvas.create_oval(
            cx - 10, cy - 10, cx + 10, cy + 10,
            fill="#f5f5f5", outline=""
        )
        self.canvas.create_line(
            cx - 4, cy, cx - 1, cy + 3, cx + 5, cy - 4,
            fill="#111111", width=2
        )

    def animate(self) -> None:
        if self.state not in ("listening", "processing"):
            self.animating = False
            return
        phase = time.time() * 7.0
        for index, item in enumerate(self.bars):
            amplitude = 3 + abs(math.sin(phase + index * 0.55)) * 8
            position, direction = self.bar_positions[index]
            if direction == "horizontal":
                self.canvas.coords(
                    item,
                    position,
                    self.current_h / 2 - amplitude / 2,
                    position,
                    self.current_h / 2 + amplitude / 2,
                )
            else:
                self.canvas.coords(
                    item,
                    self.current_w / 2 - amplitude / 2,
                    position,
                    self.current_w / 2 + amplitude / 2,
                    position,
                )
        self.root.after(80, self.animate)

    def hit_button(self, event) -> bool:
        if self.state != "listening":
            return False
        if self.orientation == "vertical":
            cancel_x, cancel_y = self.current_w / 2, 16
            confirm_x, confirm_y = self.current_w / 2, self.current_h - 16
        else:
            cancel_x, cancel_y = 16, self.current_h / 2
            confirm_x, confirm_y = self.current_w - 16, self.current_h / 2
        if (event.x - cancel_x) ** 2 + (event.y - cancel_y) ** 2 <= 12 ** 2:
            if self.on_cancel:
                self.on_cancel()
            return True
        if (event.x - confirm_x) ** 2 + (event.y - confirm_y) ** 2 <= 12 ** 2:
            if self.on_confirm:
                self.on_confirm()
            return True
        return False

    def on_press(self, event) -> None:
        if self.hit_button(event):
            self.dragging = False
            return
        self.dragging = True
        self.drag_start = (
            self.edge,
            self.orientation,
            self.root.winfo_x(),
            self.root.winfo_y(),
        )
        self.drag_offset = (
            event.x_root - self.root.winfo_x(),
            event.y_root - self.root.winfo_y(),
        )

    def on_motion(self, event) -> None:
        if not self.dragging:
            return
        work = get_work_area()
        x = event.x_root - self.drag_offset[0]
        y = event.y_root - self.drag_offset[1]
        x = max(work.left, min(work.right - self.current_w, x))
        y = max(work.top, min(work.bottom - self.current_h, y))
        self.root.geometry(f"+{x}+{y}")

    def on_release(self, event) -> None:
        if not self.dragging:
            return
        self.dragging = False
        work = get_work_area()
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        width = self.current_w
        height = self.current_h
        distances = {
            "left": x - work.left,
            "right": work.right - (x + width),
            "top": y - work.top,
            "bottom": work.bottom - (y + height),
        }
        edge, distance = min(distances.items(), key=lambda item: item[1])
        if distance <= 60:
            self.edge = edge
            if edge in ("left", "right"):
                self.orientation = "vertical"
            else:
                self.orientation = "horizontal"
            self.draw()
            width = self.current_w
            height = self.current_h
            if edge == "left":
                x = work.left
            elif edge == "right":
                x = work.right - width
            elif edge == "top":
                y = work.top
            elif edge == "bottom":
                y = work.bottom - height
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        else:
            old_edge, old_orientation, old_x, old_y = self.drag_start
            self.edge = old_edge or "right"
            self.orientation = old_orientation
            self.draw()
            width = self.current_w
            height = self.current_h
            x = max(work.left, min(work.right - width, old_x))
            y = max(work.top, min(work.bottom - height, old_y))
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.keep_on_top()
        try:
            OVERLAY_POSITION_PATH.write_text(
                json.dumps(
                    {
                        "x": x,
                        "y": y,
                        "orientation": self.orientation,
                        "edge": self.edge,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            logging.exception("Could not save overlay position")

    def on_double_click(self, event) -> None:
        if self.on_show:
            self.on_show()


class SystemAudio:
    def __init__(self) -> None:
        self.active = False
        self.was_muted = False
        self.lock = threading.Lock()

    def _volume(self):
        comtypes.CoInitialize()
        return AudioUtilities.GetSpeakers().EndpointVolume

    def mute(self) -> None:
        with self.lock:
            if self.active:
                return
            try:
                volume = self._volume()
                self.was_muted = bool(volume.GetMute())
                volume.SetMute(1, None)
                self.active = True
                logging.info("System audio muted")
            except Exception:
                logging.exception("Could not mute system audio")

    def restore(self) -> None:
        with self.lock:
            if not self.active:
                return
            try:
                volume = self._volume()
                volume.SetMute(1 if self.was_muted else 0, None)
                self.active = False
                logging.info("System audio restored")
            except Exception:
                logging.exception("Could not restore system audio")


class Recorder:
    def __init__(self) -> None:
        self.stream = None
        self.frames: list[np.ndarray] = []
        self.hwnd = 0
        self.lock = threading.Lock()
        self.audio = SystemAudio()

    def start(self, device: int | None = None) -> None:
        with self.lock:
            if self.stream is not None:
                return
            self.frames = []
            self.hwnd = get_foreground_window()
            selected_device = device if device is not None else pick_input()
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                callback=self._callback,
                device=selected_device,
            )
            self.stream.start()
            self.audio.mute()
            logging.info("Recording started")

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            logging.warning("Audio status: %s", status)
        with self.lock:
            self.frames.append(indata.copy())

    def stop(self) -> tuple[np.ndarray, int] | None:
        with self.lock:
            if self.stream is None:
                self.audio.restore()
                return None
            self.stream.stop()
            self.stream.close()
            self.stream = None
            frames = self.frames
            self.frames = []
            hwnd = self.hwnd
        self.audio.restore()
        if not frames:
            return None
        audio = np.concatenate(frames, axis=0).reshape(-1)
        logging.info("Recording stopped: %.2fs", len(audio) / SAMPLE_RATE)
        return audio, hwnd


class WhisperFlow:
    def __init__(self) -> None:
        self.hotkeys = load_hotkeys()
        self.audio_config = load_audio_config()
        global BEEP_OUTPUT_DEVICE
        BEEP_OUTPUT_DEVICE = self.audio_config.get("output_device")
        self.recorder = Recorder()
        self.mode: str | None = None
        self.pressed: set[str] = set()
        self.processing = False
        self.lock = threading.Lock()
        self.main_window = None
        self.tray = None
        self.last_target_hwnd = 0
        self.capture_active = False
        self.api_key = keyring.get_password(SERVICE, USERNAME)
        if not self.api_key:
            logging.warning("DeepSeek API key missing; set it from Settings")
        self.overlay = Overlay(
            on_confirm=self.stop_recording,
            on_cancel=self.cancel_recording,
            on_show=self.show_main,
        )
        atexit.register(self.recorder.audio.restore)
        self.keyboard_listener = keyboard.Listener(
            on_press=self.on_key_press,
            on_release=self.on_key_release,
        )
        self.mouse_listener = mouse.Listener(on_click=self.on_mouse_click)

    def matches(self, binding: dict, kind: str, value: str) -> bool:
        return (
            binding.get("type") == kind
            and str(binding.get("value", "")).lower() == value.lower()
        )

    def update_audio_device(self, kind: str, index: int) -> None:
        if kind not in ("input", "output"):
            raise ValueError("Unknown audio device kind")
        device = sd.query_devices(index)
        if kind == "input" and device["max_input_channels"] <= 0:
            raise ValueError("Selected device is not an input")
        if kind == "output" and device["max_output_channels"] <= 0:
            raise ValueError("Selected device is not an output")
        key = "input_device" if kind == "input" else "output_device"
        self.audio_config[key] = int(index)
        AUDIO_CONFIG_PATH.write_text(
            json.dumps(self.audio_config, indent=2),
            encoding="utf-8",
        )
        if kind == "output":
            global BEEP_OUTPUT_DEVICE
            BEEP_OUTPUT_DEVICE = int(index)
        logging.info("Audio %s set to %s", kind, index)

    def play_test_tone(self) -> None:
        device = self.audio_config.get("output_device")
        rate = 44100
        t = np.linspace(0, 0.28, int(rate * 0.28), endpoint=False)
        tone = 0.18 * np.sin(2 * np.pi * 660 * t)
        sd.play(tone, rate, device=device)
        sd.wait()

    def update_api_key(self, key: str) -> None:
        key = key.strip()
        if len(key) < 20:
            raise ValueError("API key is too short")
        keyring.set_password(SERVICE, USERNAME, key)
        self.api_key = key
        logging.info("DeepSeek API key updated")

    def update_hotkey(self, mode: str, binding: dict) -> dict:
        data = load_hotkeys()
        other = "hold" if mode == "toggle" else "toggle"
        if same_binding(binding, data.get(other)):
            data.pop(other, None)
        data[mode] = binding
        HOTKEYS_PATH.write_text(
            json.dumps(data, indent=2),
            encoding="utf-8",
        )
        self.hotkeys = data
        if self.main_window:
            self.main_window.refresh_keys()
        logging.info("Hotkey %s set to %s", mode, binding)
        return data

    def show_main(self) -> None:
        if self.main_window:
            self.main_window.show()

    def paste_history(self, text: str) -> None:
        pyperclip.copy(text)
        target = self.last_target_hwnd
        if not target or not is_probable_text_target(target):
            logging.info("History text copied; no text target")
            return
        if self.main_window:
            self.main_window.hide()
        time.sleep(0.2)
        set_foreground_window(target)
        time.sleep(0.15)
        try:
            controller = keyboard.Controller()
            with controller.pressed(keyboard.Key.ctrl):
                controller.press("v")
                controller.release("v")
            logging.info("History text pasted")
        except Exception:
            logging.exception("History paste failed")

    def record_history(self, text: str) -> None:
        if not self.main_window:
            return
        self.main_window.history.add(text)
        self.main_window.refresh_history()

    def shutdown(self) -> None:
        logging.info("Shutting down")
        if self.main_window and self.main_window.mic_testing:
            self.main_window.stop_mic_test()
        if self.tray:
            self.tray.stop()
        try:
            self.keyboard_listener.stop()
            self.mouse_listener.stop()
        except Exception:
            pass
        self.recorder.audio.restore()
        try:
            self.overlay.root.destroy()
        except Exception:
            pass

    def start_recording(self, mode: str) -> None:
        with self.lock:
            if self.mode is not None or self.processing:
                return
            self.mode = mode
            self.recorder.start(self.audio_config.get("input_device"))
            self.overlay.set_state("listening")
            beep(880)

    def stop_recording(self) -> None:
        with self.lock:
            if self.mode is None:
                return
            result = self.recorder.stop()
            self.mode = None
        beep(660)
        if not result:
            logging.warning("No audio captured")
            self.overlay.set_state("idle")
            return
        audio, hwnd = result
        if len(audio) < SAMPLE_RATE // 2:
            logging.info("Recording too short; ignored")
            self.overlay.set_state("idle")
            return
        self.processing = True
        self.overlay.set_state("processing")
        threading.Thread(target=self.process, args=(audio, hwnd), daemon=True).start()

    def cancel_recording(self) -> None:
        with self.lock:
            if self.mode is None:
                return
            self.recorder.stop()
            self.mode = None
        beep(520)
        self.overlay.set_state("idle")
        logging.info("Recording cancelled")

    def on_key_press(self, key) -> None:
        if self.capture_active:
            return
        name = normalize_key(key)
        if not name:
            return
        if name in self.pressed:
            return
        self.pressed.add(name)

        toggle = self.hotkeys["toggle"]
        hold = self.hotkeys["hold"]
        if self.matches(toggle, "keyboard", name):
            if self.mode == "toggle":
                self.stop_recording()
            else:
                self.start_recording("toggle")
        elif self.matches(hold, "keyboard", name):
            self.start_recording("hold")

    def on_key_release(self, key) -> None:
        if self.capture_active:
            return
        name = normalize_key(key)
        if name:
            self.pressed.discard(name)
        if self.matches(self.hotkeys["hold"], "keyboard", name or ""):
            if self.mode == "hold":
                self.stop_recording()

    def on_mouse_click(self, x, y, button, pressed) -> None:
        if self.capture_active:
            return
        name = button.name
        if pressed:
            if self.matches(self.hotkeys["toggle"], "mouse", name):
                if self.mode == "toggle":
                    self.stop_recording()
                else:
                    self.start_recording("toggle")
            elif self.matches(self.hotkeys["hold"], "mouse", name):
                self.start_recording("hold")
        else:
            if self.matches(self.hotkeys["hold"], "mouse", name):
                if self.mode == "hold":
                    self.stop_recording()

    def transcribe(self, audio: np.ndarray) -> str:
        if not self.api_key:
            raise RuntimeError("DeepSeek API key is not configured")
        texts = []
        chunk_size = CHUNK_SECONDS * SAMPLE_RATE
        for start in range(0, len(audio), chunk_size):
            chunk = audio[start : start + chunk_size]
            buffer = io.BytesIO()
            sf.write(buffer, chunk, SAMPLE_RATE, format="WAV", subtype="PCM_16")
            buffer.seek(0)
            response = httpx.post(
                ASR_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": ("audio.wav", buffer, "audio/wav")},
                timeout=120.0,
            )
            response.raise_for_status()
            text = response.json().get("text", "").strip()
            if text:
                texts.append(text)
        return " ".join(texts).strip()

    def polish(self, text: str) -> str:
        try:
            now = datetime.now().astimezone().strftime("%Y-%m-%d %I:%M %p %Z")
            system_prompt = SYSTEM_PROMPT.format(now=now)
            response = httpx.post(
                CHAT_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": "deepseek-flash",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": text},
                    ],
                    "thinking": {"type": "disabled"},
                    "temperature": 0.0,
                    "max_tokens": 4096,
                    "stream": False,
                },
                timeout=120.0,
            )
            response.raise_for_status()
            polished = response.json()["choices"][0]["message"]["content"].strip()
            return polished or text
        except Exception:
            logging.exception("Polish failed; using raw transcript")
            return text

    def paste(self, text: str, hwnd: int) -> None:
        pyperclip.copy(text)
        if not is_probable_text_target(hwnd):
            logging.info("No probable text target; text copied to clipboard only")
            return
        set_foreground_window(hwnd)
        time.sleep(0.15)
        controller = keyboard.Controller()
        with controller.pressed(keyboard.Key.ctrl):
            controller.press("v")
            controller.release("v")

    def process(self, audio: np.ndarray, hwnd: int) -> None:
        try:
            raw = self.transcribe(audio)
            if not raw:
                logging.warning("ASR returned empty text")
                beep(440, 120)
                self.overlay.set_state("error")
                return
            final = self.polish(raw)
            self.last_target_hwnd = hwnd
            self.paste(final, hwnd)
            logging.info("Processed %d chars", len(final))
            self.overlay.root.after(0, self.record_history, final)
        except Exception:
            logging.exception("Transcription failed")
            beep(440, 120)
            self.overlay.set_state("error")
        finally:
            self.processing = False
            self.overlay.set_state("idle")

    def check_show_flag(self) -> None:
        if SHOW_FLAG_PATH.exists():
            try:
                SHOW_FLAG_PATH.unlink()
            except Exception:
                pass
            self.show_main()
        if self.overlay.root.winfo_exists():
            self.overlay.root.after(500, self.check_show_flag)

    def run(self) -> None:
        logging.info("Zuri Flow started")
        self.main_window = MainWindow(self.overlay.root, self)
        self.main_window.show()
        try:
            self.tray = TrayIcon(self)
            self.tray.start()
        except Exception:
            logging.exception("Could not start tray icon")
            self.tray = None
        self.keyboard_listener.start()
        self.mouse_listener.start()
        self.overlay.set_state("idle")
        self.overlay.root.after(500, self.check_show_flag)
        self.overlay.root.mainloop()


def main() -> int:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Zuri.Flow")
    except Exception:
        pass
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\WhisperFlowApp")
    if ctypes.windll.kernel32.GetLastError() == 183:
        try:
            SHOW_FLAG_PATH.write_text("show", encoding="utf-8")
        except Exception:
            pass
        return 0
    try:
        WhisperFlow().run()
    except Exception:
        logging.exception("Fatal error")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

