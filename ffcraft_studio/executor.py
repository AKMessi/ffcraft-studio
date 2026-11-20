"""Safe FFmpeg command parser & live-progress runner for FFCraft Studio."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ffcraft_studio.media import format_bytes, probe_media

CODE_BLOCK_RE = re.compile(r"```(?:bash|sh|powershell|cmd)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
SPEED_RE = re.compile(r"speed=\s*([\d.]+x)")


def extract_ffmpeg_command(text: str) -> str | None:
    matches = CODE_BLOCK_RE.findall(text)
    if not matches:
        return None
    cmd = matches[-1].strip()
    return cmd if "ffmpeg" in cmd else None


def split_passes(command: str) -> list[str]:
    normalized = command.replace("\\\r\n", " ").replace("\\\n", " ").replace("`\n", " ")
    return [p.strip() for p in re.split(r"\s*&&\s*", normalized) if p.strip()]


def _adapt_argv_for_os(argv: list[str]) -> list[str]:
    out: list[str] = []
    for tok in argv:
        if tok == "/dev/null" and os.name == "nt":
            out.append("NUL")
        elif tok == "NUL" and os.name != "nt":
            out.append("/dev/null")
        else:
            out.append(tok)
    if out and out[0].lower() in ("ffmpeg", "ffmpeg.exe"):
        # Ensure overwrite prompt never hangs background runner & keep stats readable
        if "-y" not in out:
            out = [out[0], "-y", *out[1:]]
        if "-hide_banner" not in out:
            out = [out[0], "-hide_banner", *out[1:]]
    return out


def _detect_output_filename(passes: list[str]) -> str | None:
    if not passes:
        return None
    try:
        argv = shlex.split(passes[-1], posix=True)
    except ValueError:
        return None
    if not argv:
        return None
    last = argv[-1]
    if last.startswith("-") or last.upper() in ("NUL", "/DEV/NULL"):
        return None
    return last


def _detect_input_filenames(passes: list[str]) -> set[str]:
    inputs: set[str] = set()
    for p in passes:
        try:
            argv = shlex.split(p, posix=True)
        except ValueError:
            continue
        for idx, tok in enumerate(argv):
            if tok == "-i" and idx + 1 < len(argv):
                inputs.add(argv[idx + 1])
    return inputs


class FFmpegRunner:
    """Manages active FFmpeg jobs with cancellation and live progress callbacks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_procs: dict[str, subprocess.Popen] = {}
        self._cancelled: set[str] = set()

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            self._cancelled.add(job_id)
            proc = self._active_procs.get(job_id)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                return True
            except Exception:
                return False
        return False

    def run_command(
        self,
        *,
        command: str,
        working_dir: str | Path,
        duration_sec: float | None = None,
        input_size_bytes: int | None = None,
        on_event: Callable[[dict[str, Any]], None],
    ) -> None:
        job_id = uuid.uuid4().hex[:10]
        cwd = Path(working_dir).expanduser().resolve()
        if not cwd.is_dir():
            on_event({"type": "error", "job_id": job_id, "message": f"Working directory not found: {cwd}"})
            return

        passes = split_passes(command)
        if not passes:
            on_event({"type": "error", "job_id": job_id, "message": "No FFmpeg command found."})
            return

        # Safety check: only allow ffmpeg commands, and never overwrite an input file in place
        for p in passes:
            try:
                argv = shlex.split(p, posix=True)
            except ValueError as exc:
                on_event({"type": "error", "job_id": job_id, "message": f"Could not parse command: {exc}"})
                return
            if not argv or Path(argv[0]).stem.lower() != "ffmpeg":
                on_event({"type": "error", "job_id": job_id, "message": "Safety block: only ffmpeg commands can be executed here."})
                return

        out_name = _detect_output_filename(passes)
        in_names = _detect_input_filenames(passes)
        if out_name and out_name in in_names:
            on_event({
                "type": "error",
                "job_id": job_id,
                "message": f"Safety block: output '{out_name}' matches input filename. Rename output before running.",
            })
            return

        total_passes = len(passes)
        on_event({
            "type": "start",
            "job_id": job_id,
            "total_passes": total_passes,
            "cwd": str(cwd),
            "output_name": out_name,
        })

        t0 = time.time()
        last_lines: list[str] = []

        for pass_idx, raw_pass in enumerate(passes, 1):
            with self._lock:
                if job_id in self._cancelled:
                    on_event({"type": "cancelled", "job_id": job_id})
                    return

            argv = _adapt_argv_for_os(shlex.split(raw_pass, posix=True))
            on_event({
                "type": "pass",
                "job_id": job_id,
                "pass": pass_idx,
                "total_passes": total_passes,
            })

            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(cwd),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                )
            except Exception as exc:
                on_event({"type": "error", "job_id": job_id, "message": f"Failed to launch ffmpeg: {exc}"})
                return

            with self._lock:
                self._active_procs[job_id] = proc

            # FFmpeg updates stderr progress with \r or \n — read char-buffered chunks
            buf = ""
            assert proc.stderr is not None
            while True:
                ch = proc.stderr.read(1)
                if not ch:
                    break
                if ch in ("\r", "\n"):
                    line = buf.strip()
                    buf = ""
                    if not line:
                        continue
                    last_lines.append(line)
                    if len(last_lines) > 25:
                        last_lines.pop(0)

                    m_time = TIME_RE.search(line)
                    if m_time:
                        h, m, s = int(m_time.group(1)), int(m_time.group(2)), float(m_time.group(3))
                        cur_sec = h * 3600 + m * 60 + s
                        m_spd = SPEED_RE.search(line)
                        speed = m_spd.group(1) if m_spd else None
                        pct = None
                        if duration_sec and duration_sec > 0:
                            pass_frac = min(1.0, cur_sec / duration_sec)
                            overall = ((pass_idx - 1) + pass_frac) / total_passes
                            pct = round(overall * 100.0, 1)
                        on_event({
                            "type": "progress",
                            "job_id": job_id,
                            "pass": pass_idx,
                            "total_passes": total_passes,
                            "current_sec": round(cur_sec, 1),
                            "percent": pct,
                            "speed": speed,
                            "elapsed_sec": round(time.time() - t0, 1),
                        })
                else:
                    buf += ch

            rc = proc.wait()
            with self._lock:
                self._active_procs.pop(job_id, None)
                was_cancelled = job_id in self._cancelled

            if was_cancelled:
                on_event({"type": "cancelled", "job_id": job_id})
                return
            if rc != 0:
                tail = "\n".join(last_lines[-6:])
                on_event({
                    "type": "error",
                    "job_id": job_id,
                    "message": f"FFmpeg exited with code {rc}.\n{tail}",
                })
                return

        elapsed = round(time.time() - t0, 1)
        out_path = (cwd / out_name) if out_name else None
        out_info: dict[str, Any] = {
            "type": "done",
            "job_id": job_id,
            "elapsed_sec": elapsed,
            "cwd": str(cwd),
            "output_path": str(out_path) if out_path and out_path.exists() else None,
            "output_name": out_name,
        }
        if out_path and out_path.is_file():
            out_bytes = out_path.stat().st_size
            out_info["output_size_bytes"] = out_bytes
            out_info["output_size_human"] = format_bytes(out_bytes)
            if input_size_bytes and input_size_bytes > 0:
                delta_pct = round(((out_bytes - input_size_bytes) / input_size_bytes) * 100.0, 1)
                out_info["size_delta_pct"] = delta_pct
            try:
                out_info["probe"] = probe_media(out_path)
            except Exception:
                pass
        on_event(out_info)
