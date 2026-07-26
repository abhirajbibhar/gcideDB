#!/usr/bin/env python3
"""
Parse GCIDE ancillary lists into JSON:

  authors.lst  → authors.json
  abbrevn.lst  → abbreviations.json

Usage:
  python3 gcide_ancillary.py --gcide-dir gcide-0.54 --out-dir .
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List


def strip_htmlish(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"<[A-Za-z0-9]+/", "", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_authors(path: Path) -> List[Dict[str, Any]]:
    """
    authors.lst format (after header comments):
      Quoted-as
      Full name(s)
      Dates
      (blank line)
    Sometimes 2 name lines before dates.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    # drop comment blocks <! ... !>
    text = re.sub(r"<!.*?!>", "", text, flags=re.S)
    text = re.sub(r"<H1>.*?</h2>", "", text, flags=re.S | re.I)
    # split on blank lines
    blocks = re.split(r"\n\s*\n+", text)
    authors: List[Dict[str, Any]] = []
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        # skip pure markup / page markers
        lines = [ln for ln in lines if not re.match(r"^<!|^<h\d|^FILE:", ln, re.I)]
        if len(lines) < 2:
            continue
        quoted = strip_htmlish(lines[0])
        if not quoted or quoted.startswith("NOTE:") or quoted.startswith("comprising"):
            continue
        # last line is dates (or ND)
        dates = strip_htmlish(lines[-1])
        names = [strip_htmlish(x) for x in lines[1:-1]] if len(lines) > 2 else []
        if not names and len(lines) == 2:
            # quoted + dates only
            names = []
        authors.append(
            {
                "quoted_as": quoted,
                "names": names,
                "dates": dates if dates != "ND" else None,
            }
        )
    return authors


def parse_abbreviations(path: Path) -> List[Dict[str, str]]:
    """
    abbrevn.lst lines like:
      a., adj.          ----------    adjective
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    rows: List[Dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("=") or line.startswith("File ") or "LIST OF" in line:
            continue
        if "----------" not in line and "---" not in line:
            continue
        # split on run of dashes
        parts = re.split(r"\s*-{3,}\s*", line, maxsplit=1)
        if len(parts) != 2:
            continue
        abbr = strip_htmlish(parts[0])
        meaning = strip_htmlish(parts[1])
        if abbr and meaning:
            rows.append({"abbr": abbr, "meaning": meaning})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gcide-dir", type=Path, default=Path("./gcide-0.54"))
    ap.add_argument("--out-dir", type=Path, default=Path("./"))
    # also accept attachments paths
    ap.add_argument("--authors", type=Path, default=None)
    ap.add_argument("--abbrevn", type=Path, default=None)
    args = ap.parse_args()

    authors_path = args.authors or (args.gcide_dir / "authors.lst")
    abbrevn_path = args.abbrevn or (args.gcide_dir / "abbrevn.lst")
    # fallback attachments
    if not authors_path.is_file():
        authors_path = Path("./gcide-0.54/authors.lst")
    if not abbrevn_path.is_file():
        abbrevn_path = Path("./gcide-0.54/abbrevn.lst")

    args.out_dir.mkdir(parents=True, exist_ok=True)

    if authors_path.is_file():
        authors = parse_authors(authors_path)
        out = args.out_dir / "authors.json"
        out.write_text(json.dumps(authors, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"authors: {len(authors)} → {out}")
    else:
        print(f"authors.lst not found: {authors_path}")

    if abbrevn_path.is_file():
        abbrevs = parse_abbreviations(abbrevn_path)
        out = args.out_dir / "abbreviations.json"
        out.write_text(json.dumps(abbrevs, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"abbreviations: {len(abbrevs)} → {out}")
    else:
        print(f"abbrevn.lst not found: {abbrevn_path}")


if __name__ == "__main__":
    main()
