"""E6 tests for tray_app.py. Must never start pystray's icon loop or spawn
the uvicorn child process -- find_pid_on_port is tested as a pure function
on canned text, and the import-side-effect test monkeypatches
subprocess.Popen to raise if it's ever called merely by importing/reloading
the module."""
import importlib
import subprocess

import tray_app

SAMPLE_NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       900
  TCP    0.0.0.0:445            0.0.0.0:0              LISTENING       4
  TCP    127.0.0.1:8787         0.0.0.0:0              LISTENING       4321
  TCP    127.0.0.1:54321        127.0.0.1:8787         ESTABLISHED     6789
  TCP    0.0.0.0:9999           0.0.0.0:0              LISTENING       111
  TCP6   [::]:8787              [::]:0                 LISTENING       4321
"""


def test_find_pid_on_port_matches_listening_row_and_ignores_noise():
    assert tray_app.find_pid_on_port(SAMPLE_NETSTAT, 8787) == "4321"


def test_find_pid_on_port_returns_none_when_no_match():
    assert tray_app.find_pid_on_port(SAMPLE_NETSTAT, 5555) is None
    assert tray_app.find_pid_on_port("", 8787) is None


def test_import_tray_app_has_no_side_effects(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("subprocess.Popen must not be called at import time")

    monkeypatch.setattr(subprocess, "Popen", boom)
    importlib.reload(tray_app)  # re-executes module top level with Popen trapped

    assert tray_app._server_proc is None
    assert tray_app._owned is False


def test_load_icon_image_fallback_then_custom_logo(monkeypatch, tmp_path):
    from PIL import Image

    monkeypatch.setattr(tray_app, "LOGO_TRAY_PATH", tmp_path / "does_not_exist.png")
    fallback_img = tray_app.load_icon_image()
    assert fallback_img.size[0] > 0
    assert fallback_img.size[1] > 0

    logo_path = tmp_path / "logo_tray.png"
    Image.new("RGBA", (2, 2), (10, 20, 30, 255)).save(logo_path)
    monkeypatch.setattr(tray_app, "LOGO_TRAY_PATH", logo_path)

    custom_img = tray_app.load_icon_image()
    assert custom_img.size == (2, 2)
    assert custom_img.convert("RGBA").getpixel((0, 0)) == (10, 20, 30, 255)
