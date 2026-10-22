#!/usr/bin/env python3
"""Fail if anything site-specific has got into the repository.

This repository is public. Nothing that identifies the estate it was
developed against belongs in it — not in code, comments, tests, fixtures,
documentation or planning notes. Real IP addresses, station and processor
hostnames, property names, room numbers and real device or point paths all
count.

This check exists because scrubbing once did not hold. The detail was
removed in September 2026 and was back in two new files within a fortnight,
because a real path makes a docstring concrete and a test realistic, so it
gets written back by anyone documenting what they saw on site. A list of
patterns that fails the build is the only version of this that survives
contact with people doing their jobs.

Use documentation-safe stand-ins instead:

    addresses   192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24  (RFC 5737)
    stations    Station1, Station2, SiteBMS
    places      Main Hall, Meeting Room, Block A, Room A1
    equipment   AHU1, DB-A1-Power, A_Kitchen, Fridge-1

Run it directly, or let CI do it:

    python3 scripts/check-no-site-detail.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Each pattern is a thing that must never appear, with why it matters.
#: Keep these specific. A pattern broad enough to match innocent text gets
#: switched off, and a check that is switched off protects nothing.
FORBIDDEN: list[tuple[str, str]] = [
    (r"\bmercure\b", "property brand name"),
    (r"\bbunnings\b", "property brand name"),
    (r"\bdoncaster\b", "place name"),
    (r"\bmelbourne\b", "place name"),
    (r"\bJace\d", "real station hostname — use Station1, Station2"),
    (r"\bSiteJace\d", "real station hostname"),
    (r"\bMercureBMS\b", "real station folder — use SiteBMS"),
    (r"\bMain_Kitchen\b", "real equipment path — use A_Kitchen"),
    (r"DB[-$]2?d?L\d\.\d", "real electrical board — use DB-A1-Power"),
    (r"Rooms/\$?33\d\d", "real room number — use Rooms/A1"),
    (r"\bRooms/3\d\d\b", "real room number — use Rooms/A1"),
    # The Daikin side: guest rooms are 3xx-6xx, equipment tags are
    # ACU L4-09 and the refrigerant systems are H-xx / PS-xx / CU-H-xx.
    (r"\bRoom [3-6]\d\d\b", "real room number — use Room A1"),
    (r"\brm [3-6]\d\d\b", "real room number — use Room A1"),
    (r"\bACU[ -]L[3-6]-\d", "real equipment tag — use ACU A-01"),
    (r"\bCU-H-\d\d\b", "real outdoor unit — use CU-S-01"),
    # Private address ranges. A real one in a public repo tells a reader
    # how the estate is addressed; the documentation ranges do not.
    (r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "private IP — use 192.0.2.x"),
    (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "private IP — use 192.0.2.x"),
    (r"\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b", "private IP — use 192.0.2.x"),
]

SUFFIXES = {".py", ".yaml", ".yml", ".md", ".json", ".ts", ".js", ".mjs", ".txt", ".cfg",
            ".toml", ".html", ".sh"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"}

#: This file names the patterns, so it is allowed to contain them.
SELF = Path(__file__).resolve()


def files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file() or path.resolve() == SELF:
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        if path.suffix in SUFFIXES:
            yield path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    compiled = [(re.compile(p, re.IGNORECASE), why) for p, why in FORBIDDEN]
    findings: list[str] = []

    for path in files(root):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            for pattern, why in compiled:
                match = pattern.search(line)
                if match:
                    findings.append(
                        f"{path.relative_to(root)}:{number}: "
                        f"{match.group(0)!r} — {why}"
                    )

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
