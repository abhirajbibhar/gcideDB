#!/usr/bin/env python3
"""
gcide_build.py — Production GCIDE builder for dictionary apps.

Merges:
  • Tree-based markup parser (sense-aware, nested tags)
  • Full semantic fields (morphology, taxonomy, WordNet, collocations, …)
  • JSON export (+ optional gzip)
  • Issue log + summary
  • Entity / abbreviation / author resolution
  • Optional per-letter JSON files (one per CIDE.X source)

Usage:
  python3 gcide_build.py ./gcide-0.54 dist/
  python3 gcide_build.py ./gcide-0.54 dist/ --letters E,W --pretty

Output directory contains:
  front_matter.json          # dictionary front matter (once, at the beginning)
  gcide_A.json … gcide_Z.json  # one section object per letter:
                               #   { "section_marker": "A", "entries": [...] }
  gcide.log                  # parse log

No combined/bulky single JSON is written. Markers are never attached to
individual word entries.

Sidecar files (same directory as this script):
  entity_map.json   Unicode entities
  webchr_map.json   optional \'xx hex escapes
  abbrevn.json      field abbreviations
  authors.json      quote-author → full name
"""

import argparse, gzip, json, os, re, sys, time
from typing import Any, Dict, List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def _load(fname):
    p = os.path.join(SCRIPT_DIR, fname)
    return json.load(open(p, encoding='utf-8')) if os.path.isfile(p) else {}

ENTITY_MAP  = _load("entity_map.json")
WEBCHR_MAP  = {int(k): v for k, v in _load("webchr_map.json").items()} if _load("webchr_map.json") else {}
# also try _dico_maps.json
_dico = _load("_dico_maps.json")
if _dico:
    ENTITY_MAP = {**_dico.get("entity", {}), **ENTITY_MAP}
    WEBCHR_MAP = {**{int(k): v for k, v in _dico.get("webchr", {}).items()}, **WEBCHR_MAP}
ABBREV_MAP  = _load("abbrevn.json")
AUTHORS_MAP = _load("authors.json")

WORD_CHARS = re.compile(r"[A-Za-z0-9_]")
LETTERS    = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


# ── Comment stripping (preserves newlines for accurate line numbers) ──────────
def strip_comments(text):
    out, i, n = [], 0, len(text)
    while i < n:
        j = text.find("<--", i)
        if j == -1: out.append(text[i:]); break
        out.append(text[i:j])
        k = text.find("-->", j + 3)
        removed = text[j:] if k == -1 else text[j:k+3]
        out.append("\n" * removed.count("\n"))
        i = n if k == -1 else k + 3
    return "".join(out)


