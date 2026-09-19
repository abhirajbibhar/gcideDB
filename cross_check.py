#!/usr/bin/env python3
"""
Cross-check: compare raw CIDE.* source headwords against parsed output.

Finds:
  1. Missing entries  — headword in source but not in output
  2. Duplicates       — headword appearing more times in source than output
  3. Missing fields   — senses/definitions lost during parsing
"""
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ── Entity/webchr maps (same as gcide_build.py) ─────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def _load(fname):
    p = os.path.join(SCRIPT_DIR, fname)
    if not os.path.isfile(p):
        return {}
    with open(p, encoding='utf-8') as f:
        return json.load(f)

_dico = _load("_dico_maps.json")
_entity = _load("entity_map.json")
ENTITY_MAP = {**_dico.get("entity", {}), **_entity} if _dico else _entity
_webchr = _dico.get("webchr", {}) if _dico else {}
WEBCHR_MAP = {int(k): v for k, v in _webchr.items()}


def convert_entities(s):
    def repl(m):
        return ENTITY_MAP.get(m.group(1), m.group(0))
    s = re.sub(r"<([A-Za-z0-9]+)/", repl, s)
    return s


def apply_webchr(text):
    if not WEBCHR_MAP:
        return text
    def repl(m):
        code = int(m.group(1), 16)
        return WEBCHR_MAP.get(code, m.group(0))
    return re.sub(r"\\'([0-9a-fA-F]{2})", repl, text)


