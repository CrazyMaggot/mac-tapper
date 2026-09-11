#!/usr/bin/env python3
"""Автокликер для macOS: частота, кнопка, глобальная горячая клавиша."""

from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import asdict, dataclass
from pathlib import Path
from tkinter import ttk

import Quartz
from AppKit import NSWorkspace
from pynput import keyboard

MIN_CPS = 0.01
MAX_CPS = 1000.0
SLEEP_CPS_THRESHOLD = 200.0
POSITION_HZ = 100.0
RESYNC_LAG = 0.25

SETTINGS_PATH = Path(__file__).with_name("tapper_settings.json")
PRIVACY_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"

MODIFIER_WORDS = {
    "cmd", "command", "shift", "ctrl", "control", "alt", "option",
    "fn", "caps", "capslock", "meta", "win",
}


def _named_keys() -> dict[str, object]:
    keys = {
        "space": keyboard.Key.space,
        "tab": keyboard.Key.tab,
        "enter": keyboard.Key.enter,
        "return": keyboard.Key.enter,
        "backspace": keyboard.Key.backspace,
        "delete": keyboard.Key.delete,
        "home": keyboard.Key.home,
        "end": keyboard.Key.end,
        "pageup": keyboard.Key.page_up,
        "pagedown": keyboard.Key.page_down,
        "up": keyboard.Key.up,
        "down": keyboard.Key.down,
        "left": keyboard.Key.left,
        "right": keyboard.Key.right,
    }
    for i in range(1, 21):
        key = getattr(keyboard.Key, f"f{i}", None)
        if key is not None:
            keys[f"f{i}"] = key
    return keys


NAMED_KEYS = _named_keys()


@dataclass(frozen=True)
class Hotkey:
    """Одна неслужебная клавиша, по которой переключается работа."""

    label: str
    char: str | None = None
    special: object | None = None

    def matches(self, key) -> bool:
        if self.special is not None:
            return key == self.special
        char = getattr(key, "char", None)
        if not char:
            return False
        return char.lower() == self.char


