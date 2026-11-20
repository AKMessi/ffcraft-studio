"""1-Click Entry Point for FFCraft Studio.

Starts the local web server + auto-managed llama-server and opens a standalone native-looking
app window (Edge/Chrome --app mode on Windows/macOS/Linux, falling back to default browser).
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from ffcraft_studio.config import DEFAULT_APP_PORT
from ffcraft_studio.server import create_server


def _pick_free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", preferred)) != 0:
            return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _open_app_window(url: str) -> None:
    """Launch in chromeless --app window (Edge/Chrome) for a true native desktop feel."""
    sys_name = platform.system().lower()
    browser_bins: list[str] = []

    if sys_name == "windows":
        candidates = [
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
        browser_bins = [str(p) for p in candidates if p.is_file()]
    elif sys_name == "darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
        browser_bins = [str(p) for p in candidates if p.is_file()]
    else:
        for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
            found = shutil.which(name)
            if found:
                browser_bins.append(found)

    for b in browser_bins:
        try:
            subprocess.Popen(
                [b, f"--app={url}", "--window-size=1120,820"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except Exception:
            continue

    webbrowser.open(url)


def main() -> None:
    p = argparse.ArgumentParser(description="Launch FFCraft Studio — 1-click local FFmpeg assistant.")
    p.add_argument("--port", type=int, default=DEFAULT_APP_PORT)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-browser", action="store_true", help="Run server without opening a window")
    args = p.parse_args()

    port = _pick_free_port(args.port)
    url = f"http://{args.host}:{port}"

    httpd, state = create_server(host=args.host, port=port)

    def _shutdown(*_a: object) -> None:
        print("\nShutting down FFCraft Studio...")
        state.runtime.stop()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    print(f"FFCraft Studio running at {url}")
    print("Press Ctrl+C to quit (automatically stops llama-server).")

    if not args.no_browser:
        threading.Timer(0.45, _open_app_window, args=(url,)).start()

    try:
        httpd.serve_forever()
    finally:
        state.runtime.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
