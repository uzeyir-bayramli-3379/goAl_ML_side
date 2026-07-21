"""GoAI Baku landmark enrichment pipeline.

Stage 2 on top of the discovery extractor (data_getting.py): takes the skeleton
landmarks (name / coords / type / sitelinks) and pulls per-landmark FACTS from
Wikidata so a Gemini content-generation call can be grounded and not hallucinate.

Design notes:
  - Enrichment is a SEPARATE, QID-keyed SPARQL (VALUES ?item {...}), NOT bolted
    onto the discovery query. Discovery already runs a double transitive closure
    (P131+ with P279*) and flirts with WDQS timeouts; pinning the item set with
    VALUES removes the closure, so ~8 OPTIONALs stay cheap and reliable.
  - Two hard project rules: (1) missing property => None, never "" or a guess;
    (2) flag ambiguous data, drop only unambiguous junk (the latter is
    clean_landmarks()'s job, reused verbatim).
  - Resumable: each enriched QID is checkpointed to disk as one JSON line. If a
    batch fails, re-running only re-queries the QIDs not yet checkpointed.

Run:
    python enrich_landmarks.py            # discovery -> enrich -> clean -> CSVs
    python enrich_landmarks.py --wiki     # also fetch Wikipedia intro extracts
"""

import argparse
import json
import os
import time
import urllib.parse

import pandas as pd
import requests

from data_getting import discover_landmarks, headers, SPARQL_URL
from clean_landmarks import clean_landmarks

# --- Tunables -------------------------------------------------------------
BATCH_SIZE = 150          # QIDs per enrichment request; VALUES pins them, so no closure
SLEEP_BETWEEN = 1.0       # seconds between WDQS batches — be polite, don't hammer
REQUEST_TIMEOUT = 90      # enrichment is heavier than discovery per row

CHECKPOINT_FILE = "enrichment_checkpoint.jsonl"   # one JSON object per QID
WIKI_CACHE_FILE = "wiki_extracts.jsonl"           # one JSON object per enwiki title
CLEAN_CSV = "baku_landmarks_clean.csv"
DROPPED_CSV = "baku_dropped.csv"

# Multi-valued QID props: raw var -> (P-id, output field). Labels resolved by the
# label SERVICE, deterministically sorted+joined in Python (same discipline as
# All_Types). Pipe separator inside SPARQL so a comma inside a label can't split it.
MULTI_PROPS = {
    "style":       ("P149", "architectural_style"),
    "architect":   ("P84",  "architect"),
    "founder":     ("P112", "founded_by"),
    "creator":     ("P170", "creator"),
    "material":    ("P186", "material_used"),
    "namedAfter":  ("P138", "named_after"),
    "heritage":    ("P1435", "heritage_designation"),
}

# Single-valued props: raw var -> (P-id, output field). SAMPLE'd in SPARQL.
SINGLE_PROPS = {
    "inception": ("P571",  "inception"),
    "opening":   ("P1619", "official_opening"),
    "height":    ("P2048", "height_m"),
}

# Every enrichment field, so we can null-fill uniformly (absence must be visible).
ENRICH_FIELDS = (
    [f for _, f in SINGLE_PROPS.values()]
    + [f for _, f in MULTI_PROPS.values()]
    + ["image", "description", "enwiki_title", "enwiki_url"]
)


