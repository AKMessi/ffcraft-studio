"""Media inspection via ffprobe, native OS file/folder pickers, and context formatting."""

from __future__ import annotations

import json
import os
import platform
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any


def format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    for unit in ("KB", "MB", "GB", "TB"):
        num_bytes /= 1024.0
        if num_bytes < 1024.0 or unit == "TB":
            return f"{num_bytes:.1f} {unit}"
    return f"{num_bytes:.1f} TB"


def format_duration_clock(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def format_duration_words(seconds: float) -> str:
    total = max(1, int(round(seconds)))
    if total < 60:
        return f"{total} seconds"
    m, s = divmod(total, 60)
    if m < 60:
        return f"{m}m {s}s" if s else f"{m} minutes"
    h, rem_m = divmod(m, 60)
    return f"{h}h {rem_m}m {s}s" if s else f"{h}h {rem_m}m"


def probe_media(file_path: str | Path) -> dict[str, Any]:
    """Run ffprobe on a local media file and return rich structured metadata."""
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    proc = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_format",
            "-show_streams",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr.strip() or 'Unsupported or corrupt media file'}")

    data = json.loads(proc.stdout)
    fmt = data.get("format", {})
    streams = data.get("streams", [])

    v_streams = [s for s in streams if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic", 0) == 0]
    a_streams = [s for s in streams if s.get("codec_type") == "audio"]
    s_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

    size_bytes = path.stat().st_size
    duration_sec = float(fmt.get("duration") or 0.0)
    if duration_sec <= 0 and v_streams:
        try:
            duration_sec = float(v_streams[0].get("duration") or 0.0)
        except (TypeError, ValueError):
            duration_sec = 0.0

    width = height = None
    fps = None
    vcodec = None
    pix_fmt = None
    if v_streams:
        vs = v_streams[0]
        width = vs.get("width")
        height = vs.get("height")
        vcodec = vs.get("codec_name")
        pix_fmt = vs.get("pix_fmt")
        rate = vs.get("avg_frame_rate") or vs.get("r_frame_rate") or "0/1"
        try:
            fval = float(Fraction(rate))
            if 0 < fval < 1000:
                fps = round(fval, 2)
        except Exception:
            fps = None

    acodec = None
    channels = None
    sample_rate = None
    if a_streams:
        aus = a_streams[0]
        acodec = aus.get("codec_name")
        channels = aus.get("channels")
        sample_rate = aus.get("sample_rate")

    return {
        "path": str(path),
        "dir": str(path.parent),
        "filename": path.name,
        "ext": path.suffix.lstrip(".").lower(),
        "size_bytes": size_bytes,
        "size_human": format_bytes(size_bytes),
        "size_mb": round(size_bytes / (1024 * 1024), 2),
        "duration_sec": round(duration_sec, 2),
        "duration_clock": format_duration_clock(duration_sec) if duration_sec > 0 else None,
        "duration_words": format_duration_words(duration_sec) if duration_sec > 0 else None,
        "has_video": bool(v_streams),
        "has_audio": bool(a_streams),
        "video_streams": len(v_streams),
        "audio_streams": len(a_streams),
        "subtitle_streams": len(s_streams),
        "width": width,
        "height": height,
        "fps": fps,
        "vcodec": vcodec,
        "pix_fmt": pix_fmt,
        "acodec": acodec,
        "channels": channels,
        "sample_rate": sample_rate,
    }


def build_media_prompt_prefix(meta: dict[str, Any]) -> str:
    """Create a concise, model-friendly context note so FFCraft-1.5B knows exact filename & duration."""
    fname = meta["filename"]
    quoted = f'"{fname}"' if " " in fname else fname
    parts = [f"file={quoted}", f"size={meta['size_human']}"]
    if meta.get("duration_words"):
        parts.append(f"duration={meta['duration_words']} ({int(round(meta['duration_sec']))} seconds)")
    if meta.get("width") and meta.get("height"):
        res = f"{meta['width']}x{meta['height']}"
        if meta.get("fps"):
            fps_disp = int(meta["fps"]) if float(meta["fps"]).is_integer() else meta["fps"]
            res += f" {fps_disp}fps"
        parts.append(f"video={res} {meta.get('vcodec') or ''}".strip())
    if meta.get("has_audio"):
        parts.append(f"audio={meta.get('acodec') or 'yes'} ({meta.get('audio_streams', 1)} track)")
    else:
        parts.append("audio=none")
    if meta.get("subtitle_streams", 0) > 0:
        parts.append(f"subtitles={meta['subtitle_streams']} track(s)")
    return f"[Attached local media: {', '.join(parts)}]\n"


def pick_media_file_native() -> str | None:
    """Open a native OS file picker and return the chosen absolute file path."""
    sys_name = platform.system().lower()
    if sys_name == "windows":
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$f = New-Object System.Windows.Forms.OpenFileDialog; "
            "$f.Title = 'Select a video, audio, or image file for FFCraft Studio'; "
            "$f.Filter = 'Media Files|*.mp4;*.mkv;*.mov;*.avi;*.webm;*.flv;*.ts;*.m4v;*.mp3;*.wav;*.flac;*.m4a;*.opus;*.ogg;*.aac;*.gif;*.webp;*.png;*.jpg;*.srt|All Files|*.*'; "
            "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $f.FileName }"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps_script],
            capture_output=True,
            text=True,
        )
        chosen = proc.stdout.strip()
        return chosen or None

    if sys_name == "darwin":
        proc = subprocess.run(
            ["osascript", "-e", 'POSIX path of (choose file with prompt "Select media file for FFCraft Studio")'],
            capture_output=True,
            text=True,
        )
        chosen = proc.stdout.strip()
        return chosen or None

    # Linux / fallback using tkinter
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askopenfilename(title="Select media file for FFCraft Studio")
        root.destroy()
        return chosen or None
    except Exception:
        return None


def pick_folder_native() -> str | None:
    """Open a native OS folder picker (used when running a command without an attached file)."""
    sys_name = platform.system().lower()
    if sys_name == "windows":
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$d.Description = 'Select the folder containing your media files'; "
            "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps_script],
            capture_output=True,
            text=True,
        )
        chosen = proc.stdout.strip()
        return chosen or None

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        chosen = filedialog.askdirectory(title="Select folder containing your media files")
        root.destroy()
        return chosen or None
    except Exception:
        return None


def reveal_in_file_manager(path: str | Path) -> None:
    """Open Explorer / Finder / file manager highlighting the output file."""
    target = Path(path).resolve()
    sys_name = platform.system().lower()
    if sys_name == "windows":
        if target.is_file():
            subprocess.Popen(["explorer", f"/select,{target}"])
        else:
            subprocess.Popen(["explorer", str(target)])
    elif sys_name == "darwin":
        if target.is_file():
            subprocess.Popen(["open", "-R", str(target)])
        else:
            subprocess.Popen(["open", str(target)])
    else:
        folder = target.parent if target.is_file() else target
        subprocess.Popen(["xdg-open", str(folder)])
