#!/usr/bin/env python3
"""
scrape_rules.py — fetches every forum thread listed in config.yml, parses
the first post into Rule[] and writes rules/{section.id}.json.

Usage:
    python scripts/scrape_rules.py                   # all sections
    python scripts/scrape_rules.py --section general # one section
    python scripts/scrape_rules.py --dry-run         # don't write files

Exit code 0 on success, non-zero on guardrail violation (see config.yml).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from parsing.rule_parser import parse_rules_from_text, ParsedRule  # noqa: E402
from solver.cookie_solver import fetch_all, FetchResult  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(__file__).resolve().parent / "config.yml"

log = logging.getLogger("scrape_rules")


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def extract_first_post_text(html: str) -> str:
    """
    XenForo threads keep the original post in the FIRST `article.message`.
    Inside the article, the rule text is in `.bbWrapper`. We take the
    plain-text content so the line-based parser can do its job.
    """
    soup = BeautifulSoup(html, "html.parser")
    article = soup.select_one("article.message .bbWrapper")
    if not article:
        # fallback: any message-body on the page
        article = soup.select_one("article .bbWrapper") or soup.select_one(".bbWrapper")
    if not article:
        return ""
    return article.get_text(separator="\n", strip=False)


def build_bundle(
    rules: list[ParsedRule],
    section_id: str,
    section_title: str,
) -> dict:
    now = datetime.now(timezone.utc)
    version = now.strftime("%Y.%m.%d.%H%M")
    return {
        "version": version,
        "releaseDate": now.isoformat(),
        "author": "scrape_rules.py",
        "compatibilityVersion": "1",
        "changelog": [
            {
                "version": version,
                "date": now.isoformat(),
                "changes": [f"Автоматический снимок раздела «{section_title}»"],
            }
        ],
        "rules": [r.to_json() for r in rules],
    }


def enforce_guardrails(
    rules: list[ParsedRule],
    section_cfg: dict,
    global_cfg: dict,
) -> None:
    """Raises SystemExit with non-zero code if the scrape output looks broken."""
    min_rules = global_cfg.get("guardrails", {}).get("min_rules_per_section", 3)
    max_empty = global_cfg.get("guardrails", {}).get("max_empty_text_ratio", 0.3)

    if len(rules) < min_rules:
        log.error(
            "Раздел %s: получено только %d правил (минимум %d). Парсер сломался?",
            section_cfg["id"], len(rules), min_rules,
        )
        raise SystemExit(2)

    empty = sum(1 for r in rules if not r.text.strip())
    if empty / max(1, len(rules)) > max_empty:
        log.error(
            "Раздел %s: %d из %d правил пустые — парсер сломался",
            section_cfg["id"], empty, len(rules),
        )
        raise SystemExit(3)


async def scrape(sections: Iterable[dict], cfg: dict) -> dict[str, dict]:
    """Returns {section_id: rules_bundle_dict}."""
    base = cfg["base_url"].rstrip("/")
    urls = [base + s["url"] for s in sections]

    results = await fetch_all(
        urls=urls,
        user_agent=cfg["user_agent"],
        timeout_seconds=cfg.get("timeout_seconds", 60),
        wait_selector=cfg.get("challenge_wait_selector", "article.message .bbWrapper"),
        delay_between_seconds=2.0,
    )
    by_url = {r.url: r for r in results}

    output: dict[str, dict] = {}
    for section in sections:
        full_url = base + section["url"]
        fetch_result: FetchResult | None = by_url.get(full_url)
        if fetch_result is None:
            log.error("no fetch result for %s", full_url)
            raise SystemExit(10)

        text = extract_first_post_text(fetch_result.html)
        if not text.strip():
            log.error("empty body for section %s (%s)", section["id"], full_url)
            (REPO_ROOT / "debug-html").mkdir(exist_ok=True)
            (REPO_ROOT / "debug-html" / f"{section['id']}.html").write_text(
                fetch_result.html, encoding="utf-8"
            )
            raise SystemExit(11)

        rules = parse_rules_from_text(
            text=text,
            section_id=section["id"],
            category=section["category"],
            source_url=full_url,
        )
        enforce_guardrails(rules, section, cfg)

        output[section["id"]] = build_bundle(rules, section["id"], section["title"])
        log.info("section %s: parsed %d rules", section["id"], len(rules))

    return output


def save_bundles(bundles: dict[str, dict], rules_dir: Path, dry_run: bool) -> None:
    rules_dir.mkdir(parents=True, exist_ok=True)
    for section_id, bundle in bundles.items():
        target = rules_dir / f"{section_id}.json"
        body = json.dumps(bundle, ensure_ascii=False, indent=2) + "\n"
        if dry_run:
            log.info("[dry-run] would write %s (%d bytes)", target, len(body))
            continue
        # LF-forced write (see build_manifest.py comment) — lets us run the
        # scraper locally on Windows without invalidating the manifest SHAs.
        target.write_bytes(body.encode("utf-8"))
        log.info("wrote %s (%d bytes, %d rules)",
                 target.relative_to(REPO_ROOT), len(body), len(bundle["rules"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--section", help="only scrape one section id (e.g. 'general')")
    parser.add_argument("--dry-run", action="store_true", help="don't write files")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    cfg = load_config()
    sections = cfg["sections"]
    if args.section:
        sections = [s for s in sections if s["id"] == args.section]
        if not sections:
            log.error("Unknown section id: %s", args.section)
            return 1

    log.info("scraping %d section(s)", len(sections))
    bundles = asyncio.run(scrape(sections, cfg))

    rules_dir = REPO_ROOT / cfg["output"].get("rules_dir", "rules")
    save_bundles(bundles, rules_dir, args.dry_run)

    total = sum(len(b["rules"]) for b in bundles.values())
    log.info("done. total rules: %d", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
