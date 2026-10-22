#!/usr/bin/env python3
"""Fail if anything site-specific has got into the repository.

This repository is public. Nothing that identifies a real installation
belongs in it — not in code, comments, tests, fixtures, documentation or
planning notes. Real IP addresses, station and processor hostnames,
property names, room numbers and real device or point paths all count.

Two lists are checked:

- **The generic rules below**: private IP ranges, which no public example
  ever needs.
- **The site's own words**, from a patterns file kept *outside* the
  repository, because a published list of what to hide names exactly what
  it hides. It is looked for at `$SITE_DETAIL_PATTERNS`, then at
  `site-detail-patterns.txt` beside this repository's folder. One regular
  expression per line, optionally followed by ` | reason`; `#` starts a
  comment. Without the file only the generic rules run, and the script
  says so.

Use documentation-safe stand-ins instead:

    addresses   192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24  (RFC 5737)
    stations    Station1, Station2, SiteBMS
    places      Main Hall, Meeting Room, Block A, Room A1
    equipment   AHU1, DB-A1-Power, A_Kitchen, ACU A-01, Fridge-1

Run it directly, or let CI do it:

    python3 scripts/check-no-site-detail.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

#: Generic rules: a real private address in a public repo tells a reader how
#: a network is addressed; the documentation ranges do not.
FORBIDDEN: list[tuple[str, str]] = [
    (r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "private IP — use 192.0.2.x"),
    (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "private IP — use 192.0.2.x"),
    (r"\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b", "private IP — use 192.0.2.x"),
]

SUFFIXES = {".py", ".yaml", ".yml", ".md", ".json", ".ts", ".js", ".mjs", ".txt",
            ".cfg", ".toml", ".html", ".sh"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"}

SELF = Path(__file__).resolve()
ROOT = SELF.parent.parent


def site_patterns() -> tuple[list[tuple[str, str]], Path | None]:
    """The site's own words, from the file outside the repository."""
    for candidate in (os.environ.get("SITE_DETAIL_PATTERNS"),
                      ROOT.parent / "site-detail-patterns.txt"):
        if not candidate:
            continue
        path = Path(candidate)
        if not path.is_file():
            continue
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pattern, _, why = line.partition(" | ")
            out.append((pattern.strip(), why.strip() or "site-specific"))
        return out, path
    return [], None


def files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file() or path.resolve() == SELF:
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        if path.suffix in SUFFIXES:
            yield path


def main() -> int:
    extra, source = site_patterns()
    compiled = [(re.compile(p, re.IGNORECASE), why) for p, why in FORBIDDEN + extra]
    findings: list[str] = []

    for path in files(ROOT):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            for pattern, why in compiled:
                match = pattern.search(line)
                if match:
                    findings.append(
                        f"{path.relative_to(ROOT)}:{number}: "
                        f"{match.group(0)!r} — {why}"
                    )

    if source is None:
        print("No site patterns file found; checked the generic rules only.")
    if findings:
        print(f"{len(findings)} piece(s) of site-specific detail found:\n")
        for finding in findings:
            print(f"  {finding}")
        print(
            "\nThis repository is public. See the top of this script for what "
            "to use instead."
        )
        return 1

    print("No site-specific detail found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
