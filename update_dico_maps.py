#!/usr/bin/env python3
"""
Add missing GCIDE entities to _dico_maps.json

Usage:
  # Interactive: pass entity=unicode pairs
  python3 update_dico_maps.py colbreak=$'\\n' nbsp=$'\\u00a0' dot=·

  # From a skip-log summary (reads Most common unmapped entities section)
  python3 update_dico_maps.py --from-log gcide_skipped.log

  # Show current map size
  python3 update_dico_maps.py --status
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MAPS = Path(__file__).resolve().parent / "_dico_maps.json"

# Suggested defaults for known layout / punctuation entities
DEFAULTS = {
    "colbreak": "\n",
    "colret": "\n",
    "nbsp": "\u00a0",
    "dot": "·",
    "2dot": "‥",
    "3dot": "…",
    "8star": "⚹",   # U+26B9 SEXTILE
    "bar": "―",
    "lbrace2": "{",
    "rbrace2": "}",
    "lbrace": "{",
    "rbrace": "}",
    "star": "★",
    "ast": "*",
    "bullet": "•",
    "sp": " ",
    "thinsp": "\u2009",
    "ensp": "\u2002",
    "emsp": "\u2003",
}


def load() -> dict:
    return json.loads(MAPS.read_text(encoding="utf-8"))


def save(maps: dict) -> None:
    MAPS.write_text(json.dumps(maps, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {MAPS}  (entities={len(maps['entity'])}, webchr={len(maps['webchr'])})")


def add_entities(pairs: dict[str, str], force: bool = False) -> None:
    maps = load()
    entity = maps["entity"]
    for name, value in pairs.items():
        if name in entity and not force:
            print(f"  skip (exists): <{name}/ → {entity[name]!r}")
            continue
        entity[name] = value
        print(f"  + <{name}/ → {value!r}")
    maps["entity"] = entity
    save(maps)


def from_log(log_path: Path) -> None:
    """Parse unmapped entity names from a skip log / summary and apply DEFAULTS."""
    text = log_path.read_text(encoding="utf-8", errors="replace")
    # match: <colbreak/  →  6 occurrences   OR  entities": ["colbreak"]
    names = set(re.findall(r"<([A-Za-z0-9]+)/\s*→", text))
    names |= set(re.findall(r'"entities":\s*\[([^\]]+)\]', text) and [])
    for m in re.finditer(r'"entities":\s*\[(.*?)\]', text):
        for e in re.findall(r'"([^"]+)"', m.group(1)):
            names.add(e)

    if not names:
        print("No unmapped entity names found in log.")
        return

    print(f"Found {len(names)} unmapped names in {log_path}:")
    pairs = {}
    missing_defaults = []
    for n in sorted(names):
        if n in DEFAULTS:
            pairs[n] = DEFAULTS[n]
            print(f"  {n}: will use default {DEFAULTS[n]!r}")
        else:
            missing_defaults.append(n)
            print(f"  {n}: NO default — add manually:  python3 update_dico_maps.py {n}=<?>")

    if pairs:
        add_entities(pairs)
    if missing_defaults:
        print("\nStill need manual Unicode values for:")
        for n in missing_defaults:
            print(f"  {n}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Update _dico_maps.json entity table")
    ap.add_argument("pairs", nargs="*", help="name=value  (value may be Unicode char)")
    ap.add_argument("--from-log", type=Path, help="gcide_skipped.log or .summary.txt")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--force", action="store_true", help="overwrite existing keys")
    ap.add_argument("--list-defaults", action="store_true")
    args = ap.parse_args()

    if args.status:
        m = load()
        print(f"entities: {len(m['entity'])}")
        print(f"webchr:   {len(m['webchr'])}")
        return

    if args.list_defaults:
        for k, v in sorted(DEFAULTS.items()):
            print(f"  {k}={v!r}")
        return

    if args.from_log:
        from_log(args.from_log)
        return

    if not args.pairs:
        ap.print_help()
        return

    pairs = {}
    for p in args.pairs:
        if "=" not in p:
            print(f"Bad pair (need name=value): {p}", file=sys.stderr)
            sys.exit(1)
        name, val = p.split("=", 1)
        # allow \n \u00a0 escapes
        val = val.encode("utf-8").decode("unicode_escape")
        pairs[name] = val
    add_entities(pairs, force=args.force)


if __name__ == "__main__":
    main()
