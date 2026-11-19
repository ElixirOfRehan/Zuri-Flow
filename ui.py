import ctypes
import json
import sqlite3
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path

import pyperclip
import pystray
from PIL import Image, ImageTk
from pynput import keyboard, mouse

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", ROOT))
LOGO_PATH = RESOURCE_DIR / "logo-256.png"
LOGO_ICO = RESOURCE_DIR / "logo.ico"
DB_PATH = ROOT / "history.db"
HOTKEYS_PATH = ROOT / "hotkeys.json"


class HistoryStore:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dictations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                text TEXT NOT NULL
            )
            """
        )
        self.conn.commit()
        self.cleanup()

    def cleanup(self) -> None:
        cutoff = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
        self.conn.execute("DELETE FROM dictations WHERE created_at < ?", (cutoff,))
        self.conn.commit()

    def add(self, text: str) -> None:
        self.cleanup()
        self.conn.execute(
            "INSERT INTO dictations(created_at, text) VALUES (?, ?)",
            (datetime.now().isoformat(timespec="seconds"), text),
        )
        self.conn.commit()

    def items(self) -> list[dict]:
        self.cleanup()
        rows = self.conn.execute(
            "SELECT id, created_at, text FROM dictations ORDER BY id DESC"
        ).fetchall()
        return [
            {
                "id": row[0],
                "created_at": datetime.fromisoformat(row[1]),
                "text": row[2],
            }
            for row in rows
        ]

    def delete(self, item_id: int) -> None:
        self.conn.execute("DELETE FROM dictations WHERE id = ?", (item_id,))
        self.conn.commit()


def enable_dark_titlebar(window) -> None:
    try:
        hwnd = window.winfo_id()
        parent = ctypes.windll.user32.GetParent(hwnd)
        if parent:
            hwnd = parent
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(value), ctypes.sizeof(value)
        )
    except Exception:
        pass


def normalize_key(key) -> str | None:
    value = getattr(key, "char", None)
    if value is not None:
        return str(value).lower()
    value = getattr(key, "name", None)
    if value is not None:
        return str(value).lower()
    return None


def describe(binding: dict | None) -> str:
    if not binding:
        return "Not set"
    kind = binding.get("type", "")
    value = str(binding.get("value", ""))
    if kind == "mouse":
        return f"Mouse: {value}"
    return f"Key: {value.upper()}"


class KeyCaptureDialog:
    def __init__(self, parent, app, mode: str) -> None:
        self.app = app
        self.mode = mode
        self.captured: dict | None = None
        self.keyboard_listener = None
        self.mouse_listener = None
        self.closed = False
        self.app.capture_active = True

        self.window = tk.Toplevel(parent)
        self.window.title("Set key")
        self.window.configure(bg="#111111")
        self.window.geometry("420x250")
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.grab_set()
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.update_idletasks()
        enable_dark_titlebar(self.window)

        title = "Toggle activation" if mode == "toggle" else "Hold activation"
        tk.Label(
            self.window,
            text=f"Set {title}",
            bg="#111111",
            fg="#f3f3f3",
            font=("Segoe UI", 14, "bold"),
        ).pack(pady=(22, 6))
        tk.Label(
            self.window,
            text="Press any key or mouse button.\nEsc cancels.",
            bg="#111111",
            fg="#9ca3af",
            font=("Segoe UI", 9),
        ).pack()

        self.value = tk.Entry(
            self.window,
            justify="center",
            font=("Consolas", 12, "bold"),
            bg="#1d1d1d",
            fg="#f3f3f3",
            readonlybackground="#1d1d1d",
            disabledbackground="#1d1d1d",
            insertbackground="#f3f3f3",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#3a3a3a",
            highlightcolor="#3a3a3a",
        )
        self.value.pack(fill="x", padx=36, pady=18, ipady=8)
        self.value.configure(state="readonly")

        buttons = tk.Frame(self.window, bg="#111111")
        buttons.pack(pady=8)
        self.retry = tk.Button(
            buttons,
            text="Retry",
            command=self.start_capture,
            bg="#2a2a2a",
            fg="#f3f3f3",
            activebackground="#3a3a3a",
            activeforeground="#ffffff",
            relief="flat",
            padx=22,
            pady=7,
            state="disabled",
        )
        self.retry.pack(side="left", padx=8)
        self.confirm = tk.Button(
            buttons,
            text="Confirm",
            command=self.confirm_binding,
            bg="#f3f3f3",
            fg="#111111",
            activebackground="#ffffff",
            activeforeground="#111111",
            relief="flat",
            padx=22,
            pady=7,
            state="disabled",
        )
        self.confirm.pack(side="left", padx=8)

        self.status = tk.Label(
            self.window,
            text="Waiting for input...",
            bg="#111111",
            fg="#9ca3af",
            font=("Segoe UI", 8),
        )
        self.status.pack(pady=4)

        self.window.after(450, self.start_capture)

    def start_capture(self) -> None:
        if self.closed:
            return
        self.stop_listeners()
        self.captured = None
        self.value.configure(state="normal")
        self.value.delete(0, tk.END)
        self.value.configure(state="readonly")
        self.retry.configure(state="disabled")
        self.confirm.configure(state="disabled")
        self.status.configure(text="Waiting for input...", fg="#9ca3af")

        self.keyboard_listener = keyboard.Listener(on_press=self.on_key)
        self.mouse_listener = mouse.Listener(on_click=self.on_click)
        self.keyboard_listener.start()
        self.mouse_listener.start()

    def stop_listeners(self) -> None:
        for listener in (self.keyboard_listener, self.mouse_listener):
            if listener:
                listener.stop()
        self.keyboard_listener = None
        self.mouse_listener = None

    def on_key(self, key) -> None:
        if key == keyboard.Key.esc:
            self.window.after(0, self.close)
            return
        name = normalize_key(key)
        if name:
            binding = {"type": "keyboard", "value": name}
            self.window.after(0, lambda b=binding: self.set_capture(b))

    def on_click(self, x, y, button, pressed) -> None:
        if pressed:
            binding = {"type": "mouse", "value": button.name}
            self.window.after(0, lambda b=binding: self.set_capture(b))

    def set_capture(self, binding: dict) -> None:
        if self.captured:
            return
        self.captured = binding
        self.stop_listeners()
        self.value.configure(state="normal")
        self.value.delete(0, tk.END)
        self.value.insert(0, describe(binding))
        self.value.configure(state="readonly")
        self.retry.configure(state="normal")
        self.confirm.configure(state="normal")
        self.status.configure(text="Confirm to save, or Retry.", fg="#9ca3af")

    def confirm_binding(self) -> None:
        if not self.captured:
            return
        config = self.app.update_hotkey(self.mode, self.captured)
        other = "hold" if self.mode == "toggle" else "toggle"
        if config.get(other) is None:
            self.status.configure(
                text=f"Saved. The previous {other} binding was cleared.",
                fg="#f59e0b",
            )
        else:
            self.status.configure(text="Saved.", fg="#22c55e")
        self.window.after(600, self.close)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.app.capture_active = False
        self.stop_listeners()
        self.window.destroy()


class InlineKeyCapture:
    def __init__(self, parent, app, mode: str, on_close) -> None:
        self.app = app
        self.mode = mode
        self.on_close = on_close
        self.captured: dict | None = None
        self.keyboard_listener = None
        self.mouse_listener = None
        self.closed = False
        self.app.capture_active = True

        self.frame = tk.Frame(parent, bg="#151515")
        self.frame.pack(fill="x", padx=34, pady=(12, 0))

        title = "Toggle activation" if mode == "toggle" else "Hold activation"
        tk.Label(
            self.frame,
            text=f"Set {title}",
            bg="#151515",
            fg="#f3f3f3",
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(12, 2))
        tk.Label(
            self.frame,
            text="Press any key or mouse button. Press Esc to cancel.",
            bg="#151515",
            fg="#6b7280",
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", padx=16)

        self.value = tk.Entry(
            self.frame,
            justify="center",
            font=("Consolas", 11, "bold"),
            bg="#1d1d1d",
            fg="#f3f3f3",
            readonlybackground="#1d1d1d",
            disabledbackground="#1d1d1d",
            insertbackground="#f3f3f3",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#3a3a3a",
            highlightcolor="#3a3a3a",
        )
        self.value.pack(fill="x", padx=16, pady=10, ipady=6)
        self.value.configure(state="readonly")

        buttons = tk.Frame(self.frame, bg="#151515")
        buttons.pack(fill="x", padx=16, pady=(0, 10))
        self.retry = tk.Button(
            buttons,
            text="Retry",
            command=self.start_capture,
            bg="#2a2a2a",
            fg="#f3f3f3",
            activebackground="#3a3a3a",
            activeforeground="#ffffff",
            relief="flat",
            padx=18,
            pady=5,
            state="disabled",
        )
        self.retry.pack(side="left")
        self.confirm = tk.Button(
            buttons,
            text="Confirm",
            command=self.confirm_binding,
            bg="#f3f3f3",
            fg="#111111",
            activebackground="#ffffff",
            activeforeground="#111111",
            relief="flat",
            padx=18,
            pady=5,
            state="disabled",
        )
        self.confirm.pack(side="left", padx=8)
        tk.Button(
            buttons,
            text="Cancel",
            command=self.close,
            bg="#151515",
            fg="#9ca3af",
            activebackground="#242424",
            activeforeground="#ffffff",
            relief="flat",
            padx=12,
            pady=5,
        ).pack(side="right")
        self.status = tk.Label(
            self.frame,
            text="Waiting for input...",
            bg="#151515",
            fg="#6b7280",
            font=("Segoe UI", 8),
            anchor="w",
        )
        self.status.pack(fill="x", padx=16, pady=(0, 12))

        self.frame.after(400, self.start_capture)

    def start_capture(self) -> None:
        if self.closed:
            return
        self.stop_listeners()
        self.captured = None
        self.value.configure(state="normal")
        self.value.delete(0, tk.END)
        self.value.configure(state="readonly")
        self.retry.configure(state="disabled")
        self.confirm.configure(state="disabled")
        self.status.configure(text="Waiting for input...", fg="#6b7280")
        self.keyboard_listener = keyboard.Listener(on_press=self.on_key)
        self.mouse_listener = mouse.Listener(on_click=self.on_click)
        self.keyboard_listener.start()
        self.mouse_listener.start()

    def stop_listeners(self) -> None:
        for listener in (self.keyboard_listener, self.mouse_listener):
            if listener:
                listener.stop()
        self.keyboard_listener = None
        self.mouse_listener = None

    def on_key(self, key) -> None:
        if key == keyboard.Key.esc:
            self.frame.after(0, self.close)
            return
        name = normalize_key(key)
        if name:
            binding = {"type": "keyboard", "value": name}
            self.frame.after(0, lambda b=binding: self.set_capture(b))

    def on_click(self, x, y, button, pressed) -> None:
        if pressed:
            binding = {"type": "mouse", "value": button.name}
            self.frame.after(0, lambda b=binding: self.set_capture(b))

    def set_capture(self, binding: dict) -> None:
        if self.captured or self.closed:
            return
        self.captured = binding
        self.stop_listeners()
        self.value.configure(state="normal")
        self.value.delete(0, tk.END)
        self.value.insert(0, describe(binding))
        self.value.configure(state="readonly")
        self.retry.configure(state="normal")
        self.confirm.configure(state="normal")
        self.status.configure(text="Confirm to save, or Retry.", fg="#6b7280")

    def confirm_binding(self) -> None:
        if not self.captured:
            return
        config = self.app.update_hotkey(self.mode, self.captured)
        other = "hold" if self.mode == "toggle" else "toggle"
        if config.get(other) is None:
            self.status.configure(
                text=f"Saved. Previous {other} binding cleared.",
                fg="#f59e0b",
            )
        else:
            self.status.configure(text="Saved.", fg="#22c55e")
        self.frame.after(700, self.close)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.app.capture_active = False
        self.stop_listeners()
        try:
            self.frame.destroy()
        except Exception:
            pass
        self.on_close()


class ApiKeyPanel:
    def __init__(self, parent, app, on_close) -> None:
        self.app = app
        self.on_close = on_close
        self.closed = False

        self.frame = tk.Frame(parent, bg="#151515")
        self.frame.pack(fill="x", padx=34, pady=(8, 0))
        tk.Label(
            self.frame,
            text="Change DeepSeek API key",
            bg="#151515",
            fg="#f3f3f3",
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(12, 2))
        tk.Label(
            self.frame,
            text="Paste the new key. It replaces the current key immediately.",
            bg="#151515",
            fg="#6b7280",
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", padx=16)

        self.value = tk.Entry(
            self.frame,
            show="*",
            font=("Consolas", 11),
            bg="#1d1d1d",
            fg="#f3f3f3",
            insertbackground="#f3f3f3",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#3a3a3a",
            highlightcolor="#3a3a3a",
        )
        self.value.pack(fill="x", padx=16, pady=10, ipady=6)
        self.value.bind("<Return>", lambda event: self.save())

        buttons = tk.Frame(self.frame, bg="#151515")
        buttons.pack(fill="x", padx=16, pady=(0, 10))
        tk.Button(
            buttons,
            text="Save",
            command=self.save,
            bg="#f3f3f3",
            fg="#111111",
            activebackground="#ffffff",
            activeforeground="#111111",
            relief="flat",
            padx=18,
            pady=5,
        ).pack(side="left")
        tk.Button(
            buttons,
            text="Cancel",
            command=self.close,
            bg="#2a2a2a",
            fg="#f3f3f3",
            activebackground="#3a3a3a",
            activeforeground="#ffffff",
            relief="flat",
            padx=14,
            pady=5,
        ).pack(side="left", padx=8)
        self.status = tk.Label(
            self.frame,
            text="",
            bg="#151515",
            fg="#6b7280",
            font=("Segoe UI", 8),
            anchor="w",
        )
        self.status.pack(fill="x", padx=16, pady=(0, 12))

    def save(self) -> None:
        key = self.value.get().strip()
        try:
            self.app.update_api_key(key)
        except Exception as exc:
            self.status.configure(text=str(exc), fg="#ef4444")
            return
        self.status.configure(text="Saved. New key is active.", fg="#22c55e")
        self.frame.after(500, self.close)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.frame.destroy()
        except Exception:
            pass
        self.on_close()


class TrayIcon:
    def __init__(self, app) -> None:
        self.app = app
        image = Image.open(LOGO_PATH).resize((64, 64), Image.LANCZOS)
        menu = pystray.Menu(
            pystray.MenuItem("Open Zuri Flow", self.show, default=True),
            pystray.MenuItem("Show / Hide HUD", self.toggle_hud),
            pystray.MenuItem("Quit", self.quit),
        )
        self.icon = pystray.Icon("Zuri Flow", image, "Zuri Flow", menu)

    def start(self) -> None:
        self.icon.run_detached()

    def show(self, icon=None, item=None) -> None:
        self.app.overlay.root.after(0, self.app.show_main)

    def toggle_hud(self, icon=None, item=None) -> None:
        def toggle() -> None:
            root = self.app.overlay.root
            if root.state() == "withdrawn":
                root.deiconify()
            else:
                root.withdraw()
        self.app.overlay.root.after(0, toggle)

    def quit(self, icon=None, item=None) -> None:
        self.app.overlay.root.after(0, self.app.shutdown)

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass


class MainWindow:
    def __init__(self, parent, app) -> None:
        self.app = app
        self.history = HistoryStore()
        self.key_capture = None
        self.api_key_panel = None
        self.window = tk.Toplevel(parent)
        self.window.title("Zuri Flow")
        screen_w = self.window.winfo_screenwidth()
        screen_h = self.window.winfo_screenheight()
        width = min(1000, screen_w - 100)
        height = min(620, screen_h - 120)
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        self.window.geometry(f"{width}x{height}+{x}+{y}")
        self.window.minsize(820, 560)
        self.window.configure(bg="#0b0b0b")
        try:
            self.window.state("zoomed")
        except Exception:
            pass
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        self.window.update_idletasks()
        enable_dark_titlebar(self.window)
        try:
            logo = Image.open(LOGO_PATH).resize((54, 54), Image.LANCZOS)
            self.logo_photo = ImageTk.PhotoImage(logo)
            self.window.iconphoto(True, self.logo_photo)
            self.app.overlay.root.iconphoto(True, self.logo_photo)
            self.window.iconbitmap(default=str(LOGO_ICO))
            self.app.overlay.root.iconbitmap(default=str(LOGO_ICO))
        except Exception:
            self.logo_photo = None

        self.sidebar = tk.Frame(self.window, bg="#111111", width=210)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        brand = tk.Frame(self.sidebar, bg="#111111")
        brand.pack(fill="x", padx=16, pady=(22, 24))
        if self.logo_photo is not None:
            tk.Label(
                brand,
                image=self.logo_photo,
                bg="#111111",
                bd=0,
            ).pack(side="left")
        text = tk.Frame(brand, bg="#111111")
        text.pack(side="left", padx=(10, 0))
        tk.Label(
            text,
            text="Zuri Flow",
            bg="#111111",
            fg="#f3f3f3",
            font=("Segoe UI", 14, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            text,
            text="Voice to text",
            bg="#111111",
            fg="#6b7280",
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x")

        self.nav_buttons = {}
        for key, label in (("home", "History"), ("settings", "Settings")):
            button = tk.Button(
                self.sidebar,
                text=label,
                command=lambda k=key: self.show_page(k),
                bg="#111111",
                fg="#cbd5e1",
                activebackground="#1d1d1d",
                activeforeground="#ffffff",
                relief="flat",
                anchor="w",
                padx=20,
                pady=10,
                font=("Segoe UI", 10),
            )
            button.pack(fill="x", padx=10, pady=2)
            self.nav_buttons[key] = button

        tk.Label(
            self.sidebar,
            text="F8 toggle   •   F9 hold",
            bg="#111111",
            fg="#6b7280",
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(side="bottom", fill="x", padx=20, pady=(0, 8))
        tk.Button(
            self.sidebar,
            text="Quit",
            command=self.app.shutdown,
            bg="#111111",
            fg="#9ca3af",
            activebackground="#1d1d1d",
            activeforeground="#ffffff",
            relief="flat",
            anchor="w",
            padx=20,
            pady=8,
            font=("Segoe UI", 9),
        ).pack(side="bottom", fill="x", padx=10, pady=(0, 12))

        self.content = tk.Frame(self.window, bg="#0b0b0b")
        self.content.pack(side="left", fill="both", expand=True)

        self.pages = {}
        self.build_history_page()
        self.build_settings_page()
        self.show_page("home")
        self.refresh_history()
        self.window.after(3600000, self.periodic_cleanup)

    def periodic_cleanup(self) -> None:
        self.history.cleanup()
        self.refresh_history()
        if self.window.winfo_exists():
            self.window.after(3600000, self.periodic_cleanup)

    def build_history_page(self) -> None:
        page = tk.Frame(self.content, bg="#0b0b0b")
        self.pages["home"] = page
        header = tk.Frame(page, bg="#0b0b0b")
        header.pack(fill="x", padx=34, pady=(28, 14))
        tk.Label(
            header,
            text="History",
            bg="#0b0b0b",
            fg="#f3f3f3",
            font=("Segoe UI", 20, "bold"),
            anchor="w",
        ).pack(side="left")
        tk.Label(
            header,
            text="Last 2 days",
            bg="#0b0b0b",
            fg="#6b7280",
            font=("Segoe UI", 9),
            anchor="e",
        ).pack(side="right", pady=8)

        self.canvas = tk.Canvas(page, bg="#0b0b0b", highlightthickness=0)
        self.list_frame = tk.Frame(self.canvas, bg="#0b0b0b")
        self.scrollbar = tk.Scrollbar(
            page, orient="vertical", command=self.canvas.yview
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True, padx=(28, 0))
        self.canvas_window = self.canvas.create_window(
            (0, 0), window=self.list_frame, anchor="nw"
        )
        self.list_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self.canvas_window, width=e.width),
        )
        self.canvas.bind_all("<MouseWheel>", self.on_mousewheel)

    def build_settings_page(self) -> None:
        outer = tk.Frame(self.content, bg="#0b0b0b")
        self.pages["settings"] = outer
        self.settings_canvas = tk.Canvas(
            outer, bg="#0b0b0b", highlightthickness=0
        )
        self.settings_scrollbar = tk.Scrollbar(
            outer, orient="vertical", command=self.settings_canvas.yview
        )
        self.settings_canvas.configure(
            yscrollcommand=self.settings_scrollbar.set
        )
        self.settings_scrollbar.pack(side="right", fill="y")
        self.settings_canvas.pack(side="left", fill="both", expand=True)
        page = tk.Frame(self.settings_canvas, bg="#0b0b0b")
        settings_window = self.settings_canvas.create_window(
            (0, 0), window=page, anchor="nw"
        )
        page.bind(
            "<Configure>",
            lambda e: self.settings_canvas.configure(
                scrollregion=self.settings_canvas.bbox("all")
            ),
        )
        self.settings_canvas.bind(
            "<Configure>",
            lambda e: self.settings_canvas.itemconfigure(
                settings_window, width=e.width
            ),
        )
        self.active_page = "home"
        tk.Label(
            page,
            text="Settings",
            bg="#0b0b0b",
            fg="#f3f3f3",
            font=("Segoe UI", 20, "bold"),
            anchor="w",
        ).pack(fill="x", padx=34, pady=(28, 4))
        tk.Label(
            page,
            text="Choose the keys that control dictation.",
            bg="#0b0b0b",
            fg="#6b7280",
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", padx=34, pady=(0, 22))

        info = tk.Frame(page, bg="#151515")
        info.pack(fill="x", padx=34, pady=(0, 12))
        top = tk.Frame(info, bg="#151515")
        top.pack(fill="x", padx=16, pady=(12, 2))
        tk.Label(
            top,
            text="DeepSeek API key",
            bg="#151515",
            fg="#f3f3f3",
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            top,
            text="Stored in Windows Credential Manager. The new key applies immediately.",
            bg="#151515",
            fg="#6b7280",
            font=("Segoe UI", 8),
            anchor="w",
            wraplength=650,
            justify="left",
        ).pack(anchor="w", pady=(2, 0))
        row = tk.Frame(top, bg="#151515")
        row.pack(fill="x", pady=(8, 0))
        self.api_status = tk.Label(
            row,
            text="",
            bg="#151515",
            fg="#6b7280",
            font=("Segoe UI", 8),
            anchor="w",
        )
        self.api_status.pack(side="left")
        tk.Button(
            row,
            text="Change key",
            command=self.start_api_key_edit,
            bg="#2a2a2a",
            fg="#f3f3f3",
            activebackground="#3a3a3a",
            activeforeground="#ffffff",
            relief="flat",
            padx=12,
            pady=4,
        ).pack(side="left", padx=12)
        self.api_key_host = tk.Frame(page, bg="#0b0b0b")
        self.refresh_api_key_status()

        self.key_values = {}
        for mode, title, description in (
            ("toggle", "Toggle activation", "Press once to start, press again to stop."),
            ("hold", "Hold activation", "Hold while speaking, release to transcribe."),
        ):
            card = tk.Frame(page, bg="#151515")
            card.pack(fill="x", padx=34, pady=5)
            text = tk.Frame(card, bg="#151515")
            text.pack(fill="x", padx=16, pady=14)
            tk.Label(
                text,
                text=title,
                bg="#151515",
                fg="#f3f3f3",
                font=("Segoe UI", 10, "bold"),
                anchor="w",
            ).pack(fill="x")
            tk.Label(
                text,
                text=description,
                bg="#151515",
                fg="#6b7280",
                font=("Segoe UI", 8),
                anchor="w",
            ).pack(fill="x", pady=(2, 0))
            row = tk.Frame(text, bg="#151515")
            row.pack(fill="x", pady=(8, 0))
            value = tk.Label(
                row,
                text=describe(self.app.hotkeys.get(mode)),
                bg="#151515",
                fg="#9ca3af",
                font=("Consolas", 9, "bold"),
                anchor="w",
            )
            value.pack(side="left")
            self.key_values[mode] = value
            tk.Button(
                row,
                text="Set",
                command=lambda m=mode: self.start_key_capture(m),
                bg="#2a2a2a",
                fg="#f3f3f3",
                activebackground="#3a3a3a",
                activeforeground="#ffffff",
                relief="flat",
                padx=16,
                pady=4,
            ).pack(side="left", padx=16)

        self.key_capture_host = tk.Frame(page, bg="#0b0b0b")

    def show_page(self, name: str) -> None:
        self.active_page = name
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        if name == "home":
            self.refresh_history()

    def on_mousewheel(self, event) -> None:
        units = int(-event.delta / 120)
        if self.active_page == "settings":
            self.settings_canvas.yview_scroll(units, "units")
        else:
            self.canvas.yview_scroll(units, "units")

    def refresh_history(self) -> None:
        if not self.window.winfo_exists():
            return
        for child in self.list_frame.winfo_children():
            child.destroy()

        items = self.history.items()
        if not items:
            tk.Label(
                self.list_frame,
                text="No dictations yet.\nPress F8 or hold F9 to start.",
                bg="#0b0b0b",
                fg="#6b7280",
                font=("Segoe UI", 10),
                justify="left",
            ).pack(anchor="w", padx=6, pady=24)
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            return

        last_date = None
        for item in items:
            date = item["created_at"].date()
            if date != last_date:
                last_date = date
                label = (
                    "TODAY'S DICTATIONS"
                    if date == datetime.now().date()
                    else date.strftime("%B %d, %Y").upper()
                )
                tk.Label(
                    self.list_frame,
                    text=label,
                    bg="#0b0b0b",
                    fg="#6b7280",
                    font=("Segoe UI", 8, "bold"),
                    anchor="w",
                ).pack(fill="x", padx=6, pady=(18, 6))

            self.add_history_card(item)

        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def add_history_card(self, item: dict) -> None:
        card = tk.Frame(self.list_frame, bg="#151515")
        card.pack(fill="x", padx=6, pady=4)
        top = tk.Frame(card, bg="#151515")
        top.pack(fill="x", padx=14, pady=(12, 2))
        tk.Label(
            top,
            text=item["created_at"].strftime("%I:%M %p").lstrip("0").lower(),
            bg="#151515",
            fg="#6b7280",
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(side="left")
        actions = tk.Frame(top, bg="#151515")
        tk.Button(
            actions,
            text="Copy",
            command=lambda t=item["text"]: self.copy_text(t),
            bg="#242424",
            fg="#d1d5db",
            activebackground="#333333",
            activeforeground="#ffffff",
            relief="flat",
            padx=8,
            pady=2,
            font=("Segoe UI", 8),
        ).pack(side="left", padx=2)
        tk.Button(
            actions,
            text="Paste",
            command=lambda t=item["text"]: self.paste_text(t),
            bg="#242424",
            fg="#d1d5db",
            activebackground="#333333",
            activeforeground="#ffffff",
            relief="flat",
            padx=8,
            pady=2,
            font=("Segoe UI", 8),
        ).pack(side="left", padx=2)
        tk.Button(
            actions,
            text="Delete",
            command=lambda i=item["id"]: self.delete_item(i),
            bg="#242424",
            fg="#f87171",
            activebackground="#333333",
            activeforeground="#ffffff",
            relief="flat",
            padx=8,
            pady=2,
            font=("Segoe UI", 8),
        ).pack(side="left", padx=2)
        hide_job = [None]

        def show_actions(_event=None):
            if hide_job[0]:
                try:
                    card.after_cancel(hide_job[0])
                except Exception:
                    pass
                hide_job[0] = None
            actions.pack(side="right")

        def schedule_hide(_event=None):
            if hide_job[0]:
                try:
                    card.after_cancel(hide_job[0])
                except Exception:
                    pass
            hide_job[0] = card.after(180, actions.pack_forget)

        for widget in (card, top, actions):
            widget.bind("<Enter>", show_actions)
            widget.bind("<Leave>", schedule_hide)
        for child in actions.winfo_children():
            child.bind("<Enter>", show_actions)
            child.bind("<Leave>", schedule_hide)
        tk.Label(
            card,
            text=item["text"],
            bg="#151515",
            fg="#e5e7eb",
            font=("Segoe UI", 9),
            wraplength=730,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=14, pady=(2, 12))

    def copy_text(self, text: str) -> None:
        pyperclip.copy(text)

    def paste_text(self, text: str) -> None:
        self.app.paste_history(text)

    def delete_item(self, item_id: int) -> None:
        self.history.delete(item_id)
        self.refresh_history()

    def start_key_capture(self, mode: str) -> None:
        if self.key_capture:
            self.key_capture.close()
        if self.api_key_panel:
            self.api_key_panel.close()
        self.key_capture_host.pack(fill="x")
        self.key_capture = InlineKeyCapture(
            self.key_capture_host,
            self.app,
            mode,
            self.on_key_capture_closed,
        )

    def on_key_capture_closed(self) -> None:
        self.key_capture = None
        self.key_capture_host.pack_forget()
        self.refresh_keys()

    def start_api_key_edit(self) -> None:
        if self.api_key_panel:
            self.api_key_panel.close()
        if self.key_capture:
            self.key_capture.close()
        self.api_key_host.pack(fill="x")
        self.api_key_panel = ApiKeyPanel(
            self.api_key_host,
            self.app,
            self.on_api_key_closed,
        )

    def on_api_key_closed(self) -> None:
        self.api_key_panel = None
        self.api_key_host.pack_forget()
        self.refresh_api_key_status()

    def refresh_api_key_status(self) -> None:
        if self.app.api_key:
            self.api_status.configure(text="Configured", fg="#22c55e")
        else:
            self.api_status.configure(text="Missing", fg="#ef4444")

    def refresh_keys(self) -> None:
        for mode, label in self.key_values.items():
            label.config(text=describe(self.app.hotkeys.get(mode)))

    def show(self) -> None:
        self.window.deiconify()
        self.window.attributes("-topmost", True)
        self.window.lift()
        self.window.focus_force()
        self.window.after(
            400, lambda: self.window.attributes("-topmost", False)
        )
        self.refresh_history()

    def hide(self) -> None:
        if self.key_capture:
            self.key_capture.close()
        if self.api_key_panel:
            self.api_key_panel.close()
        self.window.withdraw()

    def destroy(self) -> None:
        self.window.destroy()
