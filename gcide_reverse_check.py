#!/usr/bin/env python3
"""
GCIDE reverse checker — compare parsed JSON/DB against original CIDE.* sources.

Finds headwords / entries that still exist in the source corpus but are missing
(or incomplete) in our export.  This is our own validation, independent of Dico.

Usage:
  # Against JSON produced by gcide_to_json_db.py
  python3 gcide_reverse_check.py --json gcide.json

  # Against SQLite DB
  python3 gcide_reverse_check.py --db gcide.db

  # Only letter E
  python3 gcide_reverse_check.py --json gcide_E_sample.json --letters E

  # Write detailed missing list
  python3 gcide_reverse_check.py --json gcide.json --out-report missing_report.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

RE_ENT = re.compile(r"<ent>(.*?)</ent>", re.DOTALL | re.IGNORECASE)
RE_HW = re.compile(r"<hw>(.*?)</hw>", re.DOTALL | re.IGNORECASE)
RE_DEF = re.compile(r"<def>(.*?)</def>", re.DOTALL | re.IGNORECASE)
RE_CD = re.compile(r"<cd>(.*?)</cd>", re.DOTALL | re.IGNORECASE)
RE_P = re.compile(r"<p>(.*?)</p>", re.DOTALL | re.IGNORECASE)
RE_TAG = re.compile(r"</?[A-Za-z0-9_.-]+(?:\s[^>]*)?>")
RE_ENTITY = re.compile(r"<([A-Za-z0-9]+)/")
RE_ESCAPE = re.compile(r"\\'([0-9a-fA-F]{2})")

# Align with parser: load official maps and convert entities before cleaning
_MAPS = Path(__file__).resolve().parent / "_dico_maps.json"
if not _MAPS.is_file():
    _MAPS = Path("_dico_maps.json")
try:
    import json as _json
    _m = _json.loads(_MAPS.read_text(encoding="utf-8"))
    ENTITY_MAP = _m["entity"]
    WEBCHR_MAP = {int(k): v for k, v in _m["webchr"].items()}
except Exception:
    ENTITY_MAP, WEBCHR_MAP = {}, {}

def convert_entities(s: str) -> str:
    def er(m):
        return ENTITY_MAP.get(m.group(1), m.group(0))
    s = RE_ENTITY.sub(er, s)
    def xr(m):
        return WEBCHR_MAP.get(int(m.group(1), 16), m.group(0))
    return RE_ESCAPE.sub(xr, s)



def clean_headword(raw: str) -> str:
    """Same normalization as parser: convert entities, strip tags, drop stress marks."""
    if not raw:
        return ""
    s = convert_entities(raw)
    s = RE_TAG.sub("", s)
    s = RE_ENTITY.sub("", s)  # any leftover unmapped
    s = s.replace('"', "").replace("`", "").replace("*", "").replace("'", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def norm_key(s: str) -> str:
    """Case-folded key for set comparison."""
    return clean_headword(s).casefold()


# ---------------------------------------------------------------------------
# Source scan
# ---------------------------------------------------------------------------
def scan_source_file(path: Path) -> Dict[str, Any]:
    """
    Extract every <ent> and <hw> from one CIDE.* file, plus light stats.
    Returns dict with lists of records.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    ents: List[Dict[str, Any]] = []
    for m in RE_ENT.finditer(text):
        raw = m.group(1)
        cleaned = clean_headword(raw)
        ents.append(
            {
                "kind": "ent",
                "raw": raw[:120],
                "clean": cleaned,
                "key": norm_key(cleaned),
                "offset": m.start(),
                "file": path.name,
            }
        )

    hws: List[Dict[str, Any]] = []
    for m in RE_HW.finditer(text):
        raw = m.group(1)
        cleaned = clean_headword(raw)
        hws.append(
            {
                "kind": "hw",
                "raw": raw[:120],
                "clean": cleaned,
                "key": norm_key(cleaned),
                "offset": m.start(),
                "file": path.name,
            }
        )

    # paragraphs that look like entry starts (have ent or hw)
    entry_paras = 0
    def_only_paras = 0
    cd_only_paras = 0
    for m in RE_P.finditer(text):
        p = m.group(1)
        has_hw = bool(RE_HW.search(p) or RE_ENT.search(p))
        has_def = bool(RE_DEF.search(p))
        has_cd = bool(RE_CD.search(p))
        if has_hw:
            entry_paras += 1
        elif has_def:
            def_only_paras += 1
        if has_hw and has_cd and not has_def:
            cd_only_paras += 1

    return {
        "file": path.name,
        "ents": ents,
        "hws": hws,
        "n_ent": len(ents),
        "n_hw": len(hws),
        "entry_paras": entry_paras,
        "def_only_paras": def_only_paras,
        "cd_without_def_in_entry_para": cd_only_paras,
        "bytes": len(text),
    }


