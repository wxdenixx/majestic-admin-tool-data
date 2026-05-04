#!/usr/bin/env python3
"""
scrape_items.py — fetches the full items catalogue from wiki.majestic-rp.ru
and writes it to items/all.json.

The wiki is server-side-rendered so we don't need Playwright; a plain
`requests` session with BeautifulSoup is enough and runs in under a minute.

Schema of the output file matches what the main tool's DataSyncService
expects when package.kind == "items":

    {
      "version": "YYYY.MM.DD",
      "source":  "wiki.majestic-rp.ru",
      "generatedAt": "<iso>",
      "categories": [
        {
          "slug": "tool",
          "title": "Инструменты",
          "items": [
            {
              "id":         "tool/1110",
              "name":       "Грабли",
              "description": "…",
              "url":         "https://wiki.majestic-rp.ru/ru/items/tool/1110"
            }
          ]
        }
      ]
    }

Unknown item structure is tolerated; unrecognised fields are dropped.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_URL = "https://wiki.majestic-rp.ru"
ITEMS_INDEX = "/ru/items"
SITEMAP = "/sitemap.xml"

log = logging.getLogger("scrape_items")

USER_AGENT = "majestic-admin-tool-scraper/0.4 (+https://github.com/wxdenixx/majestic-admin-tool-data)"

# slug → human-readable category name
CATEGORY_TITLES: dict[str, str] = {
    "tool": "Инструменты",
    "ammunition": "Боеприпасы",
    "food": "Еда",
    "drink": "Напитки",
    "fishing": "Рыбалка",
    "farming": "Сельское хозяйство",
    "weapon": "Оружие",
    "attachment": "Модификации оружия",
    "medicine": "Медикаменты",
    "material": "Материалы",
    "clothing": "Одежда",
    "accessory": "Аксессуары",
    "furniture": "Мебель",
    "document": "Документы",
    "quest": "Квестовые",
    "other": "Прочее",
}


@dataclass
class WikiItem:
    id: str
    category: str
    name: str
    description: str = ""
    url: str = ""


@dataclass
class Category:
    slug: str
    title: str
    items: list[WikiItem] = field(default_factory=list)


def fetch_sitemap_urls(session: requests.Session) -> list[str]:
    """Returns every /ru/items/<slug>/<id> URL found in sitemap.xml."""
    resp = session.get(BASE_URL + SITEMAP, timeout=30)
    resp.raise_for_status()
    # No lxml dependency — for sitemap.xml the default html.parser works
    # for our extraction (we only need <loc> elements).
    soup = BeautifulSoup(resp.content, "html.parser")
    urls = [loc.text.strip() for loc in soup.find_all("loc")]
    item_urls = [u for u in urls if re.search(r"/ru/items/[^/]+/\d+", u)]
    log.info("sitemap: %d total URLs, %d items", len(urls), len(item_urls))
    return item_urls


def parse_item_page(html: str, url: str) -> WikiItem | None:
    soup = BeautifulSoup(html, "html.parser")

    # the wiki is SSR; the main content lives in <main> / <article>
    main = soup.find("main") or soup.find("article") or soup.body
    if main is None:
        return None

    # name — usually <h1>
    h1 = main.find("h1")
    name = h1.get_text(strip=True) if h1 else ""

    # description — first <p> after the h1
    description = ""
    if h1:
        p = h1.find_next("p")
        if p:
            description = p.get_text(" ", strip=True)

    m = re.search(r"/ru/items/([^/]+)/(\d+)", url)
    if not m:
        return None
    category = m.group(1)
    iid = m.group(2)

    return WikiItem(
        id=f"{category}/{iid}",
        category=category,
        name=name or f"item-{iid}",
        description=description,
        url=url,
    )


def group_by_category(items: Iterable[WikiItem]) -> list[Category]:
    by_slug: dict[str, Category] = {}
    for item in items:
        cat = by_slug.setdefault(
            item.category,
            Category(
                slug=item.category,
                title=CATEGORY_TITLES.get(item.category, item.category.title()),
            ),
        )
        cat.items.append(item)
    # stable ordering: known categories first, then unknown alphabetically
    known = list(CATEGORY_TITLES.keys())

    def sort_key(c: Category) -> tuple[int, str]:
        try:
            return (known.index(c.slug), c.slug)
        except ValueError:
            return (len(known) + 1, c.slug)

    categories = sorted(by_slug.values(), key=sort_key)
    for c in categories:
        c.items.sort(key=lambda it: it.name.lower())
    return categories


def scrape(limit: int | None = None) -> list[Category]:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    urls = fetch_sitemap_urls(session)
    if limit:
        urls = urls[:limit]

    items: list[WikiItem] = []
    for i, url in enumerate(urls, 1):
        if i % 25 == 0 or i == len(urls):
            log.info("  fetched %d / %d", i, len(urls))
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            item = parse_item_page(resp.text, url)
            if item:
                items.append(item)
        except Exception as ex:  # noqa: BLE001
            log.warning("failed %s: %s", url, ex)

    log.info("successfully parsed %d items", len(items))
    return group_by_category(items)


def save_bundle(categories: list[Category], items_dir: Path, dry_run: bool) -> None:
    items_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    bundle = {
        "version": now.strftime("%Y.%m.%d"),
        "source": "wiki.majestic-rp.ru",
        "generatedAt": now.isoformat(),
        "categories": [
            {
                "slug": c.slug,
                "title": c.title,
                "items": [
                    {
                        "id": it.id,
                        "name": it.name,
                        "description": it.description,
                        "url": it.url,
                    }
                    for it in c.items
                ],
            }
            for c in categories
        ],
    }
    target = items_dir / "all.json"
    body = json.dumps(bundle, ensure_ascii=False, indent=2) + "\n"
    if dry_run:
        log.info("[dry-run] would write %s (%d bytes, %d categories)",
                 target, len(body), len(categories))
        return
    target.write_text(body, encoding="utf-8")
    log.info("wrote %s (%d bytes, %d categories, %d items)",
             target.relative_to(REPO_ROOT), len(body), len(categories),
             sum(len(c.items) for c in categories))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, help="debug: stop after N items")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    categories = scrape(limit=args.limit)
    if not categories:
        log.error("no items scraped — sitemap/selectors may have changed")
        return 4

    save_bundle(categories, REPO_ROOT / "items", args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