def _build_enrich_query(qids):
    """One aggregated row per item. GROUP_CONCAT(DISTINCT) keeps multi-valued
    props from exploding the row count via OPTIONAL cross-products; Python then
    re-sorts for determinism. SAMPLE for single-valued literals.

    QID-valued props are labelled with an EXPLICIT rdfs:label join, NOT the
    wikibase:label auto-service: the auto-service silently fails to resolve
    labels referenced inside GROUP_CONCAT under GROUP BY (verified 2026-07-19 —
    it returned empty for every architect/style/heritage value)."""
    values = " ".join(f"wd:{q}" for q in qids)

    single_selects = "\n  ".join(
        f"(SAMPLE(?{v}) AS ?{v}_out)" for v in SINGLE_PROPS
    )
    multi_selects = "\n  ".join(
        f'(GROUP_CONCAT(DISTINCT ?{v}Label; separator="|") AS ?{v}_out)'
        for v in MULTI_PROPS
    )
    single_optionals = "\n  ".join(
        f"OPTIONAL {{ ?item wdt:{pid} ?{v}. }}"
        for v, (pid, _) in SINGLE_PROPS.items()
    )
    multi_optionals = "\n  ".join(
        f'OPTIONAL {{ ?item wdt:{pid} ?{v}. ?{v} rdfs:label ?{v}Label. '
        f'FILTER(LANG(?{v}Label) = "en") }}'
        for v, (pid, _) in MULTI_PROPS.items()
    )

    return f"""
SELECT ?item
  {single_selects}
  {multi_selects}
  (SAMPLE(?image) AS ?image_out)
  (SAMPLE(?description) AS ?description_out)
  (SAMPLE(?article) AS ?enwiki_url_out)
  (SAMPLE(?enwikiTitle) AS ?enwiki_title_out)
WHERE {{
  VALUES ?item {{ {values} }}
  {single_optionals}
  {multi_optionals}
  OPTIONAL {{ ?item wdt:P18 ?image. }}
  OPTIONAL {{ ?item schema:description ?description. FILTER(LANG(?description) = "en") }}
  OPTIONAL {{
    ?article schema:about ?item ;
             schema:isPartOf <https://en.wikipedia.org/> ;
             schema:name ?enwikiTitle .
  }}
}}
GROUP BY ?item
"""


def _cell(binding, key):
    """SPARQL binding value or None. Empty GROUP_CONCAT ("") also -> None so an
    absent multi-valued prop reads as null, not an empty string."""
    v = binding.get(key, {}).get("value")
    if v is None or v == "":
        return None
    return v


def _join_multi(raw):
    """Pipe-joined GROUP_CONCAT -> deterministic ", "-joined string, or None."""
    if not raw:
        return None
    parts = sorted({p.strip() for p in raw.split("|") if p.strip()})
    return ", ".join(parts) or None


def _image_filename(url):
    """P18 comes back as a Special:FilePath URL; keep just the decoded filename."""
    if not url:
        return None
    return urllib.parse.unquote(url.rsplit("/", 1)[-1]) or None


def _parse_enrich_row(b):
    """One aggregated SPARQL row -> flat enrichment dict, null-disciplined."""
    qid = b["item"]["value"].rsplit("/", 1)[-1]
    rec = {"Wikidata_ID": qid}

    for v, (_, field) in SINGLE_PROPS.items():
        rec[field] = _cell(b, f"{v}_out")
    for v, (_, field) in MULTI_PROPS.items():
        rec[field] = _join_multi(_cell(b, f"{v}_out"))

    rec["image"] = _image_filename(_cell(b, "image_out"))
    rec["description"] = _cell(b, "description_out")
    rec["enwiki_title"] = _cell(b, "enwiki_title_out")
    rec["enwiki_url"] = _cell(b, "enwiki_url_out")
    return rec


# --- Checkpointing --------------------------------------------------------

def _load_checkpoint(path=CHECKPOINT_FILE):
    """Return {QID: enrichment_dict} for everything already fetched."""
    done = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    done[rec["Wikidata_ID"]] = rec
    return done


def _append_checkpoint(records, path=CHECKPOINT_FILE):
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()