def parse_hotkey(text: str) -> Hotkey:
    """Разбирает введённое в окно обозначение клавиши."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Горячая клавиша не задана")
    low = raw.lower()
    if low in {"esc", "escape"}:
        raise ValueError("Esc зарезервирован под аварийную остановку")
    if low in MODIFIER_WORDS:
        raise ValueError("Нужна одна неслужебная клавиша, без модификаторов")
    if low in NAMED_KEYS:
        return Hotkey(label=low, special=NAMED_KEYS[low])
    if len(raw) == 1 and raw.isprintable() and not raw.isspace():
        return Hotkey(label=low, char=low)
    raise ValueError(f"Не понимаю клавишу «{raw}»")


def parse_cps(text: str) -> float:
    value = _to_float(text, "частоту")
    if value < MIN_CPS or value > MAX_CPS:
        raise ValueError(f"Частота должна быть от {MIN_CPS} до {MAX_CPS:.0f} в секунду")
    return value


def parse_delay(text: str) -> float:
    value = _to_float(text, "задержку")
    if value < 0 or value > 60:
        raise ValueError("Задержка должна быть от 0 до 60 секунд")
    return value


def parse_minutes(text: str) -> float:
    value = _to_float(text, "лимит времени")
    if value <= 0 or value > 600:
        raise ValueError("Лимит времени должен быть от 0 до 600 минут")
    return value


def _to_float(text: str, what: str) -> float:
    raw = (text or "").strip().replace(",", ".")
    if not raw:
        raise ValueError(f"Не задано значение: {what}")
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"Не число: {what}") from None


@dataclass
class Settings:
    cps: float = 10.0
    button: str = "left"
    hotkey: str = "f6"
    delay: float = 0.5
    autostop_enabled: bool = False
    autostop_minutes: float = 1.0
    topmost: bool = True

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
        try:
            return cls(**known)
        except TypeError:
            return cls()

    def save(self) -> None:
        try:
            SETTINGS_PATH.write_text(
                json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass


# --- системный ввод ---------------------------------------------------------

BUTTONS = {
    "left": (
        Quartz.kCGEventLeftMouseDown,
        Quartz.kCGEventLeftMouseUp,
        Quartz.kCGMouseButtonLeft,
    ),
    "right": (
        Quartz.kCGEventRightMouseDown,
        Quartz.kCGEventRightMouseUp,
        Quartz.kCGMouseButtonRight,
    ),
}


def cursor_position():
    """Текущая позиция курсора в той же системе координат, что и события мыши."""
    return Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))


def permission_status() -> tuple[bool, bool]:
    """Возвращает, разрешено ли отправлять события и слушать клавиатуру."""
    can_post = _preflight("CGPreflightPostEventAccess")
    can_listen = _preflight("CGPreflightListenEventAccess")
    return can_post, can_listen


def _preflight(name: str) -> bool:
    func = getattr(Quartz, name, None)
    if func is None:
        return True
    try:
        return bool(func())
    except Exception:
        return True


def request_permissions() -> None:
    for name in ("CGRequestPostEventAccess", "CGRequestListenEventAccess"):
        func = getattr(Quartz, name, None)
        if func is not None:
            try:
                func()
            except Exception:
                pass


def open_privacy_settings() -> None:
    subprocess.Popen(["open", PRIVACY_URL])


def is_frontmost() -> bool:
    """Наше ли приложение сейчас впереди. Tk на macOS про это не знает."""
    try:
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
    except Exception:
        return False
    return front is not None and front.processIdentifier() == os.getpid()


def hotkey_suppressed(frontmost: bool, focused, entries) -> bool:
    """Клавиша молчит, только когда мы сами впереди и курсор стоит в поле ввода."""
    if not frontmost:
        return False
    return any(focused is entry for entry in entries)


def _inside(point, rect) -> bool:
    x, y, w, h = rect
    return x <= point.x <= x + w and y <= point.y <= y + h


class Clicker:
    """Поток, отправляющий клики в точку курсора с заданной частотой."""

    def __init__(self, events: queue.Queue):
        self._events = events
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cps = 10.0
        self._cps_lock = threading.Lock()
        self._rects_lock = threading.Lock()
        self._blocked_rect: tuple[float, float, float, float] | None = None
        self._allow_rect: tuple[float, float, float, float] | None = None
        self.clicks = 0

    # --- управление ---

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def set_cps(self, cps: float) -> None:
        with self._cps_lock:
            self._cps = max(MIN_CPS, min(MAX_CPS, cps))

    def set_rects(self, blocked, allow) -> None:
        with self._rects_lock:
            self._blocked_rect = blocked
            self._allow_rect = allow

    def start(self, cps: float, button: str, delay: float, limit_seconds: float | None) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self.clicks = 0
        self.set_cps(cps)
        self._thread = threading.Thread(
            target=self._run,
            args=(button, delay, limit_seconds),
            name="clicker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # --- рабочий цикл ---

    def _blocked_at(self, point) -> bool:
        with self._rects_lock:
            blocked, allow = self._blocked_rect, self._allow_rect
        if allow is not None and _inside(point, allow):
            return False
        return blocked is not None and _inside(point, blocked)

    def _run(self, button: str, delay: float, limit_seconds: float | None) -> None:
        down_type, up_type, code = BUTTONS[button]
        source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        point = cursor_position()
        down = Quartz.CGEventCreateMouseEvent(source, down_type, point, code)
        up = Quartz.CGEventCreateMouseEvent(source, up_type, point, code)
        for event in (down, up):
            Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, 1)

        tap = Quartz.kCGHIDEventTap
        post = Quartz.CGEventPost
        set_location = Quartz.CGEventSetLocation
        perf = time.perf_counter
        sleep = time.sleep

        reason = "Остановлен"
        started = perf()
        deadline = started + delay
        next_position_at = 0.0
        blocked = False

        try:
            while not self._stop.is_set():
                now = perf()
                if limit_seconds is not None and now - started >= limit_seconds:
                    reason = "Автостоп по времени"
                    break

                with self._cps_lock:
                    interval = 1.0 / self._cps

                wait = deadline - now
                if wait > 0:
                    if interval >= 1.0 / SLEEP_CPS_THRESHOLD and wait > 0.002:
                        if self._stop.wait(wait - 0.001):
                            break
                    else:
                        while perf() < deadline:
                            sleep(0)
                    continue

                if now >= next_position_at:
                    point = cursor_position()
                    blocked = self._blocked_at(point)
                    set_location(down, point)
                    set_location(up, point)
                    next_position_at = now + 1.0 / POSITION_HZ

                if not blocked:
                    post(tap, down)
                    post(tap, up)
                    self.clicks += 1

                deadline += interval
                if deadline < now - RESYNC_LAG:
                    deadline = now + interval
        except Exception as error:  # noqa: BLE001 - показываем причину в окне
            reason = f"Ошибка: {error}"
        finally:
            try:
                post(tap, up)
            except Exception:
                pass
            self._events.put(("stopped", reason))


# --- окно -------------------------------------------------------------------

class TestWindow(tk.Toplevel):
    """Область для замера, сколько кликов реально доходит до приложения."""

    def __init__(self, master):
        super().__init__(master)
        self.title("Проверка доставки")
        self.geometry("340x200")
        self.received = 0
        self._samples: list[tuple[float, int]] = []

        self.info = ttk.Label(self, text="Наведите курсор сюда и включите таппер")
        self.info.pack(pady=(10, 4))
        self.total = ttk.Label(self, text="Принято кликов: 0", font=("", 15, "bold"))
        self.total.pack()
        self.rate = ttk.Label(self, text="Принятая частота: 0.0 в секунду")
        self.rate.pack(pady=(2, 8))

        target = tk.Frame(self, bg="#dfe6ef", highlightbackground="#8fa3bd", highlightthickness=1)
        target.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        for sequence in ("<ButtonPress-1>", "<ButtonPress-3>"):
            target.bind(sequence, self._count)

        ttk.Button(self, text="Сбросить", command=self._reset).pack(pady=(0, 10))

    def _count(self, _event=None):
        self.received += 1

    def _reset(self):
        self.received = 0
        self._samples.clear()

    def refresh(self):
        now = time.perf_counter()
        self._samples.append((now, self.received))
        while len(self._samples) > 1 and now - self._samples[0][0] > 0.5:
            self._samples.pop(0)
        rate = 0.0
        if len(self._samples) > 1:
            span = self._samples[-1][0] - self._samples[0][0]
            if span > 0:
                rate = (self._samples[-1][1] - self._samples[0][1]) / span
        self.total.configure(text=f"Принято кликов: {self.received}")
        self.rate.configure(text=f"Принятая частота: {rate:.1f} в секунду")

    def rect(self):
        return (
            float(self.winfo_rootx()),
            float(self.winfo_rooty()),
            float(self.winfo_width()),
            float(self.winfo_height()),
        )


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = Settings.load()
        self.events: queue.Queue = queue.Queue()
        self.clicker = Clicker(self.events)
        self.test_window: TestWindow | None = None

        self._entry_focused = False
        self._entries: tuple = ()
        self._hotkey_down = False
        self._hotkey: Hotkey | None = None
        self._start_time = 0.0
        self._delay = 0.0
        self._samples: list[tuple[float, int]] = []

        root.title("Таппер")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.quit)

        style = ttk.Style()
        style.configure("Error.TEntry", fieldbackground="#ffd9d9")

        self._build()
        self._apply_settings()
        self._reload_hotkey(quiet=True)

        self.listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self.listener.daemon = True
        self.listener.start()

        atexit.register(self.clicker.stop)
        root.after(100, self._tick)
        root.after(300, self._check_permissions)

    # --- построение окна ---

    def _build(self) -> None:
        pad = {"padx": 10, "pady": 4}
        frame = ttk.Frame(self.root, padding=12)
        frame.grid(sticky="nsew")

        row = 0
        ttk.Label(frame, text="Кликов в секунду").grid(row=row, column=0, sticky="w", **pad)
        self.cps_var = tk.StringVar()
        self.cps_entry = ttk.Entry(frame, textvariable=self.cps_var, width=10)
        self.cps_entry.grid(row=row, column=1, sticky="w", **pad)
        self.cps_var.trace_add("write", lambda *_: self._on_cps_typed())

        row += 1
        ttk.Label(frame, text="Кнопка").grid(row=row, column=0, sticky="w", **pad)
        self.button_var = tk.StringVar(value="left")
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=1, sticky="w", **pad)
        ttk.Radiobutton(buttons, text="Левая", value="left", variable=self.button_var).pack(side="left")
        ttk.Radiobutton(buttons, text="Правая", value="right", variable=self.button_var).pack(side="left", padx=(10, 0))

        row += 1
        ttk.Label(frame, text="Горячая клавиша").grid(row=row, column=0, sticky="w", **pad)
        self.hotkey_var = tk.StringVar()
        self.hotkey_entry = ttk.Entry(frame, textvariable=self.hotkey_var, width=10)
        self.hotkey_entry.grid(row=row, column=1, sticky="w", **pad)
        self.hotkey_entry.bind("<KeyRelease>", lambda _e: self._reload_hotkey())
        self.hotkey_entry.bind("<FocusOut>", lambda _e: self._reload_hotkey())

        row += 1
        ttk.Label(frame, text="Задержка старта, с").grid(row=row, column=0, sticky="w", **pad)
        self.delay_var = tk.StringVar()
        self.delay_entry = ttk.Entry(frame, textvariable=self.delay_var, width=10)
        self.delay_entry.grid(row=row, column=1, sticky="w", **pad)

        row += 1
        self.autostop_var = tk.BooleanVar()
        ttk.Checkbutton(
            frame, text="Автостоп через, мин", variable=self.autostop_var
        ).grid(row=row, column=0, sticky="w", **pad)
        self.minutes_var = tk.StringVar()
        self.minutes_entry = ttk.Entry(frame, textvariable=self.minutes_var, width=10)
        self.minutes_entry.grid(row=row, column=1, sticky="w", **pad)

        row += 1
        self.topmost_var = tk.BooleanVar()
        ttk.Checkbutton(
            frame, text="Поверх всех окон", variable=self.topmost_var, command=self._apply_topmost
        ).grid(row=row, column=0, columnspan=2, sticky="w", **pad)

        row += 1
        ttk.Separator(frame).grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)

        row += 1
        self.state_label = ttk.Label(frame, text="Остановлен", font=("", 15, "bold"))
        self.state_label.grid(row=row, column=0, columnspan=2, sticky="w", **pad)

        row += 1
        self.rate_label = ttk.Label(frame, text="Фактическая частота: 0.0 в секунду")
        self.rate_label.grid(row=row, column=0, columnspan=2, sticky="w", **pad)

        row += 1
        self.clicks_label = ttk.Label(frame, text="Кликов за сессию: 0")
        self.clicks_label.grid(row=row, column=0, columnspan=2, sticky="w", **pad)

        row += 1
        self.message = ttk.Label(frame, text="", foreground="#b00020", wraplength=320)
        self.message.grid(row=row, column=0, columnspan=2, sticky="w", **pad)

        row += 1
        actions = ttk.Frame(frame)
        actions.grid(row=row, column=0, columnspan=2, sticky="w", **pad)
        self.toggle_button = ttk.Button(actions, text="Старт", command=self.toggle)
        self.toggle_button.pack(side="left")
        ttk.Button(actions, text="Проверка доставки", command=self.open_test).pack(side="left", padx=(8, 0))

        row += 1
        ttk.Label(
            frame,
            text="Esc останавливает во время работы. Клики над этим окном заблокированы.",
            foreground="#555555",
            wraplength=320,
        ).grid(row=row, column=0, columnspan=2, sticky="w", **pad)

        self._entries = (self.cps_entry, self.hotkey_entry, self.delay_entry, self.minutes_entry)

    def _apply_settings(self) -> None:
        s = self.settings
        self.cps_var.set(_format_number(s.cps))
        self.button_var.set(s.button if s.button in BUTTONS else "left")
        self.hotkey_var.set(s.hotkey)
        self.delay_var.set(_format_number(s.delay))
        self.autostop_var.set(s.autostop_enabled)
        self.minutes_var.set(_format_number(s.autostop_minutes))
        self.topmost_var.set(s.topmost)
        self._apply_topmost()

    def _apply_topmost(self) -> None:
        self.root.attributes("-topmost", bool(self.topmost_var.get()))

    def _collect(self) -> Settings:
        return Settings(
            cps=_safe_float(self.cps_var.get(), self.settings.cps),
            button=self.button_var.get(),
            hotkey=self.hotkey_var.get().strip(),
            delay=_safe_float(self.delay_var.get(), self.settings.delay),
            autostop_enabled=bool(self.autostop_var.get()),
            autostop_minutes=_safe_float(self.minutes_var.get(), self.settings.autostop_minutes),
            topmost=bool(self.topmost_var.get()),
        )

    # --- горячая клавиша ---

    def _update_focus_guard(self) -> None:
        try:
            focused = self.root.focus_get()
        except KeyError:
            focused = None
        self._entry_focused = hotkey_suppressed(is_frontmost(), focused, self._entries)

    def _reload_hotkey(self, quiet: bool = False) -> None:
        try:
            self._hotkey = parse_hotkey(self.hotkey_var.get())
        except ValueError as error:
            self._hotkey = None
            self.hotkey_entry.configure(style="Error.TEntry")
            if not quiet:
                self._say(str(error))
            return
        self.hotkey_entry.configure(style="TEntry")
        if not quiet:
            self._say("")

    def _on_press(self, key) -> None:
        if key == keyboard.Key.esc:
            if self.clicker.is_running():
                self.events.put(("stop", "Аварийная остановка по Esc"))
            return
        hotkey = self._hotkey
        if hotkey is None or not hotkey.matches(key):
            return
        if self._hotkey_down:
            return
        self._hotkey_down = True
        if self._entry_focused:
            return
        self.events.put(("toggle", None))

    def _on_release(self, key) -> None:
        # Сбрасываем на любом отпускании: при зажатом модификаторе символ
        # клавиши приходит другим, и сравнение с горячей клавишей не сойдётся.
        self._hotkey_down = False

    # --- запуск и остановка ---

    def toggle(self) -> None:
        if self.clicker.is_running():
            self.clicker.stop()
        else:
            self.start()

    def start(self) -> None:
        checks = (
            (self.cps_entry, lambda: parse_cps(self.cps_var.get())),
            (self.delay_entry, lambda: parse_delay(self.delay_var.get())),
            (self.hotkey_entry, lambda: parse_hotkey(self.hotkey_var.get())),
        )
        values = []
        for entry, check in checks:
            entry.configure(style="TEntry")
        for entry, check in checks:
            try:
                values.append(check())
            except ValueError as error:
                entry.configure(style="Error.TEntry")
                entry.focus_set()
                self._say(str(error))
                return
        cps, delay, _hotkey = values

        limit = None
        self.minutes_entry.configure(style="TEntry")
        if self.autostop_var.get():
            try:
                limit = parse_minutes(self.minutes_var.get()) * 60.0
            except ValueError as error:
                self.minutes_entry.configure(style="Error.TEntry")
                self.minutes_entry.focus_set()
                self._say(str(error))
                return

        self._say("")
        self.settings = self._collect()
        self.settings.save()
        self._samples.clear()
        self._start_time = time.perf_counter()
        self._delay = delay
        self.clicker.start(cps, self.button_var.get(), delay, limit)
        self.toggle_button.configure(text="Стоп")

    def open_test(self) -> None:
        if self.test_window is not None and self.test_window.winfo_exists():
            self.test_window.lift()
            return
        self.test_window = TestWindow(self.root)

    def quit(self) -> None:
        self.clicker.stop()
        thread = self.clicker._thread
        if thread is not None:
            thread.join(timeout=1.0)
        try:
            self.listener.stop()
        except Exception:
            pass
        self._collect().save()
        self.root.destroy()

    # --- периодическое обновление ---

    def _tick(self) -> None:
        while True:
            try:
                name, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if name == "toggle":
                self.toggle()
            elif name == "stop":
                self.clicker.stop()
                self._say(payload)
            elif name == "stopped":
                self.toggle_button.configure(text="Старт")
                if payload and payload != "Остановлен":
                    self._say(payload)

        self._update_focus_guard()
        self._update_rects()
        self._update_stats()
        self.root.after(100, self._tick)

    def _update_rects(self) -> None:
        blocked = (
            float(self.root.winfo_rootx()),
            float(self.root.winfo_rooty()),
            float(self.root.winfo_width()),
            float(self.root.winfo_height()),
        )
        allow = None
        if self.test_window is not None:
            if self.test_window.winfo_exists():
                allow = self.test_window.rect()
                self.test_window.refresh()
            else:
                self.test_window = None
        self.clicker.set_rects(blocked, allow)

    def _update_stats(self) -> None:
        running = self.clicker.is_running()
        now = time.perf_counter()
        clicks = self.clicker.clicks

        if running:
            if now - self._start_time < self._delay:
                left = self._delay - (now - self._start_time)
                self.state_label.configure(text=f"Старт через {left:.1f} с", foreground="#a06000")
            else:
                self.state_label.configure(text="Работает", foreground="#0a7a2f")
            try:
                self.clicker.set_cps(parse_cps(self.cps_var.get()))
            except ValueError:
                pass
        else:
            self.state_label.configure(text="Остановлен", foreground="#333333")

        self._samples.append((now, clicks))
        while len(self._samples) > 1 and now - self._samples[0][0] > 0.5:
            self._samples.pop(0)
        rate = 0.0
        if running and len(self._samples) > 1:
            span = self._samples[-1][0] - self._samples[0][0]
            if span > 0:
                rate = (self._samples[-1][1] - self._samples[0][1]) / span
        self.rate_label.configure(text=f"Фактическая частота: {rate:.1f} в секунду")
        self.clicks_label.configure(text=f"Кликов за сессию: {clicks}")

    def _on_cps_typed(self) -> None:
        self.cps_entry.configure(style="TEntry")

    def _say(self, text: str) -> None:
        self.message.configure(text=text)

    # --- разрешения системы ---

    def _check_permissions(self) -> None:
        can_post, can_listen = permission_status()
        if can_post and can_listen:
            return
        request_permissions()
        self._show_permission_dialog()

    def _show_permission_dialog(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Нужно разрешение")
        window.resizable(False, False)
        body = ttk.Frame(window, padding=16)
        body.pack()
        ttk.Label(
            body,
            wraplength=380,
            text=(
                "Программе не выдан «Универсальный доступ». Без него горячая клавиша "
                "не сработает, а клики не дойдут до других приложений.\n\n"
                "Откройте настройки, включите в списке приложение, из которого запущен "
                "таппер, и перезапустите его."
            ),
        ).pack(pady=(0, 12))
        buttons = ttk.Frame(body)
        buttons.pack()
        ttk.Button(
            buttons, text="Открыть настройки", command=open_privacy_settings
        ).pack(side="left")
        ttk.Button(buttons, text="Закрыть", command=window.destroy).pack(side="left", padx=(8, 0))


def _format_number(value: float) -> str:
    text = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _safe_float(text: str, fallback: float) -> float:
    try:
        return _to_float(text, "значение")
    except ValueError:
        return fallback


def main() -> int:
    sys.setswitchinterval(0.0005)
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
