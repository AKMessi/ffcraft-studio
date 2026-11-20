"""Threaded HTTP + SSE server bridging the FFCraft Studio UI, llama-server, ffprobe, and ffmpeg."""

from __future__ import annotations

import json
import mimetypes
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ffcraft_studio.config import MODEL_ALIAS, SYSTEM_PROMPT, WEB_DIR, detect_os_label
from ffcraft_studio.executor import FFmpegRunner, extract_ffmpeg_command
from ffcraft_studio.media import (
    build_media_prompt_prefix,
    pick_folder_native,
    pick_media_file_native,
    probe_media,
    reveal_in_file_manager,
)
from ffcraft_studio.runtime import ModelRuntime


class StudioAppState:
    def __init__(self) -> None:
        self.runtime = ModelRuntime()
        self.runner = FFmpegRunner()


def make_handler(state: StudioAppState) -> type[BaseHTTPRequestHandler]:
    class StudioHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            # Keep terminal quiet unless FFCRAFT_DEBUG=1
            pass

        def _send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            return json.loads(raw.decode("utf-8"))

        def _start_sse(self) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

        def _send_sse(self, event_obj: dict[str, Any]) -> bool:
            chunk = f"data: {json.dumps(event_obj, ensure_ascii=False)}\n\n".encode("utf-8")
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/status":
                data = state.runtime.status_dict()
                data["os"] = detect_os_label()
                self._send_json(data)
                return

            # Static files from web/
            rel = parsed.path.lstrip("/") or "index.html"
            if ".." in rel:
                self._send_json({"error": "Invalid path"}, HTTPStatus.BAD_REQUEST)
                return
            target = (WEB_DIR / rel).resolve()
            if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
                target = WEB_DIR / "index.html"

            ctype, _ = mimetypes.guess_type(str(target))
            body = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{ctype or 'application/octet-stream'}; charset=utf-8" if (ctype or "").startswith("text/") or ctype == "application/javascript" else (ctype or "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/api/model/download":
                started = state.runtime.start_download_model()
                self._send_json({"started": started, "status": state.runtime.status_dict()})
                return

            if path == "/api/model/start":
                state.runtime.start_async()
                self._send_json({"status": state.runtime.status_dict()})
                return

            if path == "/api/file/pick":
                chosen = pick_media_file_native()
                if not chosen:
                    self._send_json({"cancelled": True})
                    return
                try:
                    meta = probe_media(chosen)
                    self._send_json({"cancelled": False, "file": meta})
                except Exception as exc:
                    self._send_json({"cancelled": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return

            if path == "/api/file/probe":
                body = self._read_json()
                raw_path = (body.get("path") or "").strip().strip('"').strip("'")
                if not raw_path:
                    self._send_json({"error": "Empty path"}, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    meta = probe_media(raw_path)
                    self._send_json({"file": meta})
                except Exception as exc:
                    self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return

            if path == "/api/folder/pick":
                folder = pick_folder_native()
                if not folder:
                    self._send_json({"cancelled": True})
                    return
                self._send_json({"cancelled": False, "folder": folder})
                return

            if path == "/api/reveal":
                body = self._read_json()
                target = body.get("path")
                if not target or not Path(target).exists():
                    self._send_json({"error": "Path not found"}, HTTPStatus.NOT_FOUND)
                    return
                reveal_in_file_manager(target)
                self._send_json({"ok": True})
                return

            if path == "/api/run/cancel":
                body = self._read_json()
                ok = state.runner.cancel(body.get("job_id", ""))
                self._send_json({"cancelled": ok})
                return

            if path == "/api/chat":
                self._handle_chat_sse()
                return

            if path == "/api/run":
                self._handle_run_sse()
                return

            self._send_json({"error": f"Unknown endpoint: {path}"}, HTTPStatus.NOT_FOUND)

        def _handle_chat_sse(self) -> None:
            body = self._read_json()
            messages: list[dict[str, str]] = list(body.get("messages") or [])
            attached_file: dict[str, Any] | None = body.get("attached_file")
            use_system: bool = bool(body.get("use_system_prompt", True))

            if state.runtime.state != "ready":
                self._send_json(
                    {"error": f"Model is not ready yet (state={state.runtime.state})."},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return

            # Build final message list cleanly without mutating user-visible history
            send_msgs: list[dict[str, str]] = []
            if use_system:
                send_msgs.append({"role": "system", "content": SYSTEM_PROMPT})

            # Inject real ffprobe file context into the FIRST user message (so multi-turn chats stay consistent!)
            media_prefix = build_media_prompt_prefix(attached_file) if attached_file else ""
            injected = False
            for m in messages:
                role = m.get("role")
                content = m.get("content", "")
                if role == "user" and media_prefix and not injected:
                    content = media_prefix + content
                    injected = True
                if role in ("user", "assistant"):
                    send_msgs.append({"role": role, "content": content})

            payload = {
                "model": MODEL_ALIAS,
                "messages": send_msgs,
                "temperature": 0.1,
                "top_p": 0.9,
                "max_tokens": 512,
                "stream": True,
            }

            self._start_sse()
            t0 = time.time()
            full_text = ""
            token_chunks = 0

            req = urllib.request.Request(
                f"{state.runtime.api_base_url}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": "Bearer local"},
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    for raw_line in resp:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {}).get("content")
                        if delta:
                            full_text += delta
                            token_chunks += 1
                            if not self._send_sse({"type": "delta", "content": delta}):
                                return
            except urllib.error.URLError as exc:
                self._send_sse({"type": "error", "message": f"Lost connection to llama-server: {exc}"})
                return
            except Exception as exc:
                self._send_sse({"type": "error", "message": str(exc)})
                return

            elapsed = max(0.05, time.time() - t0)
            tps = round(token_chunks / elapsed, 1)
            cmd = extract_ffmpeg_command(full_text)
            self._send_sse({
                "type": "done",
                "full_text": full_text,
                "command": cmd,
                "tokens": token_chunks,
                "elapsed_sec": round(elapsed, 2),
                "tokens_per_sec": tps,
            })

        def _handle_run_sse(self) -> None:
            body = self._read_json()
            command = (body.get("command") or "").strip()
            working_dir = (body.get("working_dir") or "").strip()
            duration_sec = body.get("duration_sec")
            input_size_bytes = body.get("input_size_bytes")

            if not command:
                self._send_json({"error": "Missing command"}, HTTPStatus.BAD_REQUEST)
                return
            if not working_dir:
                self._send_json({"error": "Missing working_dir"}, HTTPStatus.BAD_REQUEST)
                return

            self._start_sse()

            def emit(ev: dict[str, Any]) -> None:
                self._send_sse(ev)

            state.runner.run_command(
                command=command,
                working_dir=working_dir,
                duration_sec=float(duration_sec) if duration_sec else None,
                input_size_bytes=int(input_size_bytes) if input_size_bytes else None,
                on_event=emit,
            )

    return StudioHandler


def create_server(host: str = "127.0.0.1", port: int = 8765) -> tuple[ThreadingHTTPServer, StudioAppState]:
    state = StudioAppState()
    state.runtime.start_async()
    server = ThreadingHTTPServer((host, port), make_handler(state))
    server.daemon_threads = True
    return server, state
