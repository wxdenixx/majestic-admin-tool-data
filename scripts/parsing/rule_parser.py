"""
Line-based parser that converts a forum post's plain text into a list of
`Rule` objects compatible with the main tool's RulesBundle schema.

Algorithm:

  1. ``_normalise_lines`` — strip NBSP, normalise whitespace, split on \\n.
  2. ``_preprocess`` — repair common forum quirks (rule numbers split across
     lines, e.g. "2.\\n4 Запрещено..." → "2.4 Запрещено...").
  3. Single-pass state machine over normalised lines.

State machine (per-rule):
  - **rule_text** (default): looking for `RULE_FULL_RE`.
  - When a `|` line is seen we enter **collect_punishment** mode and gather
    every subsequent non-marker line into the punishment buffer until the
    next rule / section / note marker.
  - **note** mode kicks in after Примечание / Пояснение / Исключение /
    Пример headers; subsequent continuation lines are appended to the
    matching field on the **current** rule (NOT the next one).

See docs/FORUM_SCRAPER_SPEC.md in the main tool repo for the wider context.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from .linking import parse_punishment_tokens, resolve_links

# ---- regex catalogue -----------------------------------------------------

# "1.2 Текст" or "1.2.3 Текст". Requires non-empty text after the number.
RULE_FULL_RE = re.compile(r"^\s*(\d+)\.(\d+(?:\.\d+)?)\.?\s+(\S.+)$")

# Rule number on its own line ("1.3" or "1.3.").
RULE_NUMBER_ONLY_RE = re.compile(r"^\s*(\d+\.\d+(?:\.\d+)?)\.?\s*$")

# Lonely integer-with-dot ("2.") that the forum sometimes puts on its
# own line right before the decimal portion of the next rule.
INT_DOT_ONLY_RE = re.compile(r"^\s*(\d+)\.\s*$")

# Section header: integer + dot + non-digit text (so it doesn't conflict
# with a rule like "1.5 …").
SECTION_RE = re.compile(r"^\s*(\d+)\.\s+(\D.{2,})$")

# Stand-alone punishment line ("| Mute 30 минут.").
PUNISHMENT_LINE_RE = re.compile(r"^\s*\|\s*(.+)$")

# Inline punishment delimiter inside a rule line.
INLINE_PUNISHMENT_RE = re.compile(r"\s*\|\s*(.+)$")

# Note headers: capture (header, body_on_same_line).
NOTE_HEADER_RE = re.compile(
    r"^\s*(Примечание|Пояснение|Исключение|Пример)[:\s]\s*(.*)$",
    re.IGNORECASE,
)

NOTE_HEADER_TARGETS: dict[str, str] = {
    "примечание": "explanation",
    "пояснение":  "explanation",
    "исключение": "exception",
    "пример":     "example",
}


# ---- output --------------------------------------------------------------


@dataclass
class ParsedRule:
    id: str
    number: str
    title: str
    text: str
    category: str
    explanation: Optional[str] = None
    examples: list[str] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    punishments: list[dict] = field(default_factory=list)
    linked_punishments: list[dict] = field(default_factory=list)
    related_templates: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source: Optional[str] = None
    status: str = "Active"
    updated_at: str = ""

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "number": self.number,
            "title": self.title,
            "category": self.category,
            "text": self.text,
            "explanation": self.explanation,
            "examples": self.examples,
            "exceptions": self.exceptions,
            "punishments": self.punishments,
            "linkedPunishments": self.linked_punishments,
            "relatedTemplates": self.related_templates,
            "tags": self.tags,
            "source": self.source,
            "status": self.status,
            "updatedAt": self.updated_at,
        }


# ---- main entry ----------------------------------------------------------


def parse_rules_from_text(
    text: str,
    section_id: str,
    category: str,
    source_url: str,
) -> list[ParsedRule]:
    now_iso = datetime.now(timezone.utc).isoformat()
    raw_lines = list(_normalise_lines(text))
    lines = _preprocess(raw_lines)

    rules: list[ParsedRule] = []
    current: Optional[ParsedRule] = None
    punishment_buffer: list[str] = []
    note_target: Optional[str] = None

    def finalize_punishment() -> None:
        """Flush punishment_buffer into current rule's punishments arrays."""
        nonlocal punishment_buffer
        if current is None or not punishment_buffer:
            punishment_buffer = []
            return
        joined = " ".join(punishment_buffer).strip().rstrip(".").strip()
        if joined:
            current.punishments.extend(parse_punishment_tokens(joined))
            current.linked_punishments.extend(resolve_links(joined))
        punishment_buffer = []

    def emit() -> None:
        nonlocal current, note_target
        finalize_punishment()
        if current is not None:
            rules.append(current)
        current = None
        note_target = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        # Standalone punishment line ("| Mute 30 минут.")
        if m := PUNISHMENT_LINE_RE.match(line):
            if current is not None:
                # Replace any previous chunk with this fresh punishment.
                finalize_punishment()
                punishment_buffer = [m.group(1).strip()]
                note_target = None
            continue

        # Full rule line — emits the previous one, starts a new one.
        if m := RULE_FULL_RE.match(line):
            emit()
            section_num, decimal, body = m.group(1), m.group(2), m.group(3).strip()
            number = f"{section_num}.{decimal}"

            inline_punishment = None
            if pm := INLINE_PUNISHMENT_RE.search(body):
                inline_punishment = pm.group(1).strip()
                body = INLINE_PUNISHMENT_RE.sub("", body).rstrip()

            body = body.rstrip(".").strip()
            current = ParsedRule(
                id=f"{section_id}.{number}",
                number=number,
                title=_short_title(body),
                text=body,
                category=category,
                tags=_derive_tags(body),
                source=f"forum:{section_id}:{source_url}",
                updated_at=now_iso,
            )
            if inline_punishment:
                punishment_buffer = [inline_punishment]
            note_target = None
            continue

        # Section header — emits current rule, resets context.
        if m := SECTION_RE.match(line):
            emit()
            continue

        # Note header (Примечание / Пояснение / Исключение / Пример).
        if m := NOTE_HEADER_RE.match(line):
            finalize_punishment()
            kind = m.group(1).lower()
            body = m.group(2).strip()
            note_target = NOTE_HEADER_TARGETS.get(kind)
            if current is not None and note_target and body:
                _attach_note(current, note_target, body)
            continue

        # Continuation line: append to whichever buffer is open.
        if current is None:
            continue
        if punishment_buffer:
            # Skip pure decoration like "—" or "."
            stripped = line.strip(" .—–-")
            if stripped:
                punishment_buffer.append(stripped)
        elif note_target:
            _attach_note(current, note_target, line)
        # else: orphan inside rule body — ignored on purpose. Forum post
        # often has bullet enumerations that are not part of rule semantics.

    emit()
    return rules


