"""
Soft glossary: maps human-readable punishment type names (as they appear on
the forum — "Demorgan", "WARN", "Выговор лидеру", …) to machine-dispatchable
`Punishment.Id` values that exist in the main tool's `punishments.default.json`.

Unknown types produce empty `linkedPunishments` so the tool UI simply doesn't
render a dispatch card for them. The admin can add missing punishment types
(e.g. a brand new /demorgan command) via PR to the main repo and extend this
glossary accordingly.
"""
from __future__ import annotations

import re
from typing import Optional

# Key = lowercase first word of the punishment text.
# Value = stable id that MUST exist in the main tool's punishments.default.json.
PUNISHMENT_ID_BY_NAME: dict[str, str] = {
    # known in the tool out-of-the-box:
    "warn":           "punishment_warn",
    "предупреждение": "punishment_warn",
    "mute":           "punishment_mute",
    "мут":            "punishment_mute",
    "kick":           "punishment_kick",
    "кик":            "punishment_kick",
    "jail":           "punishment_jail",
    "jailed":         "punishment_jail",
    "ban":            "punishment_ban",
    "бан":            "punishment_ban",

    # TODO(tool): these IDs do not yet exist in punishments.default.json.
    # Add them before enabling auto-link production:
    # "demorgan":       "punishment_demorgan",
    # "выговор":        "punishment_warn_leader",
    # "снятие":         "punishment_demote_leader",
}


DURATION_RE = re.compile(
    r"""(\d+(?:\s*[-–—]\s*\d+)?)   # "15" or "15-35"
        \s*
        (минут[а-я]*|час[а-я]*|день|дн[а-я]*|сут[а-я]*|лет|год[а-я]*|m|h|d)
    """,
    re.VERBOSE | re.IGNORECASE,
)


def parse_punishment_tokens(raw: Optional[str]) -> list[dict]:
    """
    Break raw text like "Demorgan 15-35 минут / WARN" into structured dicts
    suitable for the `Rule.punishments` array.
    """
    if not raw:
        return []

    raw = raw.strip().rstrip(".")
    variants = [v.strip() for v in raw.split("/") if v.strip()]

    out: list[dict] = []
    for v in variants:
        kind = _first_significant_word(v)
        duration_match = DURATION_RE.search(v)
        out.append({
            "type": kind,
            "duration": duration_match.group(0).strip() if duration_match else None,
            "reason": None,
        })
    return out


def resolve_links(raw: Optional[str]) -> list[dict]:
    """
    Produce `linkedPunishments` entries for items whose type is present in
    PUNISHMENT_ID_BY_NAME. Unknown kinds are silently dropped.
    """
    if not raw:
        return []

    links: list[dict] = []
    for token in parse_punishment_tokens(raw):
        kind_lower = token["type"].strip().lower()
        pid = PUNISHMENT_ID_BY_NAME.get(kind_lower)
        if not pid:
            # try first word of multi-word kind ("выговор лидеру" → "выговор")
            first = kind_lower.split()[0] if kind_lower else ""
            pid = PUNISHMENT_ID_BY_NAME.get(first)
        if not pid:
            continue
        links.append({
            "punishmentId": pid,
            "presetDuration": token.get("duration"),
            "presetReason": None,
            "note": "Авто по форуму",
        })
    return links


def _first_significant_word(text: str) -> str:
    """
    Extract the punishment TYPE from a punishment variant.
    "Demorgan 15 минут" -> "Demorgan"
    "Выговор лидеру"     -> "Выговор лидеру"
    "WARN"               -> "WARN"
    """
    text = text.strip()
    # strip trailing punctuation
    text = re.sub(r"[\.,;:!?\s]+$", "", text)

    # If the text has a digit in it, cut everything from the first digit.
    m = re.search(r"\d", text)
    if m:
        text = text[: m.start()].strip()

    return text or "unknown"