def clean_headword(raw):
    if not raw:
        return ""
    s = convert_entities(raw)
    s = apply_webchr(s)
    s = re.sub(r"<[^>]+>", "", s)         # strip tags
    s = s.replace('"', "").replace("`", "").replace("*", "").replace("'", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def norm(s):
    return clean_headword(s).casefold()


# ── 1. Scan source files ─────────────────────────────────────────────────────
def scan_source(gcide_dir, letters=None):
    """Extract every <ent> and <hw> from CIDE.* files."""
    files = sorted(Path(gcide_dir).glob("CIDE.[A-Z]"))
    if letters:
        files = [f for f in files if f.name[-1] in letters]

    RE_ENT = re.compile(r"<ent>(.*?)</ent>", re.DOTALL | re.IGNORECASE)
    RE_HW  = re.compile(r"<hw>(.*?)</hw>", re.DOTALL | re.IGNORECASE)
    RE_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

    all_ents = []  # list of {key, raw, clean, file}
    all_hws  = []

    for fp in files:
        print(f"  scanning {fp.name} ...", flush=True)
        text = fp.read_text(encoding="utf-8", errors="replace")
        text = RE_COMMENT.sub("", text)

        for m in RE_ENT.finditer(text):
            raw = m.group(1)
            cleaned = clean_headword(raw)
            key = norm(cleaned)
            if key:
                all_ents.append({"key": key, "raw": raw[:120], "clean": cleaned,
                                 "file": fp.name, "offset": m.start()})

        for m in RE_HW.finditer(text):
            raw = m.group(1)
            cleaned = clean_headword(raw)
            key = norm(cleaned)
            if key:
                all_hws.append({"key": key, "raw": raw[:120], "clean": cleaned,
                                "file": fp.name, "offset": m.start()})

    return all_ents, all_hws, [f.name for f in files]


# ── 2. Load parsed entries ────────────────────────────────────────────────────
def load_db(db_path):
    """Load all entries + senses from SQLite DB."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    entries = {}
    senses_count = 0
    empty_senses = 0

    for row in conn.execute("SELECT id, headword, letter, part_of_speech, raw_json FROM entries"):
        eid = row["id"]
        hw = row["headword"]
        key = norm(hw) if hw else ""

        # Count senses
        sc = conn.execute("SELECT COUNT(*) FROM senses WHERE entry_id = ?", (eid,)).fetchone()[0]
        senses_count += sc
        if sc == 0:
            empty_senses += 1

        if key not in entries:
            entries[key] = {
                "id": eid, "headword": hw, "letter": row["letter"],
                "pos": row["part_of_speech"], "sense_count": sc,
                "raw_json_len": len(row["raw_json"] or ""),
            }
        else:
            # duplicate key in DB
            entries[key]["_dup_in_db"] = entries[key].get("_dup_in_db", 1) + 1

    conn.close()
    return entries, senses_count, empty_senses


def load_json_keys(json_dir):
    """Load headword keys from per-letter JSON files."""
    keys = Counter()
    examples = {}
    for fp in sorted(Path(json_dir).glob("gcide_*.json")):
        data = json.loads(fp.read_text(encoding="utf-8"))
        entries = data.get("entries", []) if isinstance(data, dict) else data
        for entry in entries:
            hw = entry.get("headword", "")
            key = norm(hw)
            if key:
                keys[key] += 1
                examples.setdefault(key, entry)
    return keys, examples


# ── 3. Deep audit: check for missing senses ──────────────────────────────────
def deep_audit_sample(source_ents, db_entries, sample_size=200):
    """For a sample of source headwords, check if all senses are present."""
    import random
    random.seed(42)

    # Pick headwords that exist in both source and DB
    common = [e for e in source_ents if e["key"] in db_entries]
    if not common:
        return []

    sample = random.sample(common, min(sample_size, len(common)))
    issues = []

    for src in sample:
        db = db_entries.get(src["key"], {})
        sc = db.get("sense_count", 0)
        if sc == 0:
            issues.append({
                "headword": src["clean"],
                "key": src["key"],
                "file": src["file"],
                "issue": "entry exists in DB but has 0 senses",
            })

    return issues


# ── 4. Analyze duplicates ────────────────────────────────────────────────────
def analyze_source_duplicates(source_ents):
    """Find headwords that appear multiple times in source (legitimate homographs)."""
    by_key = defaultdict(list)
    for e in source_ents:
        by_key[e["key"]].append(e)

    dups = {k: v for k, v in by_key.items() if len(v) > 1}
    return dups


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    gcide_dir = sys.argv[1] if len(sys.argv) > 1 else "./gcide-0.54"
    db_path   = sys.argv[2] if len(sys.argv) > 2 else "dist/gcide.db"
    json_dir  = sys.argv[3] if len(sys.argv) > 3 else "dist"

    print("=" * 70)
    print("GCIDE CROSS-CHECK: Source vs Parsed Output")
    print("=" * 70)

    # ── Scan source ────────────────────────────────────────────────────────
    print("\n[1] Scanning source CIDE.* files ...")
    source_ents, source_hws, source_files = scan_source(gcide_dir)
    src_ent_keys = Counter(e["key"] for e in source_ents)
    src_hw_keys  = Counter(e["key"] for e in source_hws)
    src_ent_unique = set(src_ent_keys)
    src_hw_unique  = set(src_hw_keys)
    print(f"  Source files:  {len(source_files)}")
    print(f"  <ent> tags:    {len(source_ents)} total, {len(src_ent_unique)} unique")
    print(f"  <hw>  tags:    {len(source_hws)} total, {len(src_hw_unique)} unique")

    # ── Load DB ────────────────────────────────────────────────────────────
    print("\n[2] Loading parsed SQLite DB ...")
    db_entries, db_senses, db_empty = load_db(db_path)
    db_keys = set(db_entries.keys())
    print(f"  DB entries:    {len(db_entries)}")
    print(f"  DB senses:     {db_senses}")
    print(f"  Entries w/ 0 senses: {db_empty}")

    # ── Load JSON ──────────────────────────────────────────────────────────
    print("\n[3] Loading per-letter JSON files ...")
    json_keys, json_examples = load_json_keys(json_dir)
    json_unique = set(json_keys)
    print(f"  JSON entries:  {sum(json_keys.values())} total, {len(json_unique)} unique")

    # ── Compare: MISSING from output ───────────────────────────────────────
    print("\n" + "=" * 70)
    print("[4] MISSING ENTRIES (in source, not in output)")
    print("=" * 70)

    missing_from_db = sorted(src_ent_unique - db_keys)
    missing_from_json = sorted(src_ent_unique - json_unique)

    print(f"\n  vs DB:     {len(missing_from_db)} missing out of {len(src_ent_unique)}")
    print(f"  vs JSON:   {len(missing_from_json)} missing out of {len(src_ent_unique)}")

    if missing_from_db:
        print(f"\n  Sample missing (first 30):")
        by_file = defaultdict(int)
        for k in missing_from_db:
            ex = next((e for e in source_ents if e["key"] == k), None)
            if ex:
                by_file[ex["file"]] += 1
                print(f"    [{ex['file']}] {ex['clean']!r}  raw={ex['raw']!r}")
        print(f"\n  Missing by file:")
        for fn, n in sorted(by_file.items(), key=lambda x: -x[1])[:15]:
            print(f"    {fn}: {n}")

    # ── Compare: EXTRA in output ───────────────────────────────────────────
    print("\n" + "=" * 70)
    print("[5] EXTRA ENTRIES (in output, not in source <ent>)")
    print("=" * 70)

    extra_in_db = sorted(db_keys - src_ent_unique)
    extra_in_json = sorted(json_unique - src_ent_unique)

    print(f"\n  vs DB:     {len(extra_in_db)} extra")
    print(f"  vs JSON:   {len(extra_in_json)} extra")

    if extra_in_db:
        print(f"\n  Sample extra in DB (first 20):")
        for k in extra_in_db[:20]:
            e = db_entries[k]
            print(f"    {e['headword']!r} (letter={e['letter']}, pos={e['pos']})")

    # ── Duplicates ─────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("[6] DUPLICATE ANALYSIS")
    print("=" * 70)

    # Source duplicates (legitimate homographs)
    src_dups = analyze_source_duplicates(source_ents)
    print(f"\n  Source <ent> with multiple occurrences: {len(src_dups)} headwords")
    print(f"  (These are legitimate homographs, e.g. noun+verb)")

    # DB duplicates
    db_dup_count = sum(1 for v in db_entries.values() if v.get("_dup_in_db", 0) > 0)
    print(f"  DB entries with same headword appearing >1 time: {db_dup_count}")

    # Compare source count vs DB count for duplicated headwords
    print(f"\n  Source vs DB count comparison for homographs:")
    mismatch_count = 0
    for key, src_list in sorted(src_dups.items()):
        src_count = len(src_list)
        db_count = db_entries.get(key, {}).get("_dup_in_db", 1) if key in db_entries else 0
        if key not in db_entries:
            db_count = 0
        if src_count != db_count:
            mismatch_count += 1
            if mismatch_count <= 15:
                clean = src_list[0]["clean"]
                print(f"    {clean!r}: source={src_count}, db={db_count}  [{src_list[0]['file']}]")
    if mismatch_count > 15:
        print(f"    ... and {mismatch_count - 15} more mismatches")
    print(f"  Total count mismatches: {mismatch_count}")

    # ── SENSE COVERAGE ─────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("[7] SENSE / DEFINITION COVERAGE")
    print("=" * 70)

    # Entries with 0 senses
    zero_sense = [k for k, v in db_entries.items() if v["sense_count"] == 0]
    print(f"  Entries with 0 senses: {len(zero_sense)}")
    if zero_sense:
        print(f"  Sample (first 10):")
        for k in zero_sense[:10]:
            e = db_entries[k]
            print(f"    {e['headword']!r} (letter={e['letter']}, pos={e['pos']})")

    # Senses distribution
    sense_counts = Counter(v["sense_count"] for v in db_entries.values())
    print(f"\n  Senses per entry distribution:")
    for n in sorted(sense_counts.keys())[:10]:
        print(f"    {n} senses: {sense_counts[n]} entries")
    print(f"    ...")
    max_senses = max(sense_counts.keys()) if sense_counts else 0
    print(f"    max: {max_senses} senses in one entry")

    # ── DEEP AUDIT ─────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("[8] DEEP AUDIT: Sample cross-check (200 random entries)")
    print("=" * 70)

    issues = deep_audit_sample(source_ents, db_entries)
    if issues:
        print(f"  Issues found: {len(issues)}")
        for iss in issues[:10]:
            print(f"    [{iss['file']}] {iss['headword']!r}: {iss['issue']}")
    else:
        print(f"  No issues found in sample — entries have senses.")

    # ── SUMMARY ────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Source <ent> unique headwords:  {len(src_ent_unique)}")
    print(f"  Source <hw>  unique headwords:  {len(src_hw_unique)}")
    print(f"  DB unique headwords:           {len(db_entries)}")
    print(f"  JSON unique headwords:         {len(json_unique)}")
    print(f"")
    print(f"  Missing from DB:               {len(missing_from_db)}  ({100*len(missing_from_db)/max(1,len(src_ent_unique)):.1f}%)")
    print(f"  Missing from JSON:             {len(missing_from_json)}  ({100*len(missing_from_json)/max(1,len(src_ent_unique)):.1f}%)")
    print(f"  Extra in DB:                   {len(extra_in_db)}")
    print(f"  Extra in JSON:                 {len(extra_in_json)}")
    print(f"  Duplicate count mismatches:    {mismatch_count}")
    print(f"  Entries with 0 senses:         {len(zero_sense)}")
    print(f"  Total senses:                  {db_senses}")
    print("=" * 70)


if __name__ == "__main__":
    main()