# ── Tokenizer → generic node tree ────────────────────────────────────────────
def parse_markup(text, letter=None, log=None):
    def lineno(p): return text.count("\n", 0, p) + 1
    def snip(p, w=70):
        s, e = max(0, p-w//2), min(len(text), p+w//2)
        return text[s:e].replace("\n", " ")

    n    = len(text)
    root = {"tag": None, "children": []}
    stk  = [root]
    i    = 0

    while i < n:
        if text[i] != "<":
            j = text.find("<", i); j = n if j == -1 else j
            if j > i: stk[-1]["children"].append(text[i:j])
            i = j; continue

        if i + 1 >= n: stk[-1]["children"].append("<"); i += 1; continue
        nxt = text[i+1]

        # closing tag
        if nxt == "/":
            j = i + 2
            while j < n and WORD_CHARS.match(text[j]): j += 1
            name = text[i+2:j].lower()
            gt   = text.find(">", j); pos = i
            i    = (gt+1) if gt != -1 else n
            if not name: continue
            # <p> is purely typographical -- only pop the p itself
            if name == "p":
                if len(stk) > 1 and stk[-1]["tag"] == "p":
                    stk.pop()
                continue
            m = next((k for k in range(len(stk)-1,0,-1) if stk[k]["tag"]==name), None)
            if m is not None:
                del stk[m:]
            elif log is not None:
                log.append({"letter": letter, "source_file": f"CIDE.{letter}" if letter else None,
                    "type": "parse_warning",
                    "line": lineno(pos),
                    "reason": (f"stray </{name}> with no matching open tag -- "
                               f"GCIDE source error where <{name}> was opened in "
                               f"one <p> block and closed in another. Entry content "
                               f"intact; tag ignored (matches GNU Dico C parser)."),
                    "context": snip(pos)})
            continue

        if not WORD_CHARS.match(nxt): stk[-1]["children"].append("<"); i += 1; continue

        # tag name
        j = i + 1
        while j < n and WORD_CHARS.match(text[j]): j += 1
        name_x = text[i+1:j]
        name   = name_x.lower()
        k = j
        while k < n and text[k] in " \t": k += 1

        # entity shorthand <name/ or true self-close <name/>
        if k < n and text[k] == "/":
            pos = i
            k2  = k + 1
            i   = (k2+1) if (k2 < n and text[k2] == ">") else k+1
            if name == "br":
                stk[-1]["children"].append({"tag": "br"})
            else:
                repl = ENTITY_MAP.get(name_x) if name_x != name else None
                if repl is None: repl = ENTITY_MAP.get(name)
                if repl is not None:
                    if repl: stk[-1]["children"].append(repl)
                else:
                    stk[-1]["children"].append({"tag": "_unk", "name": name_x})
                    if log is not None:
                        log.append({"letter": letter, "source_file": f"CIDE.{letter}" if letter else None,
                            "type": "parse_warning",
                            "line": lineno(pos),
                            "reason": (f"unrecognized entity <{name_x}/ -- "
                                       f"no Unicode mapping. Kept as marker node; "
                                       f"omitted from output fields."),
                            "context": snip(pos)})
            continue

        # plain open tag
        if k < n and text[k] == ">":
            i    = k + 1
            node = {"tag": name, "children": []}
            stk[-1]["children"].append(node); stk.append(node); continue

        # tag with attributes (rare: <table FRAME="...">)
        gt = text.find(">", k)
        if gt == -1: attr, i = text[k:], n
        else:        attr, i = text[k:gt], gt+1
        sc   = attr.rstrip().endswith("/")
        node = {"tag": name, "children": []}
        if attr.strip().rstrip("/").strip(): node["attrs"] = attr.strip().rstrip("/").strip()
        stk[-1]["children"].append(node)
        if not sc: stk.append(node)

    return root["children"]


# ── Tree helpers ──────────────────────────────────────────────────────────────
def is_tag(n, t):   return isinstance(n, dict) and n.get("tag") == t
def is_any(n, *ts): return isinstance(n, dict) and n.get("tag") in ts

def text_of(node):
    if isinstance(node, str):    return node
    if isinstance(node, list):   return "".join(text_of(c) for c in node)
    if isinstance(node, dict):
        if node.get("tag") in ("br", "_unk"): return "" if node.get("tag") == "_unk" else "\n"
        return "".join(text_of(c) for c in node.get("children", []))
    return ""

def clean(s):
    s = re.sub(r"[ \t]+", " ", s).strip()
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()

def find_all(nodes, *tags):
    it = nodes if isinstance(nodes, list) else [nodes]
    for n in it:
        if isinstance(n, dict):
            if n.get("tag") in tags: yield n
            yield from find_all(n.get("children", []), *tags)

def first_tag(nodes, *tags):
    return next(find_all(nodes, *tags), None)

def is_blank(ch): return not clean(text_of(ch))

def first_real(ch):
    for c in ch:
        if isinstance(c, str) and not c.strip(): continue
        return c
    return None

def expand_abbrev(s):
    """Expand known abbreviations in a field string (longest-first)."""
    if not s:
        return s
    # longest keys first so "a., adj." wins over "a."
    for abbr, full in sorted(ABBREV_MAP.items(), key=lambda kv: -len(kv[0])):
        s = re.sub(
            r"(?<![A-Za-z])" + re.escape(abbr) + r"(?![A-Za-z])",
            full,
            s,
        )
    return s


def expand_pos(pos: str):
    """Expand part-of-speech abbreviations to full words when known."""
    if not pos:
        return pos
    key = pos.strip()
    if key in ABBREV_MAP:
        return ABBREV_MAP[key]
    # compound: "a. & n." / "v. t." / "p. p."
    # First try full string with normalized spaces
    norm = re.sub(r"\s+", " ", key)
    if norm in ABBREV_MAP:
        return ABBREV_MAP[norm]
    # token expand
    # keep "v. i." / "v. t." / "p. p." / "p. pr." together
    tokens = re.findall(
        r"v\.\s*[it]\.|"
        r"p\.\s*p\.|"
        r"p\.\s*pr\.|"
        r"vb\.\s*n\.|"
        r"[A-Za-z]+\.|"
        r"[&,]|"
        r"\s+|"
        r"[^\s]+",
        norm,
    )
    out = []
    for tok in tokens:
        if not tok.strip() or tok in "&,":
            out.append(tok if tok in "&," else tok)
            continue
        t = tok.strip()
        hit = None
        for abbr, full in sorted(ABBREV_MAP.items(), key=lambda kv: -len(kv[0])):
            if t == abbr or t.rstrip(".") == abbr.rstrip(".") or re.sub(r"\s+", "", t) == re.sub(r"\s+", "", abbr):
                hit = full
                break
        out.append(hit or tok)
    result = "".join(out)
    result = re.sub(r"\s+", " ", result).strip()
    return result if result else pos


def expand_field_label(field_raw: str):
    """Expand <fld> labels like (Bot.) / (Zool.) → Botany / Zoology."""
    if not field_raw:
        return field_raw
    # normalize Zoöl entity leftovers
    s = field_raw.replace("Zoöl", "Zool").replace("Zoölogy", "Zoology")
    stripped = s.strip().strip("()[]").strip()
    candidates = [
        s, stripped, stripped + ".",
        stripped.rstrip("."),
        field_raw.strip(),
    ]
    for c in candidates:
        if c in ABBREV_MAP:
            return ABBREV_MAP[c]
    expanded = expand_abbrev(stripped)
    if expanded != stripped:
        return expanded
    return field_raw


# ── Entry segmentation ────────────────────────────────────────────────────────
def leading_headwords(ch):
    words = []
    for c in ch:
        if isinstance(c, str) and not c.strip(): continue
        if is_tag(c, "br"): continue
        if is_tag(c, "ent"): words.append(clean(text_of(c["children"]))); continue
        break
    return words

def is_section_marker(ch):
    """True for letter running-heads like <centered>A.</centered>, optionally
    followed by <br/> / [1913 Webster] source tags on the same <p>."""
    real = [c for c in ch if not (isinstance(c, str) and not c.strip())]
    if not real:
        return False
    if not is_tag(real[0], "centered"):
        return False
    # Allow trailing br / source / source-like noise after the centered head
    for c in real[1:]:
        if is_tag(c, "br"):
            continue
        if is_tag(c, "source"):
            continue
        # anything else means this is not a pure section marker
        return False
    return True

def extract_front_quote(ch):
    """Turn a pre-entry <p> into structured front-matter.

    Handles the standard opening quote:
      <q>…</q><rj><qau>Locke.</qau></rj>
    Returns a dict {text, author?, author_info?} or plain text fallback.
    """
    q_node = first_tag(ch, "q")
    if q_node is None:
        t = clean(text_of(ch))
        return t if t else None

    # Quote text only (exclude any nested author tags if present)
    parts = []
    for c in q_node.get("children", []):
        if is_any(c, "qau", "au"):
            continue
        parts.append(text_of(c))
    text = clean("".join(parts))
    if not text:
        return None

    item: Dict[str, Any] = {"text": text}

    au_node = first_tag(ch, "qau", "au")
    if au_node is None:
        au_node = first_tag(q_node.get("children", []), "qau", "au")
    if au_node is not None:
        short = clean(text_of(au_node.get("children", [])))
        if short:
            item["author"] = short.rstrip(".")  # canonical short key form
            info = resolve_author(short)
            if info:
                item["author_info"] = {
                    k: v for k, v in info.items() if v not in (None, "", "ND")
                }
                # Prefer the full display name as author string
                item["author"] = info.get("name") or item["author"]
    return item


def segment_entries(top_nodes, letter, log=None):
    entries, front_matter, section_markers, current = [], [], [], None
    for p in (n for n in top_nodes if is_tag(n, "p")):
        ch = p.get("children", [])
        if is_blank(ch):
            # Page-number markers — handled, not logged
            continue
        if is_section_marker(ch):
            t = clean(text_of(ch))
            section_markers.append(t)
            # Section markers become section_marker / other_markers — not logged
            continue
        first = first_real(ch)
        if is_tag(first, "ent"):
            words = leading_headwords(ch)
            current = {"headword": words[0], "alt_headwords": words[1:] or None,
                       "letter": letter, "blocks": [ch]}
            entries.append(current)
        else:
            if current:
                current["blocks"].append(ch)
            else:
                # Pre-entry material → structured front matter (not logged)
                item = extract_front_quote(ch)
                if item:
                    front_matter.append(item)
    return entries, front_matter, section_markers


# ── Semantic extraction ───────────────────────────────────────────────────────
def extract(entry, homograph):
    blocks = entry["blocks"]
    stream = []
    for b in blocks: stream.extend(b)

    # ── header fields from first block ───────────────────────────────────────
    b0 = blocks[0]
    hw_node   = first_tag(b0, "hw")
    pr_node   = first_tag(b0, "pr")
    pos_node  = first_tag(b0, "pos")
    fld_node  = first_tag(b0, "fld")
    ety_node  = first_tag(b0, "ety")
    vmr_node  = first_tag(b0, "vmorph")
    amr_node  = first_tag(b0, "amorph")
    nmr_node  = first_tag(b0, "nmorph")
    plu_node  = first_tag(b0, "plu")
    sing_node = first_tag(b0, "sing")

    headword_full   = clean(text_of(hw_node["children"]))  if hw_node   else None
    pronunciation   = clean(text_of(pr_node["children"]))  if pr_node   else None
    pos             = clean(text_of(pos_node["children"])) if pos_node  else None
    field_raw       = clean(text_of(fld_node["children"])) if fld_node  else None
    etymology       = clean(text_of(ety_node["children"])) if ety_node  else None

    # conjugated verb forms: extract conjf words + their pos labels from vmorph
    conjugated_forms = None
    if vmr_node:
        conjugated_forms = clean(text_of(vmr_node["children"]))

    # adjective comparative/superlative forms from amorph
    adj_forms = None
    if amr_node:
        adj_forms = clean(text_of(amr_node["children"]))

    # noun declension forms from nmorph
    noun_forms = None
    if nmr_node:
        noun_forms = clean(text_of(nmr_node["children"]))

    # plural: just the <plw> word
    plural = None
    if plu_node:
        plw = first_tag(plu_node["children"], "plw")
        plural = clean(text_of(plw["children"])) if plw else None

    # singular (for headwords that are plural forms)
    singular = None
    if sing_node:
        singw = first_tag(sing_node["children"], "singw")
        singular = clean(text_of(singw["children"])) if singw else None

    # ── stream walker ─────────────────────────────────────────────────────────
    senses      = []
    cur_sense   = None
    pending_q   = None  # pending quote text waiting for author

    entry_sources  = []
    entry_quotes   = []
    synonyms       = None
    antonyms_t     = []
    usage          = None
    cross_refs     = []
    see_also       = []
    alt_spells     = []
    note_parts     = []
    derived_forms  = None
    chemical_formula = None
    taxonomy       = {}
    hypernym       = None
    subtypes       = []
    wordnet_sense  = None
    contrasting    = []
    illu_examples  = []   # WordNet <illu> examples
    plural_def     = False  # entry defined only in plural (<pluf>)

    def push_sense(number=None):
        s = {"number": number, "sub_defs": [], "field": None, "marker": None,
             "definition": None, "examples": [], "quotations": []}
        senses.append(s)
        return s

    def flush_q(author=None):
        nonlocal pending_q
        if pending_q is None: return
        q = {"text": pending_q}
        if author: q["author"] = author
        if cur_sense: cur_sense["quotations"].append(q)
        else:         entry_quotes.append(q)
        pending_q = None

    def clean_example(a):
        t = clean(text_of(a["children"]))
        return re.sub(r'^as[,;]\s*', '', t, flags=re.IGNORECASE).strip()

    i = 0
    while i < len(stream):
        node = stream[i]; i += 1
        if not isinstance(node, dict): continue
        tag = node.get("tag")
        ch  = node.get("children", [])

        if tag == "sn":
            flush_q()
            cur_sense = push_sense(clean(text_of(ch)) or None)

        elif tag == "fld":
            t = clean(text_of(ch))
            if cur_sense and not cur_sense["field"]: cur_sense["field"] = expand_field_label(t)

        elif tag == "mark":
            t = clean(text_of(ch))
            if cur_sense: cur_sense["marker"] = t

        elif tag == "pluf":
            plural_def = True  # this entry is defined as a plural form

        elif tag in ("def", "def2", "cd", "cd2"):
            flush_q()
            if cur_sense is None: cur_sense = push_sense()
            examples = [clean_example(a) for a in find_all(ch, "as")]
            illu_ex  = [clean(text_of(a["children"])) for a in find_all(ch, "illu")]
            t = clean(text_of(ch))
            if cur_sense["definition"]: cur_sense["definition"] += " " + t
            else:                       cur_sense["definition"] = t
            cur_sense["examples"].extend(e for e in examples if e)
            illu_examples.extend(e for e in illu_ex if e)

        elif tag == "sd":
            # lettered sub-definition label (a), (b) -- attach upcoming def to this
            t = clean(text_of(ch))
            if cur_sense is None: cur_sense = push_sense()
            cur_sense["sub_defs"].append({"label": t, "definition": None})

        elif tag == "illu":
            t = clean(text_of(ch))
            if t: illu_examples.append(t)

        elif tag == "q":
            flush_q()
            pending_q = clean(text_of(ch))
            au = first_tag(ch, "au", "qau")
            if au: flush_q(clean(text_of(au["children"])))

        elif tag == "rj":
            au = first_tag(ch, "qau", "au")
            if au:
                author = clean(text_of(au["children"]))
                if pending_q is not None: flush_q(author)
                else:
                    pool = cur_sense["quotations"] if cur_sense else entry_quotes
                    if pool and "author" not in pool[-1]: pool[-1]["author"] = author
            else: flush_q()

        elif tag == "source":
            t = clean(text_of(ch))
            if t: entry_sources.append(t)

        elif tag == "syn":
            flush_q(); synonyms = clean(text_of(ch))

        elif tag == "ant":
            t = clean(text_of(ch))
            if t: antonyms_t.append(t)

        elif tag == "usage":
            flush_q(); usage = clean(text_of(ch))

        elif tag == "er":
            t = clean(text_of(ch))
            if t and t not in cross_refs: cross_refs.append(t)

        elif tag == "see":
            for er in find_all(ch, "er", "simto"):
                t = clean(text_of(er["children"]))
                if t and t not in see_also: see_also.append(t)

        elif tag == "cref":
            t = clean(text_of(ch))
            if t and t not in see_also: see_also.append(t)

        elif tag == "altsp":
            for asp in find_all(ch, "asp"):
                t = clean(text_of(asp["children"]))
                if t and t not in alt_spells: alt_spells.append(t)

        elif tag in ("altname", "altnpluf"):
            t = clean(text_of(ch))
            if t and t not in alt_spells: alt_spells.append(t)

        elif tag == "note":
            flush_q(); note_parts.append(clean(text_of(ch)))

        elif tag == "wordforms":
            derived_forms = clean(text_of(ch)) or None

    flush_q()

    # ── deep scan for fields nested inside def/cd/note/etc ───────────────────
    # These tags can appear anywhere in the tree (inside definitions, notes...)
    # so we need a full recursive scan, not just the top-level stream.
    for node in find_all(stream,
            "spn","gen","fam","ord","class","subclass","phylum","subphylum","kingdom",
            "isa","hypen","stype","chform","chformi","wns","contr","cref","illu"):
        tag = node.get("tag")
        t   = clean(text_of(node.get("children", [])))
        if not t: continue
        if tag == "spn":                        taxonomy.setdefault("species", t)
        elif tag == "gen":                      taxonomy.setdefault("genus", t)
        elif tag == "fam":                      taxonomy.setdefault("family", t)
        elif tag == "ord":                      taxonomy.setdefault("order", t)
        elif tag in ("class","subclass"):       taxonomy.setdefault("class", t)
        elif tag in ("phylum","subphylum"):     taxonomy.setdefault("phylum", t)
        elif tag == "kingdom":                  taxonomy.setdefault("kingdom", t)
        elif tag in ("isa","hypen"):
            if not hypernym: hypernym = t
        elif tag == "stype":
            if t not in subtypes: subtypes.append(t)
        elif tag in ("chform","chformi"):
            if not chemical_formula: chemical_formula = t
        elif tag == "wns":
            if not wordnet_sense: wordnet_sense = t
        elif tag == "contr":
            if t not in contrasting: contrasting.append(t)
        elif tag == "cref":
            if t not in see_also: see_also.append(t)
        elif tag == "illu":
            if t not in illu_examples: illu_examples.append(t)

    # ── collocations
    collocations = []
    for cs in find_all(stream, "cs"):
        # Each <col>/<colp> within <cs> is one phrase; <mcol> groups multiple
        # phrases sharing a definition; <cd> is the definition
        def extract_cols(parent_ch):
            results = []
            j = 0
            nodes_list = list(parent_ch)
            while j < len(nodes_list):
                n = nodes_list[j]
                if is_any(n, "col", "colp"):
                    phrase = clean(text_of(n["children"]))
                    defn = None
                    quotes = []
                    # look ahead for cd/cd2
                    k = j + 1
                    while k < len(nodes_list):
                        nn = nodes_list[k]
                        if is_any(nn, "cd", "cd2"):
                            defn = clean(text_of(nn["children"]))
                            k += 1; break
                        elif is_any(nn, "col", "colp", "mcol", "cs"):
                            break
                        k += 1
                    # collect quotations in this col's scope
                    for q in find_all([n] + nodes_list[j+1:k], "q"):
                        qt = clean(text_of(q["children"]))
                        au = first_tag(q["children"], "au", "qau")
                        qo = {"text": qt}
                        if au: qo["author"] = clean(text_of(au["children"]))
                        quotes.append(qo)
                    results.append({
                        "phrase": phrase,
                        "definition": defn,
                        "quotations": quotes or None,
                    })
                    j = k
                elif is_tag(n, "mcol"):
                    # multiple phrases sharing one definition
                    phrases = [clean(text_of(c["children"])) for c in find_all(n["children"], "col","colp")]
                    # find following cd
                    k = j + 1
                    defn = None
                    while k < len(nodes_list):
                        nn = nodes_list[k]
                        if is_any(nn, "cd", "cd2"):
                            defn = clean(text_of(nn["children"])); k += 1; break
                        elif is_any(nn, "col","mcol","cs"): break
                        k += 1
                    for phrase in phrases:
                        results.append({"phrase": phrase, "definition": defn, "quotations": None})
                    j = k
                else:
                    j += 1
            return results
        collocations.extend(extract_cols(cs.get("children", [])))

    # ── clean up ──────────────────────────────────────────────────────────────
    # Remove empty sub_defs lists from senses
    for s in senses:
        if not s["sub_defs"]: s.pop("sub_defs")

    # Deduplicate sources, expand abbreviations
    seen = set()
    sources = []
    for s in entry_sources:
        if s not in seen:
            seen.add(s); sources.append(s)

    # Expand field + POS abbreviations for production dictionary data
    field = expand_field_label(field_raw) if field_raw else None
    pos = expand_pos(pos) if pos else None

    note = "\n".join(note_parts) if note_parts else None

    return {
        "headword":          entry["headword"],
        "headword_full":     headword_full,
        "alt_headwords":     entry.get("alt_headwords"),
        "letter":            entry["letter"],
        "homograph":         homograph,
        "pronunciation":     pronunciation or None,
        "part_of_speech":    pos or None,
        "field":             field or None,
        "etymology":         etymology or None,
        "conjugated_forms":  conjugated_forms or None,
        "adj_forms":         adj_forms or None,
        "noun_forms":        noun_forms or None,
        "plural":            plural or None,
        "singular":          singular or None,
        "plural_def":        plural_def or None,
        "derived_forms":     derived_forms or None,
        "chemical_formula":  chemical_formula or None,
        "taxonomy":          taxonomy or None,
        "hypernym":          hypernym or None,
        "subtypes":          subtypes or None,
        "wordnet_sense":     wordnet_sense or None,
        "senses":            senses or None,
        "collocations":      collocations or None,
        "synonyms":          synonyms or None,
        "antonyms":          ", ".join(antonyms_t) if antonyms_t else None,
        "contrasting":       contrasting or None,
        "usage":             usage or None,
        "derived_forms":     derived_forms or None,
        "see_also":          see_also or None,
        "cross_refs":        cross_refs or None,
        "alt_spellings":     alt_spells or None,
        "illu_examples":     illu_examples or None,
        "sources":           sources or None,
        "note":              note or None,
    }


# ── Log helpers ───────────────────────────────────────────────────────────────
def default_log_path(out):
    for s in (".json.gz", ".json"):
        if out.endswith(s): return out[:-len(s)] + ".log"
    return out + ".log"

def write_log(events, total, path):
    by_type = {}
    for ev in events:
        by_type.setdefault(ev.get("type", "other"), []).append(ev)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("GCIDE parse log\n================\n\n")
        fh.write(
            "Every source block that did NOT become a dictionary entry, plus\n"
            "non-fatal markup anomalies, are listed here with the reason.\n"
            "Each item includes source_file and line when available.\n\n"
            "Summary\n-------\n"
        )
        fh.write(f"Total entries written to JSON: {total}\n")
        for t, evs in sorted(by_type.items()):
            fh.write(f"{t}: {len(evs)}\n")
        for t, evs in sorted(by_type.items()):
            fh.write(f"\n\n{'=' * 70}\n{t} ({len(evs)})\n{'=' * 70}\n\n")
            for ev in evs:
                src = ev.get("source_file") or (
                    f"CIDE.{ev['letter']}" if ev.get("letter") else "?"
                )
                line = ev.get("line")
                loc = f"{src}:{line}" if line else src
                fh.write(f"[{loc}]\n")
                if ev.get("reason"):
                    fh.write(f"reason: {ev['reason']}\n")
                if ev.get("context"):
                    fh.write(f"context: {ev['context']!r}\n")
                fh.write("\n")
    # machine-readable JSONL sibling
    jsonl = path + ".jsonl" if not path.endswith(".jsonl") else path
    if not path.endswith(".jsonl"):
        with open(path + ".jsonl", "w", encoding="utf-8") as fh:
            for ev in events:
                if "source_file" not in ev and ev.get("letter"):
                    ev = {**ev, "source_file": f"CIDE.{ev['letter']}"}
                fh.write(__import__("json").dumps(ev, ensure_ascii=False) + "\n")



# ── Main ──────────────────────────────────────────────────────────────────────
def apply_webchr(text: str) -> str:
    """Replace \'xx hex escapes using WEBCHR_MAP."""
    if not WEBCHR_MAP:
        return text
    def repl(m):
        code = int(m.group(1), 16)
        return WEBCHR_MAP.get(code, m.group(0))
    return re.sub(r"\\'([0-9a-fA-F]{2})", repl, text)


def resolve_author(name: str) -> Optional[Dict[str, Any]]:
    """Look up quote author in AUTHORS_MAP (keyed by 'quoted as')."""
    if not name or not AUTHORS_MAP:
        return None
    raw = name.strip()
    # normalize whitespace/newlines from source
    raw = re.sub(r"\s+", " ", raw)
    candidates = [
        raw,
        raw.rstrip("."),
        raw.rstrip(".") + ".",
        raw.replace("\n", " "),
    ]
    info = None
    for key in candidates:
        if key in AUTHORS_MAP:
            info = AUTHORS_MAP[key]
            break
    if info is None:
        # fuzzy: case-insensitive exact
        low = {k.lower(): k for k in AUTHORS_MAP}
        for key in candidates:
            if key.lower() in low:
                info = AUTHORS_MAP[low[key.lower()]]
                break
    if not info:
        return None
    if isinstance(info, dict):
        return info
    return {"name": str(info)}


def author_display(name: str) -> str:
    """Replace short citation form with full author name when known."""
    if not name:
        return name
    info = resolve_author(name)
    if not info:
        return re.sub(r"\s+", " ", name.strip())
    full = info.get("name") or name
    dates = info.get("dates")
    if dates and dates != "ND":
        return f"{full} ({dates})"
    return full


def drop_nulls(obj: Any) -> Any:
    """Recursively drop None / empty list / empty dict for compact JSON."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            v2 = drop_nulls(v)
            if v2 is None or v2 == [] or v2 == {}:
                continue
            out[k] = v2
        return out
    if isinstance(obj, list):
        return [drop_nulls(x) for x in obj if x is not None and x != [] and x != {}]
    return obj


def parse_letter(path, letter, log=None):
    raw = open(path, encoding="utf-8", errors="replace").read()
    raw = strip_comments(raw)
    raw = apply_webchr(raw)
    nodes = parse_markup(raw, letter=letter, log=log)
    return segment_entries(nodes, letter, log=log)




def attach_authors(entry: Dict[str, Any]) -> None:
    """Replace short author forms with full display names only (no author_short / author_info)."""
    def fix_quote(q):
        if not isinstance(q, dict) or not q.get("author"):
            return
        short = re.sub(r"\s+", " ", str(q["author"]).strip())
        q["author"] = author_display(short)
        # drop any leftover diagnostic keys
        q.pop("author_short", None)
        q.pop("author_info", None)

    for s in entry.get("senses") or []:
        for q in s.get("quotations") or []:
            fix_quote(q)
    for q in entry.get("quotations") or []:
        fix_quote(q)


def main():
    ap = argparse.ArgumentParser(
        description="Build production GCIDE JSON for dictionary apps"
    )
    ap.add_argument("source_dir", help="Path to unpacked gcide-0.54 (CIDE.A … CIDE.Z)")
    ap.add_argument("output_dir", help="Output directory for front_matter.json + gcide_A.json … gcide_Z.json")
    ap.add_argument("--pretty", action="store_true", default=True,
                    help="Indent JSON (default: on)")
    ap.add_argument("--compact", action="store_true",
                    help="Disable pretty-printing (compact JSON)")
    ap.add_argument("--letters", default="", help="Comma letters, e.g. E,W (default: all)")
    ap.add_argument("--log", default=None, help="Issue log path (default: output_dir/gcide.log)")
    ap.add_argument("--keep-nulls", action="store_true",
                    help="Keep null/empty fields in JSON (default: drop for compact data)")
    args = ap.parse_args()
    if args.compact:
        args.pretty = False

    letters = [l.strip().upper() for l in args.letters.split(",") if l.strip()] or LETTERS
    out_dir = os.path.abspath(args.output_dir)
    os.makedirs(out_dir, exist_ok=True)
    log_path = args.log or os.path.join(out_dir, "gcide.log")
    t0 = time.time()

    all_entries: List[Dict[str, Any]] = []
    # Per-letter section payloads (no front_matter — that is dictionary-level only)
    by_letter: Dict[str, Dict[str, Any]] = {}
    dict_front_matter: List[Any] = []  # structured quotes once at dictionary start
    log_events: List[Dict[str, Any]] = []

    print(f"Entity map : {len(ENTITY_MAP)}  webchr: {len(WEBCHR_MAP)}", file=sys.stderr)
    print(f"Abbrev map : {len(ABBREV_MAP)}  authors: {len(AUTHORS_MAP)}", file=sys.stderr)

    for letter in letters:
        path = os.path.join(args.source_dir, "CIDE." + letter)
        if not os.path.isfile(path):
            print(f"  !! {path} not found, skipping", file=sys.stderr)
            continue
        print(f"Parsing CIDE.{letter} ...", file=sys.stderr)
        entries, front_matter, section_markers = parse_letter(path, letter, log=log_events)

        letter_entries: List[Dict[str, Any]] = []
        seen: Dict[str, int] = {}
        for e in entries:
            hw = e["headword"]
            seen[hw] = seen.get(hw, 0) + 1
            obj = extract(e, seen[hw])
            attach_authors(obj)
            if not args.keep_nulls:
                obj = drop_nulls(obj)
            letter_entries.append(obj)
            all_entries.append(obj)

        # One top-level section marker per alphabet letter (A, B, C, …).
        # Front matter is collected once at the dictionary root (not per letter).
        # Running-head text from the source (e.g. "NUMBERS.") kept as extras only.
        extras = [m for m in (section_markers or [])
                  if m.rstrip(".") != letter]
        payload: Dict[str, Any] = {
            "section_marker": letter,          # once per alphabet section
            "entries": letter_entries,
        }
        if extras:
            payload["other_markers"] = extras  # e.g. "NUMBERS." under A
        by_letter[letter] = payload
        # Capture genuine front matter once at dictionary start (A has the Locke
        # quote; F repeats it — skip duplicates). Ignore letter-head leftovers.
        for fm in (front_matter or []):
            if not fm:
                continue
            # skip pure running-heads like "E.\n[1913 Webster]"
            if isinstance(fm, str) and re.match(
                r"^[A-Z]\.\s*(\[1913 Webster\])?\s*$", fm
            ):
                continue
            # dedupe by quote text (structured) or full string
            key = fm.get("text") if isinstance(fm, dict) else fm
            existing = {
                (x.get("text") if isinstance(x, dict) else x)
                for x in dict_front_matter
            }
            if key in existing:
                continue
            dict_front_matter.append(fm)
        print(f"  {letter}: {len(entries)} entries"
              f", other_markers={extras or '[]'}",
              file=sys.stderr)

    kw = {"ensure_ascii": False, **({"indent": 2} if args.pretty else {})}

    # front_matter.json — once, at the beginning of the dictionary
    fm_path = os.path.join(out_dir, "front_matter.json")
    fm_obj = {"front_matter": dict_front_matter}
    print(f"Writing {fm_path} ({len(dict_front_matter)} item(s)) ...", file=sys.stderr)
    with open(fm_path, "w", encoding="utf-8") as fh:
        json.dump(fm_obj, fh, **kw)

    # Per-letter JSON files only (no bulky combined JSON).
    #   { "section_marker": "A", "entries": [...] }
    per_letter_paths: List[str] = []
    for letter in letters:
        if letter not in by_letter:
            continue
        payload = by_letter[letter]
        pl_path = os.path.join(out_dir, f"gcide_{letter}.json")
        n_ent = len(payload["entries"])
        print(f"Writing {pl_path} (section_marker={payload['section_marker']}, "
              f"{n_ent} entries) ...",
              file=sys.stderr)
        with open(pl_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, **kw)
        per_letter_paths.append(pl_path)

    print(f"Writing {log_path} ...", file=sys.stderr)
    write_log(log_events, len(all_entries), log_path)

    elapsed = round(time.time() - t0, 2)
    print(
        f"Done. {len(all_entries)} entries, {len(log_events)} log events, {elapsed}s.",
        file=sys.stderr,
    )
    summary = f"  front_matter : {fm_path}\n"
    if per_letter_paths:
        summary += f"  Per-letter   : {len(per_letter_paths)} files "
        summary += f"({os.path.basename(per_letter_paths[0])} … "
        summary += f"{os.path.basename(per_letter_paths[-1])})\n"
    summary += f"  Log          : {log_path}"
    print(summary, file=sys.stderr)


if __name__ == "__main__":
    main()