def scan_sources(gcide_dir: Path, letters: Optional[Set[str]] = None) -> Dict[str, Any]:
    files = sorted(gcide_dir.glob("CIDE.[A-Z]"))
    if letters:
        files = [f for f in files if f.suffix[1:] in letters]

    by_file = []
    all_ent_keys: Counter = Counter()
    all_hw_keys: Counter = Counter()
    ent_examples: Dict[str, Dict[str, Any]] = {}
    hw_examples: Dict[str, Dict[str, Any]] = {}

    for fp in files:
        print(f"  scanning {fp.name} ...", flush=True)
        info = scan_source_file(fp)
        by_file.append({k: info[k] for k in (
            "file", "n_ent", "n_hw", "entry_paras",
            "def_only_paras", "cd_without_def_in_entry_para", "bytes",
        )})
        for rec in info["ents"]:
            if not rec["key"]:
                continue
            all_ent_keys[rec["key"]] += 1
            ent_examples.setdefault(rec["key"], rec)
        for rec in info["hws"]:
            if not rec["key"]:
                continue
            all_hw_keys[rec["key"]] += 1
            hw_examples.setdefault(rec["key"], rec)

    return {
        "by_file": by_file,
        "ent_keys": all_ent_keys,
        "hw_keys": all_hw_keys,
        "ent_examples": ent_examples,
        "hw_examples": hw_examples,
        "files": [f.name for f in files],
    }


# ---------------------------------------------------------------------------
# Load parsed export
# ---------------------------------------------------------------------------
def load_json_keys(json_path: Path) -> Tuple[Counter, Dict[str, Dict[str, Any]]]:
    """Load keys from either per-letter JSON or flat JSON array.

    Per-letter format (from gcide_build.py):
      {"section_marker": "A", "entries": [{...}, ...]}

    Flat format (legacy):
      [{headword: ...}, ...]
    """
    data = json.loads(json_path.read_text(encoding="utf-8"))

    # Accept both flat list and per-letter section object
    if isinstance(data, dict) and "entries" in data:
        # Single per-letter file
        entries = data["entries"]
    elif isinstance(data, list):
        entries = data
    else:
        # Unknown format, try iterating
        entries = data if isinstance(data, list) else []

    keys: Counter = Counter()
    examples: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        candidates = []
        if entry.get("headword"):
            candidates.append(entry["headword"])
        for h in entry.get("alt_headwords") or []:
            candidates.append(h)
        for e in entry.get("ents") or []:
            candidates.append(e)
        for c in candidates:
            k = norm_key(c)
            if not k:
                continue
            keys[k] += 1
            examples.setdefault(k, entry)
    return keys, examples


