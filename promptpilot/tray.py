"""System tray launcher for PromptPilot.

Double-click pp.exe → tray icon appears.
Right-click → start/stop services, open Web UI.
"""

import atexit
import os
import subprocess
import sys
import threading
import webbrowser

import pystray
from PIL import Image, ImageDraw, ImageFont

from .config import HOST, PORT

_procs: dict[str, subprocess.Popen] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Icon
# ---------------------------------------------------------------------------

def _make_icon(color: str = "#757575") -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Circle background
    d.ellipse([2, 2, size - 2, size - 2], fill=color)
    # "PP" label — use default font, center it
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    d.text((size // 2, size // 2), "PP", fill="white", font=font, anchor="mm")
    return img


def _status_color() -> str:
    running = sum(1 for p in _procs.values() if p.poll() is None)
    if running == 0:
        return "#757575"   # grey  — all stopped
    if running < len(_procs):
        return "#FF9800"   # orange — partial
    return "#4CAF50"       # green  — all running


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

def _cmd(service: str) -> list[str]:
    """Build command for a service using the current executable."""
    if getattr(sys, "frozen", False):
        return [sys.executable, service]
    return [sys.executable, "-m", "promptpilot", service]


def _is_running(service: str) -> bool:
    p = _procs.get(service)
    return p is not None and p.poll() is None


def _start(service: str):
    with _lock:
        if _is_running(service):
            return
        _procs[service] = subprocess.Popen(_cmd(service))


def _stop(service: str):
    with _lock:
        p = _procs.pop(service, None)
        if p and p.poll() is None:
            p.terminate()


def _stop_all():
    for service in list(_procs):
        _stop(service)


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

def _label(service: str):
    """Dynamic menu item label showing running state."""
    def fn(item):
        mark = "▶" if _is_running(service) else "■"
        return f"{mark}  {service.capitalize()}"
    return fn


def _toggle(service: str, icon: pystray.Icon):
    if _is_running(service):
        _stop(service)
    else:
        _start(service)
    _refresh(icon)


def _refresh(icon: pystray.Icon):
    icon.icon = _make_icon(_status_color())
    icon.title = _tooltip()
    icon.update_menu()


def _tooltip() -> str:
    parts = []
    for name, p in _procs.items():
        state = "running" if p.poll() is None else "stopped"
        parts.append(f"{name}: {state}")
    return "PromptPilot\n" + "\n".join(parts) if parts else "PromptPilot"


def _build_menu(icon: pystray.Icon) -> pystray.Menu:
    def toggle_fn(svc):
        return lambda i, it: _toggle(svc, i)

    def toggle_bot(i, it):
        if not os.environ.get("PP_TG_TOKEN"):
            # Show hint in tooltip — token not configured
            i.title = "PromptPilot: PP_TG_TOKEN не задан в .env"
            return
        _toggle("bot", i)

    def start_all(i, it):
        _start("worker")
        _start("server")
        if os.environ.get("PP_TG_TOKEN"):
            _start("bot")
        _refresh(i)

    def stop_all(i, it):
        _stop_all()
        _refresh(i)

    def quit_app(i, it):
        _stop_all()
        i.stop()

    items = [
        pystray.MenuItem("PromptPilot", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(_label("worker"), toggle_fn("worker")),
        pystray.MenuItem(_label("server"), toggle_fn("server")),
        pystray.MenuItem(_label("bot"),    toggle_bot),
    ]

    items += [
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Запустить все",  start_all),
        pystray.MenuItem("Остановить все", stop_all),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            f"Открыть Web UI  ({HOST}:{PORT})",
            lambda i, it: webbrowser.open(f"http://{HOST}:{PORT}"),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", quit_app),
    ]

    return pystray.Menu(*items)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_tray():
    """Start the system tray application and auto-launch worker + server."""
    atexit.register(_stop_all)

    icon = pystray.Icon(
        name="PromptPilot",
        icon=_make_icon(_status_color()),
        title="PromptPilot",
    )
    icon.menu = _build_menu(icon)

    # Auto-start core services
    _start("worker")
    _start("server")
    if os.environ.get("PP_TG_TOKEN"):
        _start("bot")

    _refresh(icon)
    icon.run()
