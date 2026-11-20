"""Auto-managed llama-server lifecycle + Hugging Face GGUF downloader."""

from __future__ import annotations

import atexit
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from ffcraft_studio.config import (
    DEFAULT_GGUF_NAME,
    DEFAULT_LLM_PORT,
    HF_DOWNLOAD_URL,
    HF_REPO_ID,
    MODEL_ALIAS,
    download_target_path,
    find_local_model,
)


def _find_llama_server() -> str | None:
    found = shutil.which("llama-server")
    if found:
        return found
    # Check common Windows winget & macOS Homebrew locations
    candidates = [
        Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links" / "llama-server.exe",
        Path("/opt/homebrew/bin/llama-server"),
        Path("/usr/local/bin/llama-server"),
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def _is_port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.35)
        return s.connect_ex((host, port)) == 0


def _pick_free_port(preferred: int) -> int:
    if not _is_port_open("127.0.0.1", preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class ModelRuntime:
    """Owns the background llama-server process and optional model download state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.external_api_url: str | None = os.environ.get("FFCRAFT_API_URL")
        self.llama_bin: str | None = _find_llama_server()
        self.ffmpeg_ok: bool = shutil.which("ffmpeg") is not None
        self.ffprobe_ok: bool = shutil.which("ffprobe") is not None
        self.model_path: Path | None = find_local_model()
        self.llm_port: int = DEFAULT_LLM_PORT
        self.proc: subprocess.Popen | None = None
        self.state: str = "idle"  # idle | starting | ready | missing_model | missing_llama | downloading | error
        self.error: str | None = None
        self.download_progress: dict[str, Any] = {
            "active": False,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "percent": 0.0,
            "speed_mb_s": 0.0,
            "error": None,
        }
        atexit.register(self.stop)

    @property
    def api_base_url(self) -> str:
        if self.external_api_url:
            return self.external_api_url.rstrip("/")
        return f"http://127.0.0.1:{self.llm_port}/v1"

    def _health_ok(self) -> bool:
        if self.external_api_url:
            return True
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.llm_port}/health", timeout=1.5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def start_async(self) -> None:
        threading.Thread(target=self.ensure_started, daemon=True).start()

    def ensure_started(self) -> None:
        with self._lock:
            if self.external_api_url:
                self.state = "ready"
                return

            self.llama_bin = _find_llama_server()
            self.model_path = find_local_model()

            if not self.llama_bin:
                self.state = "missing_llama"
                self.error = "llama-server not found. Install via: winget install ggml.llamacpp (or brew install llama.cpp)."
                return

            if not self.model_path:
                self.state = "missing_model"
                self.error = None
                return

            if self.proc and self.proc.poll() is None and self._health_ok():
                self.state = "ready"
                return

            self.state = "starting"
            self.error = None
            self.llm_port = _pick_free_port(DEFAULT_LLM_PORT)

            cmd = [
                self.llama_bin,
                "-m", str(self.model_path),
                "--host", "127.0.0.1",
                "--port", str(self.llm_port),
                "--ctx-size", "4096",
                "--alias", MODEL_ALIAS,
            ]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                self.proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=creationflags,
                )
            except Exception as exc:
                self.state = "error"
                self.error = f"Failed to start llama-server: {exc}"
                return

        # Poll health outside lock (loading Q8_0 onto GPU/CPU takes ~2-8s)
        deadline = time.time() + 60.0
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                err_txt = ""
                try:
                    err_txt = (self.proc.stderr.read() or "")[-500:]
                except Exception:
                    pass
                with self._lock:
                    self.state = "error"
                    self.error = f"llama-server exited early: {err_txt.strip()}"
                return
            if self._health_ok():
                with self._lock:
                    self.state = "ready"
                return
            time.sleep(0.35)

        with self._lock:
            self.state = "error"
            self.error = "Timed out waiting for llama-server to become ready."

    def start_download_model(self) -> bool:
        with self._lock:
            if self.download_progress["active"]:
                return False
            self.state = "downloading"
            self.error = None
            self.download_progress = {
                "active": True,
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "percent": 0.0,
                "speed_mb_s": 0.0,
                "error": None,
            }

        threading.Thread(target=self._download_worker, daemon=True).start()
        return True

    def _download_worker(self) -> None:
        dest = download_target_path()
        tmp = dest.with_suffix(".gguf.part")
        start_t = time.time()
        try:
            req = urllib.request.Request(HF_DOWNLOAD_URL, headers={"User-Agent": "FFCraft-Studio/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                with tmp.open("wb") as fh:
                    while True:
                        chunk = resp.read(1024 * 512)
                        if not chunk:
                            break
                        fh.write(chunk)
                        downloaded += len(chunk)
                        elapsed = max(0.05, time.time() - start_t)
                        self.download_progress.update({
                            "downloaded_bytes": downloaded,
                            "total_bytes": total,
                            "percent": round((downloaded / total * 100.0), 1) if total else 0.0,
                            "speed_mb_s": round((downloaded / (1024 * 1024)) / elapsed, 2),
                        })
            tmp.replace(dest)
            self.download_progress["active"] = False
            self.download_progress["percent"] = 100.0
            self.model_path = dest
            self.ensure_started()
        except Exception as exc:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            with self._lock:
                self.download_progress["active"] = False
                self.download_progress["error"] = str(exc)
                self.state = "error"
                self.error = f"Download failed: {exc}"

    def stop(self) -> None:
        proc = self.proc
        self.proc = None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def status_dict(self) -> dict[str, Any]:
        # Re-check health if ready
        if self.state == "ready" and not self.external_api_url:
            if self.proc is None or self.proc.poll() is not None:
                self.state = "error"
                self.error = "llama-server stopped unexpectedly."
        return {
            "state": self.state,
            "error": self.error,
            "model_alias": MODEL_ALIAS,
            "model_file": self.model_path.name if self.model_path else None,
            "model_path": str(self.model_path) if self.model_path else None,
            "hf_repo": HF_REPO_ID,
            "default_gguf": DEFAULT_GGUF_NAME,
            "llama_found": self.llama_bin is not None,
            "ffmpeg_found": self.ffmpeg_ok,
            "ffprobe_found": self.ffprobe_ok,
            "llm_port": self.llm_port,
            "download": dict(self.download_progress),
        }
