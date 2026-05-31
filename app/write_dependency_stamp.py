from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
requirements = ROOT / "requirements.txt"
stamp = ROOT / ".venv" / ".requirements.sha256"

stamp.parent.mkdir(parents=True, exist_ok=True)
stamp.write_text(hashlib.sha256(requirements.read_bytes()).hexdigest(), encoding="utf-8")