def _batches(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# --- Enrichment driver ----------------------------------------------------

def enrich(qids, verbose=True):
    """Fetch facts for every QID, resuming from checkpoint. Returns {QID: dict}.
    Guarantees a record for EVERY input QID (all-null if Wikidata had nothing),
    so the downstream merge can never silently drop a discovered landmark."""
    done = _load_checkpoint()
    todo = [q for q in qids if q not in done]
    if verbose:
        print(f"Enrichment: {len(done)} cached, {len(todo)} to fetch "
              f"in {(len(todo) + BATCH_SIZE - 1) // BATCH_SIZE} batch(es).")

    for i, batch in enumerate(_batches(todo, BATCH_SIZE), 1):
        query = _build_enrich_query(batch)
        r = requests.get(SPARQL_URL, params={"query": query, "format": "json"},
                         headers=headers, timeout=REQUEST_TIMEOUT)
        if r.status_code == 429:
            print(f"Rate limited on batch {i}. Retry-After: "
                  f"{r.headers.get('Retry-After')}s. Re-run to resume.")
            break
        if not r.ok:
            # WDQS timeout => HTTP 500 with a Java stack in the body. Surface it,
            # then stop — checkpoint already holds prior batches, so resume is safe.
            print(f"Batch {i} HTTP {r.status_code}. Body (first 500):\n{r.text[:500]}")
            break

        bindings = r.json()["results"]["bindings"]
        found = {}
        for b in bindings:
            rec = _parse_enrich_row(b)
            found[rec["Wikidata_ID"]] = rec

        # Items with zero matched OPTIONALs never appear in results -> null record,
        # so absence stays visible and the batch is fully accounted for.
        records = [found.get(q, {"Wikidata_ID": q, **{f: None for f in ENRICH_FIELDS}})
                   for q in batch]
        _append_checkpoint(records)
        done.update({rec["Wikidata_ID"]: rec for rec in records})

        if verbose:
            hit = sum(1 for q in batch if q in found)
            print(f"  batch {i}: {len(batch)} items, {hit} with >=1 fact")
        time.sleep(SLEEP_BETWEEN)

    return done


def merge(landmarks, enrichment):
    """Merge enrichment fields onto the discovery dict IN PLACE, null-filling any
    field the enrichment map lacks so every row has the full column set."""
    for qid, lm in landmarks.items():
        facts = enrichment.get(qid, {})
        for field in ENRICH_FIELDS:
            lm[field] = facts.get(field)  # .get -> None if enrichment missed it
    return landmarks


# --- Stage 3 (optional): Wikipedia intro extracts -------------------------

WIKI_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"


def _load_wiki_cache(path=WIKI_CACHE_FILE):
    cache = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    cache[rec["title"]] = rec["extract"]
    return cache


def _fetch_wiki_extract(title):
    """Intro extract for one enwiki article, or None on 404/failure."""
    url = WIKI_SUMMARY_URL.format(title=urllib.parse.quote(title.replace(" ", "_")))
    try:
        r = requests.get(url, headers=headers, timeout=30)
    except requests.exceptions.RequestException:
        return None
    if r.status_code == 404:
        return None
    if r.status_code == 429:
        wait = int(r.headers.get("Retry-After", 5))
        print(f"  wiki 429 on '{title}', sleeping {wait}s")
        time.sleep(wait)
        return _fetch_wiki_extract(title)
    if not r.ok:
        return None
    return r.json().get("extract") or None


def add_wiki_extracts(clean_df, verbose=True):
    """Add a `wiki_extract` column. Only fetched for rows where core_zone is True
    OR sitelinks >= 10 (Wikidata is thin on descriptive prose; this fills the gap
    for the landmarks users actually stand in front of). Cached + resumable."""
    cache = _load_wiki_cache()
    extracts = []
    fetched = 0

    for _, row in clean_df.iterrows():
        title = row.get("enwiki_title")
        eligible = bool(row.get("core_zone")) or row.get("Sitelinks", 0) >= 10
        if not (eligible and title):
            extracts.append(None)
            continue

        if title in cache:
            extracts.append(cache[title])
            continue

        extract = _fetch_wiki_extract(title)
        cache[title] = extract
        with open(WIKI_CACHE_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"title": title, "extract": extract},
                                ensure_ascii=False) + "\n")
        extracts.append(extract)
        fetched += 1
        time.sleep(0.5)  # polite pacing on the public REST API

    clean_df = clean_df.copy()
    clean_df["wiki_extract"] = extracts
    if verbose:
        got = sum(1 for e in extracts if e)
        print(f"Wiki extracts: {got} present ({fetched} freshly fetched).")
    return clean_df


# --- Orchestration --------------------------------------------------------

def main(with_wiki=False):
    landmarks = discover_landmarks()
    enrichment = enrich(list(landmarks.keys()))
    merge(landmarks, enrichment)

    clean_df, report = clean_landmarks(landmarks)

    if with_wiki:
        clean_df = add_wiki_extracts(clean_df)

    clean_df.to_csv(CLEAN_CSV, index=False)
    report.to_csv(DROPPED_CSV, index=False)
    print(f"\nWrote {len(clean_df)} rows -> {CLEAN_CSV}")
    print(f"Wrote {len(report)} rows -> {DROPPED_CSV}")
    return clean_df, report


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Enrich Baku landmarks with grounding facts.")
    ap.add_argument("--wiki", action="store_true",
                    help="also fetch Wikipedia intro extracts (stage 3)")
    args = ap.parse_args()
    main(with_wiki=args.wiki)
