"""End-to-end integration test for FFCraft Studio (status, ffprobe, streaming chat, real FFmpeg run)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def http_json(url: str, payload: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def read_sse(url: str, payload: dict, timeout: int = 90) -> list[dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    events: list[dict] = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


def main() -> None:
    port = 8791
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "run.py"), "--no-browser", "--port", str(port)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # 1. Wait for HTTP + llama-server ready
        deadline = time.time() + 45
        st = None
        while time.time() < deadline:
            try:
                st = http_json(f"{base}/api/status", timeout=3)
                if st.get("state") == "ready":
                    break
                if st.get("state") in ("error", "missing_llama", "missing_model"):
                    raise RuntimeError(f"Runtime failed: {st}")
            except urllib.error.URLError:
                pass
            time.sleep(0.5)
        assert st and st["state"] == "ready", f"Server never became ready: {st}"
        print(f"[PASS] /api/status ready | model={st['model_file']} | ffmpeg={st['ffmpeg_found']}")

        # 2. Static frontend assets return 200
        for path in ("/", "/styles.css", "/app.js"):
            with urllib.request.urlopen(f"{base}{path}", timeout=5) as resp:
                assert resp.status == 200 and len(resp.read()) > 500
        print("[PASS] Static UI assets (index.html, styles.css, app.js) served 200 OK")

        # 3. Create a real tiny clip & test /api/file/probe
        with tempfile.TemporaryDirectory(prefix="ffstudio_test_") as tmp:
            clip = Path(tmp) / "gameplay.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=3",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k",
                    str(clip),
                ],
                check=True,
            )
            probe_resp = http_json(f"{base}/api/file/probe", {"path": str(clip)})
            fmeta = probe_resp["file"]
            assert fmeta["filename"] == "gameplay.mp4"
            assert fmeta["width"] == 1280 and fmeta["height"] == 720
            assert fmeta["duration_sec"] >= 2.5
            print(f"[PASS] /api/file/probe -> {fmeta['filename']} {fmeta['width']}x{fmeta['height']} {fmeta['duration_clock']} {fmeta['size_human']}")

            # 4. Streaming /api/chat with attached file context
            chat_events = read_sse(
                f"{base}/api/chat",
                {
                    "messages": [{"role": "user", "content": "downscale this to 480p balanced H.264 MP4"}],
                    "attached_file": fmeta,
                },
            )
            deltas = [e for e in chat_events if e.get("type") == "delta"]
            done_ev = next((e for e in chat_events if e.get("type") == "done"), None)
            assert deltas and done_ev and done_ev.get("command"), f"Bad chat SSE: {chat_events[-3:]}"
            cmd = done_ev["command"]
            print(f"[PASS] /api/chat streamed {done_ev['tokens']} tokens ({done_ev['tokens_per_sec']} t/s)")
            print(f"       CMD: {cmd}")

            # 5. Live /api/run executing the generated FFmpeg command!
            run_events = read_sse(
                f"{base}/api/run",
                {
                    "command": cmd,
                    "working_dir": fmeta["dir"],
                    "duration_sec": fmeta["duration_sec"],
                    "input_size_bytes": fmeta["size_bytes"],
                },
            )
            run_done = next((e for e in run_events if e.get("type") == "done"), None)
            assert run_done and run_done.get("output_path"), f"Run failed: {run_events[-3:]}"
            out_probe = run_done.get("probe") or {}
            assert out_probe.get("height") == 480, f"Expected 480p output, got {out_probe}"
            print(
                f"[PASS] /api/run executed cleanly in {run_done['elapsed_sec']}s | "
                f"{fmeta['size_human']} -> {run_done['output_size_human']} ({run_done.get('size_delta_pct')}%) | "
                f"verified {out_probe.get('width')}x{out_probe.get('height')} {out_probe.get('vcodec')}"
            )

            # 6. Also test vague prompt -> should ask clarifying questions without file!
            vague_events = read_sse(
                f"{base}/api/chat",
                {"messages": [{"role": "user", "content": "bro my video is way too big, shrink it"}]},
            )
            vague_done = next((e for e in vague_events if e.get("type") == "done"), None)
            assert vague_done and vague_done.get("command") is None and "?" in vague_done.get("full_text", ""), (
                f"Expected clarifying questions on vague prompt, got: {vague_done}"
            )
            print("[PASS] /api/chat vague prompt cleanly asked clarifying questions!")

        print("\nALL 6 E2E INTEGRATION CHECKS PASSED!")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
