from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"
STAMP = ROOT / ".venv" / ".requirements.sha256"
MODULES = {
    "google-genai": "google.genai",
    "python-dotenv": "dotenv",
    "pillow": "PIL",
}


def requirements_hash() -> str:
    if not REQUIREMENTS.exists():
        return ""
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def main() -> int:
    for _package, module in MODULES.items():
        if importlib.util.find_spec(module) is None:
            return 1

    expected = requirements_hash()
    if expected:
        if not STAMP.exists() or STAMP.read_text(encoding="utf-8").strip() != expected:
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

