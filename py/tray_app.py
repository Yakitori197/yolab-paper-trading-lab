"""E6 system-tray resident for the paper-trading dashboard.

Starts (or attaches to) the FastAPI/uvicorn server from dashboard.py and
exposes a tray icon with "open" / "close" controls. Nothing here touches
dashboard.py, index.html, or the paper_loop.py tick pipeline -- this is
purely a process-lifecycle wrapper around `uvicorn dashboard:app`.

Design decision: the tray menu deliberately does NOT offer a "stop the
scheduled tick" option. The Task Scheduler job that runs paper_loop.py on
its own cadence is a separate concern from this dashboard server, and a
stray click here silently breaking the paper-trading pipeline is worse than
the inconvenience of having to go stop it explicitly (scripts\\tray_remove.bat
or Task Scheduler) if that's ever actually wanted.

All imports with side effects (pystray, PIL, subprocess launches, the
127.0.0.1:8787 port probe) are deferred into functions -- `import tray_app`
alone must never start a server or create an icon; only main() does that.
"""
import os
import re
import socket
import subprocess
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / "py" / ".venv" / "Scripts" / "python.exe"
DASHBOARD_LOG = ROOT / "data" / "dashboard.log"
HOST = "127.0.0.1"
PORT = 8787
DASHBOARD_URL = f"http://{HOST}:{PORT}"
TOOLTIP = "Paper Lab 儀表板"

_server_proc = None  # subprocess.Popen once main() runs, only if this instance owns it
_owned = False


def is_port_listening(host=HOST, port=PORT, timeout=0.5):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def start_dashboard_server():
    """Launch uvicorn as a child process iff nothing already owns PORT.
    Returns (proc_or_None, owned: bool)."""
    if is_port_listening():
        return None, False
    DASHBOARD_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(DASHBOARD_LOG, "ab")
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "uvicorn", "dashboard:app",
         "--app-dir", "py", "--host", HOST, "--port", str(PORT)],
        stdout=log_f, stderr=log_f, cwd=str(ROOT),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return proc, True


def find_pid_on_port(text, port):
    """Pure function: parse `netstat -ano` output, return the PID (str) of
    the first TCP LISTENING row bound to `port`, or None if there isn't
    one. Kept standalone (no subprocess call inside) so it's testable on
    canned sample text."""
    suffix = f":{port}"
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        proto, local_addr, _foreign_addr, state, pid = parts[:5]
        if proto.upper() != "TCP" or state.upper() != "LISTENING":
            continue
        if local_addr.endswith(suffix):
            return pid
    return None


def stop_dashboard_server():
    """Stop whatever is listening on PORT: terminate our own child process
    (with a kill fallback) if we started it, otherwise shell out to
    netstat/taskkill to stop whoever else owns the port."""
    global _server_proc
    if _owned and _server_proc is not None:
        _server_proc.terminate()
        try:
            _server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server_proc.kill()
        _server_proc = None
        return
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
    except Exception:
        return
    pid = find_pid_on_port(out, PORT)
    if pid:
        subprocess.run(
            ["taskkill", "/f", "/pid", pid], capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


def make_icon_image():
    """64x64 dark circle with a small green rising-candle motif, drawn in
    place with PIL (no icon asset file). Fallback used by load_icon_image()
    when the custom tray logo asset is missing or unreadable."""
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, size - 2, size - 2), fill=(15, 18, 22, 255))
    bars = [(16, 44, 24, 52), (28, 34, 36, 52), (40, 20, 48, 52)]
    for x0, y0, x1, y1 in bars:
        d.rectangle((x0, y0, x1, y1), fill=(47, 179, 68, 255))
    return img


LOGO_TRAY_PATH = ROOT / "assets" / "logo_tray.png"


def load_icon_image():
    """Load the custom tray logo (assets/logo_tray.png, relative to ROOT)
    if present and readable; otherwise fall back to make_icon_image().
    Never raises."""
    from PIL import Image
    try:
        if LOGO_TRAY_PATH.exists():
            return Image.open(LOGO_TRAY_PATH).convert("RGBA")
    except Exception:
        pass
    return make_icon_image()


def on_open(icon=None, item=None):
    webbrowser.open(DASHBOARD_URL)


def on_close(icon=None, item=None):
    stop_dashboard_server()
    icon.stop()


def build_menu():
    import pystray
    return pystray.Menu(
        pystray.MenuItem("開啟儀表板", on_open, default=True),
        pystray.MenuItem("關閉儀表板", on_close),
    )


def main():
    global _server_proc, _owned
    os.chdir(ROOT)
    _server_proc, _owned = start_dashboard_server()

    import pystray
    icon = pystray.Icon("paper-lab-dashboard", load_icon_image(), TOOLTIP, build_menu())
    icon.run()


if __name__ == "__main__":
    main()
