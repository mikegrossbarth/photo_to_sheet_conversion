from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path


DATA_PATH = Path(__file__).resolve().parent / "player_sport_data.json"


def normalize_name(value: str) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9\s'.-]", " ", text)
    text = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", text)
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=1)
def load_players() -> dict:
    if not DATA_PATH.exists():
        return {}
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    return data.get("players", {})


def lookup_sport(player: str, label_text: str = "") -> str:
    players = load_players()
    normalized = normalize_name(player)
    if normalized in players:
        return str(players[normalized].get("sport", "") or "").upper()

    label_normalized = normalize_name(label_text)
    if normalized and normalized in label_normalized and normalized in players:
        return str(players[normalized].get("sport", "") or "").upper()

    tokens = set(label_normalized.split())
    best_name = ""
    best_sport = ""
    for name, payload in players.items():
        if not name or len(name) < 4:
            continue
        name_tokens = set(name.split())
        if len(name_tokens) < 2:
            continue
        if name_tokens.issubset(tokens) and len(name) > len(best_name):
            best_name = name
            best_sport = str(payload.get("sport", "") or "").upper()
    return best_sport

