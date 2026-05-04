#!/usr/bin/env python3
"""
build_manifest.py — walks `rules/` and `items/`, calculates SHA-256 and size
for every JSON file, and rewrites `manifest.json` at the repo root.

The tool's IDataSyncService reads manifest.json first, so every commit MUST
include a fresh manifest. The GitHub Actions workflow runs this script
automatically after scrape_rules.py + scrape_items.py.

Usage:
    python scripts/build_manifest.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "manifest.json"

log = logging.getLogger("build_manifest")

SCHEMA_VERSION = "1"


def sha256_and_size(path: Path) -> tuple[str, int]:
    """Hash and measure the file AFTER normalising CRLF->LF so the manifest
    SHA-256 matches the bytes GitHub raw serves regardless of local Git
    autocrlf settings on the scraper machine."""
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest(), len(data)


def scan_directory(directory: Path, kind: str, id_prefix: str) -> list[dict]:
    if not directory.is_dir():
        log.info("directory %s not present, skipping kind=%s", directory, kind)
        return []

    packages: list[dict] = []
    for file in sorted(directory.glob("*.json")):
        stat = file.stat()
        package_id = f"{id_prefix}.{file.stem}"
        # package version = its first changelog entry, if present
        version = _peek_version(file)
        sha, size = sha256_and_size(file)
        packages.append({
            "id": package_id,
            "kind": kind,
            "url": f"{directory.name}/{file.name}",
            "sha256": sha,
            "size": size,
            "version": version,
            "updatedAt": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
        log.info("  %s (%d bytes LF-norm, sha256 %s...)",
                 package_id, size, sha[:8])

    return packages


def _peek_version(file: Path) -> str:
    """Best-effort: read the top-level `version` field of a bundle."""
    try:
        with file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        v = data.get("version")
        if isinstance(v, str):
            return v
    except Exception:
        pass
    return file.stat().st_mtime.__str__()


def build() -> dict:
    now = datetime.now(timezone.utc)
    packages: list[dict] = []
    packages.extend(scan_directory(REPO_ROOT / "rules", kind="rules", id_prefix="rules"))
    packages.extend(scan_directory(REPO_ROOT / "items", kind="items", id_prefix="items"))

    # Optional: punishments/ directory if someone hand-curates overrides.
    packages.extend(scan_directory(
        REPO_ROOT / "punishments", kind="punishments", id_prefix="punishments"
    ))

    manifest = {
        "version": now.strftime("%Y.%m.%d.%H%M"),
        "generatedAt": now.isoformat(),
        "schemaVersion": SCHEMA_VERSION,
        "baseUrl": None,
        "packages": packages,
    }
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    manifest = build()
    body = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"

    if args.dry_run:
        log.info("[dry-run] %d packages, %d bytes", len(manifest["packages"]), len(body))
        print(body)
        return 0

    # Write manifest.json with LF newlines so its own bytes on disk match
    # what git + GitHub raw serve. (Python's write_text would translate
    # \n → \r\n on Windows otherwise, invalidating any external integrity
    # checks run against our own file.)
    MANIFEST_PATH.write_bytes(body.encode("utf-8"))
    log.info("wrote %s (%d packages, %d bytes)",
             MANIFEST_PATH.name, len(manifest["packages"]), len(body))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
