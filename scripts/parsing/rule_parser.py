"""
Line-based parser that converts a forum post's plain text into a list of
`Rule` objects compatible with the main tool's RulesBundle schema.

The parser is deliberately text-oriented (we do `BeautifulSoup.get_text()`
once and then walk lines) because XenForo forum rarely keeps a stable
element-level structure between threads, but the HUMAN text layout is
remarkably consistent:

    1. Общее положение                 ← section header
    1.1 <text>.                        ← rule without punishment (inherits)
    1.2 <text>. | <punishment>         ← rule with inline punishment
    Примечание: <text>                 ← becomes `explanation` of NEXT rule
    Исключение: <text>                 ← becomes `exceptions` of NEXT rule

See docs/FORUM_SCRAPER_SPEC.md in the main repo for the full specification.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from .linking import parse_punishment_tokens, resolve_links

# ---- regexes --------------------------------------------------------------

# "1. Общее положение" / "12. Правила …"
SECTION_RE = re.compile(r"^\s*(\d+)\.\s+([^\d].+)$")

# "1.1 текст …" or "1.1. текст …"  (dot after second number is optional)
# Capture: group(1) = "1.1", group(2) = text (incl. optional " | punishment").
RULE_RE = re.compile(r"^\s*(\d+\.\d+(?:\.\d+)?)\.?\s+(.+)$")

NOTE_RE = re.compile(r"^\s*Примечание[:\s]\s*(.+)$", re.IGNORECASE)
EXCEPTION_RE = re.compile(r"^\s*Исключение[:\s]\s*(.+)$", re.IGNORECASE)

# Matches optional tail of a rule: "… | Demorgan 15 минут / WARN"
PUNISHMENT_DELIM = re.compile(r"\s*\|\s*(.+)$")


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
    """
    Walk each line of the forum post and emit one ParsedRule per `N.N` match.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    current_section_title: Optional[str] = None
    pending_note: Optional[str] = None
    pending_exception: Optional[str] = None

    rules: list[ParsedRule] = []

    for raw_line in _normalise_lines(text):
        line = raw_line.strip()
        if not line:
            continue

        # PRIORITY: rule (N.N) > note > exception > section (N.)
        if m := RULE_RE.match(line):
            number = m.group(1)
            rest = m.group(2).strip()

            punishment_raw: Optional[str] = None
            if pm := PUNISHMENT_DELIM.search(rest):
                punishment_raw = pm.group(1).strip()
                rest = PUNISHMENT_DELIM.sub("", rest).rstrip()

            rest = rest.rstrip(".").strip()
            title = _short_title(rest)

            rule = ParsedRule(
                id=f"{section_id}.{number}",
                number=number,
                title=title,
                text=rest,
                category=category,
                explanation=pending_note,
                exceptions=[pending_exception] if pending_exception else [],
                punishments=parse_punishment_tokens(punishment_raw),
                linked_punishments=resolve_links(punishment_raw),
                tags=_derive_tags(title + " " + rest),
                source=f"forum:{section_id}:{source_url}",
                updated_at=now_iso,
            )
            rules.append(rule)

            pending_note = None
            pending_exception = None
            continue

        if m := NOTE_RE.match(line):
            # Note applies to the NEXT rule or to the PREVIOUS one if
            # nothing follows. Heuristic: attach to previous when no rule
            # came in the last 4 lines. For MVP we attach to next.
            pending_note = m.group(1).strip()
            continue

        if m := EXCEPTION_RE.match(line):
            pending_exception = m.group(1).strip()
            continue

        if m := SECTION_RE.match(line):
            current_section_title = m.group(2).strip()
            continue

        # orphan line — append to previous rule's text if exists
        if rules and len(line) > 10 and not line[0].isdigit():
            rules[-1].text = (rules[-1].text + " " + line).strip()

    # attach last pending note/exception to the last rule as fallback
    if rules and pending_note and not rules[-1].explanation:
        rules[-1].explanation = pending_note
    if rules and pending_exception:
        rules[-1].exceptions.append(pending_exception)

    return rules


# ---- helpers -------------------------------------------------------------


def _normalise_lines(text: str) -> Iterable[str]:
    """Collapse repeated whitespace, strip NBSP, split on any line break."""
    text = text.replace("\xa0", " ").replace("\u2028", "\n").replace("\u2029", "\n")
    for line in text.splitlines():
        yield re.sub(r"[ \t]+", " ", line)


def _short_title(text: str, limit: int = 90) -> str:
    """
    Use the first sentence (up to punctuation) or the first `limit` chars.
    """
    # first sentence
    m = re.search(r"[.!?]", text)
    first = text[: m.start()] if m else text
    if len(first) > limit:
        first = first[:limit].rstrip() + "…"
    return first.strip() or text[:limit]


_STOPWORDS = {
    "это", "что", "как", "или", "для", "при", "если", "все", "без",
    "той", "этом", "быть", "его", "был", "там", "оно", "она",
    "в", "и", "на", "не", "с", "по", "за", "от", "до", "из", "к", "у", "о",
}

_TAG_RE = re.compile(r"[А-Яа-яA-Za-z]{4,}")


def _derive_tags(text: str) -> list[str]:
    """
    Primitive keyword extraction: take words ≥4 chars that look rare.
    The main tool's search already does substring-matching, so we don't
    need heavy NLP here — tags are just a "bag of words" hint.
    """
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