def load_db_keys(db_path: Path) -> Tuple[Counter, Dict[str, Dict[str, Any]]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    keys: Counter = Counter()
    examples: Dict[str, Dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT id, headword, headwords, definitions, pos FROM entries"
    ):
        candidates = [row["headword"] or ""]
        try:
            candidates.extend(json.loads(row["headwords"] or "[]"))
        except Exception:
            pass
        entry = {
            "id": row["id"],
            "headword": row["headword"],
            "pos": row["pos"],
            "definitions": row["definitions"],
        }
        for c in candidates:
            k = norm_key(c)
            if not k:
                continue
            keys[k] += 1
            examples.setdefault(k, entry)
    conn.close()
    return keys, examples


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------
def compare(
    source: Dict[str, Any],
    parsed_keys: Counter,
    parsed_examples: Dict[str, Dict[str, Any]],
    prefer: str = "ent",
) -> Dict[str, Any]:
    """
    prefer='ent' → treat source <ent> as ground truth
    prefer='hw'  → treat source <hw> as ground truth
    """
    src_counter: Counter = source["ent_keys"] if prefer == "ent" else source["hw_keys"]
    src_examples = source["ent_examples"] if prefer == "ent" else source["hw_examples"]

    src_set = set(src_counter)
    parsed_set = set(parsed_keys)

    missing_keys = sorted(src_set - parsed_set)  # in source, not in export
    extra_keys = sorted(parsed_set - src_set)    # in export, not in source (unusual)

    missing_records = []
    for k in missing_keys:
        ex = src_examples.get(k, {})
        missing_records.append(
            {
                "key": k,
                "clean": ex.get("clean", k),
                "raw": ex.get("raw", ""),
                "file": ex.get("file", ""),
                "offset": ex.get("offset"),
                "source_count": src_counter[k],
            }
        )

    # completeness by file
    by_file_missing: Counter = Counter()
    for rec in missing_records:
        by_file_missing[rec["file"]] += 1

    return {
        "prefer": prefer,
        "source_unique": len(src_set),
        "parsed_unique": len(parsed_set),
        "missing_count": len(missing_keys),
        "extra_count": len(extra_keys),
        "coverage_pct": round(100.0 * (1 - len(missing_keys) / max(1, len(src_set))), 3),
        "missing_by_file": dict(by_file_missing.most_common()),
        "missing": missing_records,
        "extra_sample": extra_keys[:50],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Reverse-check GCIDE JSON/DB vs CIDE.* sources")
    ap.add_argument("--gcide-dir", type=Path, default=Path("./gcide-0.54"))
    ap.add_argument("--json", type=Path, help="Parsed JSON from gcide_build.py (per-letter or flat)")
    ap.add_argument("--db", type=Path, help="Parsed SQLite DB")
    ap.add_argument("--letters", type=str, default="", help="e.g. E,A")
    ap.add_argument("--prefer", choices=("ent", "hw"), default="ent",
                    help="Ground-truth tag in source (default: ent)")
    ap.add_argument("--out-report", type=Path, default=None,
                    help="JSONL of missing headwords (default: gcide_missing.jsonl)")
    ap.add_argument("--out-summary", type=Path, default=None,
                    help="Summary text file (default: gcide_reverse_summary.txt)")
    ap.add_argument("--limit-missing", type=int, default=0, help="Cap missing list in report (0=all)")
    args = ap.parse_args()
    # defaults based on output dir
    if args.out_report is None:
        args.out_report = Path("gcide_missing.jsonl")
    if args.out_summary is None:
        args.out_summary = Path("gcide_reverse_summary.txt")

    if not args.json and not args.db:
        print("Provide --json and/or --db", file=sys.stderr)
        sys.exit(1)
    if not args.gcide_dir.is_dir():
        print(f"GCIDE dir not found: {args.gcide_dir}", file=sys.stderr)
        sys.exit(1)

    letters = {c.upper().strip() for c in args.letters.split(",") if c.strip()} or None

    print("=== 1. Scan source CIDE.* ===")
    source = scan_sources(args.gcide_dir, letters)
    print(f"  files: {len(source['files'])}")
    print(f"  unique <ent>: {len(source['ent_keys'])}")
    print(f"  unique <hw> : {len(source['hw_keys'])}")

    print("\n=== 2. Load parsed export ===")
    if args.json:
        print(f"  JSON: {args.json}")
        parsed_keys, parsed_examples = load_json_keys(args.json)
    else:
        print(f"  DB: {args.db}")
        parsed_keys, parsed_examples = load_db_keys(args.db)
    print(f"  unique keys in export: {len(parsed_keys)}")

    print("\n=== 3. Compare (source ground truth = <{}>) ===".format(args.prefer))
    result = compare(source, parsed_keys, parsed_examples, prefer=args.prefer)

    print(f"  source unique : {result['source_unique']}")
    print(f"  parsed unique : {result['parsed_unique']}")
    print(f"  MISSING       : {result['missing_count']}")
    print(f"  extra in JSON : {result['extra_count']}")
    print(f"  coverage      : {result['coverage_pct']}%")
    if result["missing_by_file"]:
        print("  missing by file:")
        for fn, n in list(result["missing_by_file"].items())[:30]:
            print(f"    {fn}: {n}")

    # write JSONL report
    missing = result["missing"]
    if args.limit_missing and args.limit_missing > 0:
        missing = missing[: args.limit_missing]
    args.out_report.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out_report, "w", encoding="utf-8") as f:
        for rec in missing:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"\n  report : {args.out_report} ({len(missing)} lines)")

    # summary text
    with open(args.out_summary, "w", encoding="utf-8") as f:
        f.write("GCIDE Reverse-Check Summary\n")
        f.write("=" * 60 + "\n")
        f.write(f"Source dir : {args.gcide_dir}\n")
        f.write(f"Export     : {args.json or args.db}\n")
        f.write(f"Ground truth tag: <{args.prefer}>\n")
        f.write(f"Letters    : {sorted(letters) if letters else 'ALL'}\n\n")
        f.write(f"Source unique keys : {result['source_unique']}\n")
        f.write(f"Parsed unique keys : {result['parsed_unique']}\n")
        f.write(f"Missing            : {result['missing_count']}\n")
        f.write(f"Extra in export    : {result['extra_count']}\n")
        f.write(f"Coverage           : {result['coverage_pct']}%\n\n")
        f.write("Per-file source stats:\n")
        for row in source["by_file"]:
            f.write(
                f"  {row['file']}: ent={row['n_ent']} hw={row['n_hw']} "
                f"entry_paras={row['entry_paras']} "
                f"cd_wo_def={row['cd_without_def_in_entry_para']}\n"
            )
        f.write("\nMissing by file:\n")
        for fn, n in result["missing_by_file"].items():
            f.write(f"  {fn}: {n}\n")
        f.write("\nSample missing (up to 40):\n")
        for rec in result["missing"][:40]:
            f.write(f"  [{rec['file']}] {rec['clean']!r}  raw={rec['raw']!r}\n")
        if result["extra_sample"]:
            f.write("\nSample extra keys in export (not in source <ent>/<hw>):\n")
            for k in result["extra_sample"][:40]:
                f.write(f"  {k!r}\n")
        f.write("\nNext steps:\n")
        f.write("  1. Inspect gcide_missing.jsonl\n")
        f.write("  2. Open the cited CIDE.* file near offset / headword\n")
        f.write("  3. Update gcide_to_json_db.py grouping / field rules\n")
        f.write("  4. Re-run parser + this reverse check until coverage ~100%\n")

    print(f"  summary: {args.out_summary}")
    print("\nDone.")


if __name__ == "__main__":
    main()