# ---- helpers -------------------------------------------------------------


def _normalise_lines(text: str) -> Iterable[str]:
    """Collapse whitespace runs, strip NBSP, split on any line break."""
    text = (
        text.replace("\xa0", " ")
            .replace("\u2028", "\n")
            .replace("\u2029", "\n")
    )
    for line in text.splitlines():
        yield re.sub(r"[ \t]+", " ", line)


def _preprocess(lines: list[str]) -> list[str]:
    """
    Heal accidental line splits in rule numbers caused by inline <br>:

        "1.3"           "1.3 Текст…"
        "Текст…"   →    (lines merged)

        "2."            "2.4 Текст…"
        "4 Текст…"      (lines merged into one rule line)

        "2."            stays as-is (next line isn't a rule sub-number)
        "Раздел…"       so it's treated as a section header.
    """
    result: list[str] = []
    i = 0
    n = len(lines)

    def next_non_empty(idx: int) -> int:
        j = idx + 1
        while j < n and not lines[j].strip():
            j += 1
        return j if j < n else -1

    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # Case 1: just a decimal rule number on its own ("1.3")
        if m := RULE_NUMBER_ONLY_RE.match(line):
            j = next_non_empty(i)
            if j != -1:
                nxt = lines[j].strip()
                # Merge unless the next line looks like a marker itself
                if (not RULE_NUMBER_ONLY_RE.match(nxt)
                        and not RULE_FULL_RE.match(nxt)
                        and not SECTION_RE.match(nxt)
                        and not NOTE_HEADER_RE.match(nxt)
                        and not PUNISHMENT_LINE_RE.match(nxt)):
                    result.append(f"{m.group(1)} {nxt}")
                    i = j + 1
                    continue
            result.append(line)
            i += 1
            continue

        # Case 2: lonely "2." followed by a digit-decimal start
        if m := INT_DOT_ONLY_RE.match(line):
            j = next_non_empty(i)
            if j != -1:
                nxt = lines[j].strip()
                if mm := re.match(r"^(\d+)\s+(.+)$", nxt):
                    # Split rule number → merge as "2.4 Текст…"
                    result.append(f"{m.group(1)}.{mm.group(1)} {mm.group(2)}")
                    i = j + 1
                    continue
                # Otherwise it's a real section header, glue them as "N. Title"
                result.append(f"{m.group(1)}. {nxt}")
                i = j + 1
                continue
            result.append(line)
            i += 1
            continue

        result.append(line)
        i += 1

    return result


def _attach_note(rule: ParsedRule, target: str, body: str) -> None:
    body = body.strip()
    if not body:
        return
    if target == "explanation":
        if rule.explanation:
            rule.explanation = f"{rule.explanation} {body}".strip()
        else:
            rule.explanation = body
    elif target == "exception":
        rule.exceptions.append(body)
    elif target == "example":
        rule.examples.append(body)


def _short_title(text: str, limit: int = 90) -> str:
    """First sentence (truncated to `limit` chars) — never empty."""
    m = re.search(r"[.!?]", text)
    first = text[: m.start()] if m else text
    if len(first) > limit:
        first = first[:limit].rstrip() + "…"
    return first.strip() or text[:limit].strip() or "untitled"


_STOPWORDS = {
    "это", "что", "как", "или", "для", "при", "если", "все", "без",
    "той", "этом", "быть", "его", "был", "там", "оно", "она",
    "в", "и", "на", "не", "с", "по", "за", "от", "до", "из", "к", "у", "о",
    "также", "только", "ещё", "уже", "тоже", "может", "своего", "своих",
    "своему", "которые", "которое", "которая", "который", "когда", "тогда",
}

_TAG_RE = re.compile(r"[А-Яа-яA-Za-z]{4,}")


def _derive_tags(text: str) -> list[str]:
    words = [w.lower() for w in _TAG_RE.findall(text)]
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if w in _STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= 6:
            break
    return out
