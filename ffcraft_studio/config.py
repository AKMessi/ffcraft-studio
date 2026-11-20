"""Configuration, model discovery, and canonical FFCraft-1.5B prompt."""

from __future__ import annotations

import os
import platform
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT_DIR / "web"
LOCAL_MODELS_DIR = ROOT_DIR / "models"

HF_REPO_ID = "AKMESSI/FFCraft-1.5B-GGUF"
DEFAULT_GGUF_NAME = "ffcraft-1.5b-v2.Q8_0.gguf"
HF_DOWNLOAD_URL = f"https://huggingface.co/{HF_REPO_ID}/resolve/main/{DEFAULT_GGUF_NAME}"

MODEL_ALIAS = "ffcraft-1.5b"
DEFAULT_APP_PORT = int(os.environ.get("FFCRAFT_PORT", "8765"))
DEFAULT_LLM_PORT = int(os.environ.get("FFCRAFT_LLM_PORT", "18080"))

SYSTEM_PROMPT = (
    "You are an expert FFmpeg assistant for everyday users and pros alike. "
    "Convert plain-English requests into the best, production-ready FFmpeg command.\n"
    "Rules:\n"
    "- If the request has enough detail, reply immediately with the optimal command inside a "
    "```bash code block (use ```powershell or ```cmd if the user specifies Windows) followed by "
    "a 1-2 line plain-English note.\n"
    "- Ask 1-3 short, plain-English clarifying questions ONLY when missing details would "
    "materially change the command (such as missing video duration for a strict file-size cap, "
    "unclear compatibility vs. smallest-size priority, or instant lossless copy vs. re-encode). "
    "Never ask for filenames or technical defaults.\n"
    "- Follow FFmpeg best practices: stream copy (-c copy) when re-encoding is unnecessary, "
    "universal playback defaults (libx264, -pix_fmt yuv420p, -movflags +faststart) unless "
    "another codec fits better, even scaling dimensions (-2), palettegen/paletteuse for GIFs, "
    "and exact 2-pass VBR bitrate math for strict file-size limits."
)


def detect_os_label() -> str:
    sys_name = platform.system().lower()
    if sys_name == "windows":
        return "Windows"
    if sys_name == "darwin":
        return "macOS"
    return "Linux"


def find_local_model() -> Path | None:
    """Locate FFCraft-1.5B GGUF across standard local folders (zero unnecessary duplication)."""
    env_path = os.environ.get("FFCRAFT_MODEL_PATH")
    if env_path:
        p = Path(env_path).expanduser()
        if p.is_file():
            return p

    search_candidates = [
        LOCAL_MODELS_DIR / DEFAULT_GGUF_NAME,
        ROOT_DIR.parent / "model" / "ffcraft" / DEFAULT_GGUF_NAME,
        ROOT_DIR.parent / "hf_release" / "FFCraft-1.5B-GGUF" / DEFAULT_GGUF_NAME,
        Path.home() / ".cache" / "ffcraft-studio" / DEFAULT_GGUF_NAME,
    ]
    for candidate in search_candidates:
        if candidate.is_file():
            return candidate

    # Fallback: any ffcraft *.gguf inside ./models/
    if LOCAL_MODELS_DIR.is_dir():
        for gguf in sorted(LOCAL_MODELS_DIR.glob("*.gguf")):
            return gguf

    return None


def download_target_path() -> Path:
    LOCAL_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    return LOCAL_MODELS_DIR / DEFAULT_GGUF_NAME
