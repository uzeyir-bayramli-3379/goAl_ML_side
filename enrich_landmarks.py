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
WIKI_CACHE_FILE = "wiki_extracts_multilang.jsonl"  # one JSON object per {lang}:{title}
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
    + ["image", "description",
       "enwiki_title", "enwiki_url", "azwiki_title", "ruwiki_title"]
)


def _build_enrich_query(qids):
    """One aggregated row per item. GROUP_CONCAT(DISTINCT) keeps multi-valued
    props from exploding the row count via OPTIONAL cross-products; Python then
    re-sorts for determinism. SAMPLE for single-valued literals.

    QID-valued props are labelled with an EXPLICIT rdfs:label join, NOT the
    wikibase:label auto-service: the auto-service silently fails to resolve
    labels referenced inside GROUP_CONCAT under GROUP BY (verified 2026-07-19 —
    it returned empty for every architect/style/heritage value).

    NOTE: the Sitelinks *count* is produced by discovery (data_getting.py), not
    here. If that count includes non-Wikipedia sitelinks (Commons etc.), an entity
    with an image gets a free +1 — it should be restricted to schema:isPartOf
    ending in wikipedia.org. Not changed in this file."""
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
  (SAMPLE(?azwikiTitle) AS ?azwiki_title_out)
  (SAMPLE(?ruwikiTitle) AS ?ruwiki_title_out)
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
  # az/ru only: fr/uk/ka/fa sitelinks exist on many entities but are downstream
  # translations with near-zero marginal facts. These feed the wiki-extract
  # fallback when the enwiki lead is thin or absent.
  OPTIONAL {{
    ?azArticle schema:about ?item ;
               schema:isPartOf <https://az.wikipedia.org/> ;
               schema:name ?azwikiTitle .
  }}
  OPTIONAL {{
    ?ruArticle schema:about ?item ;
               schema:isPartOf <https://ru.wikipedia.org/> ;
               schema:name ?ruwikiTitle .
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
    rec["azwiki_title"] = _cell(b, "azwiki_title_out")
    rec["ruwiki_title"] = _cell(b, "ruwiki_title_out")
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

WIKI_PARAMS_BASE = {
    "action": "query",
    "format": "json",
    "formatversion": 2,   # pages becomes a list, not a dict keyed by pageid
    "prop": "extracts",
    "exintro": 1,         # lead section only, not the whole article
    "explaintext": 1,     # plain text, not HTML
    "redirects": 1,       # REST followed these silently; Action API does not
}

# The imported `headers` is the Wikidata/SPARQL one (Accept: sparql-results+json),
# wrong for the Wikipedia REST API. Wikimedia asks for a descriptive User-Agent
# with contact info and can throttle/block generic or absent UAs.
WIKI_HEADERS = {
    "User-Agent": ("GoAI/1.0 landmark-grounding "
                   "(https://github.com/uzeyir-bayramli-3379; uzeyir00b3379@gmail.com)"),
    "Accept": "application/json",
}

WIKI_MAX_RETRIES = 4      # same retry budget as get_amenities_places.py::_post
WIKI_BACKOFF_BASE = 2     # seconds; wait grows WIKI_BACKOFF_BASE * attempt

# Distinct from None: None means "genuinely no article" (cacheable); this means
# "transient failure after retries" and must NEVER be written to the cache, so a
# later run retries instead of remembering a dropped connection as permanent absence.
_FETCH_FAILED = object()


def _load_wiki_cache(path=WIKI_CACHE_FILE):
    """Return {f"{lang}:{title}": extract}. Keyed on lang:title, NOT bare title:
    three language wikis share this dict and can hold the same title, so a bare
    key would collide and serve the wrong-language article as a silent wrong
    answer. New filename on purpose — the old en-only cache keyed on bare titles
    would otherwise mask every en title as a stale hit."""
    cache = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    cache[f"{rec['lang']}:{rec['title']}"] = rec["extract"]
    return cache


def _fetch_wiki_extract(title, lang="en"):
    """Intro extract (full lead section) for one article on the {lang} wikipedia.
    The Action API contract is identical across language wikis. Returns:
      - str            : the extract text
      - None           : article genuinely absent / invalid title / empty lead
                         — safe to cache as permanent absence
      - _FETCH_FAILED  : transient failure after retries — caller must NOT cache.

    NOTE: unlike the REST summary endpoint, api.php returns HTTP 200 for a
    missing article. Absence is signalled INSIDE the body, not by status code."""
    api_url = f"https://{lang}.wikipedia.org/w/api.php"
    params = dict(WIKI_PARAMS_BASE, titles=title)
    for attempt in range(1, WIKI_MAX_RETRIES + 1):
        try:
            r = requests.get(api_url, params=params,
                             headers=WIKI_HEADERS, timeout=30)
        except requests.exceptions.RequestException as e:
            print(f"  wiki network error on '{title}' "
                  f"(attempt {attempt}/{WIKI_MAX_RETRIES}): {e}")
            time.sleep(WIKI_BACKOFF_BASE * attempt)
            continue

        # No 404 branch any more. If api.php itself 404s, the URL is wrong —
        # that must fail loudly as _FETCH_FAILED, never cache as absence.
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After") or WIKI_BACKOFF_BASE * attempt)
            print(f"  wiki 429 on '{title}', sleeping {wait}s "
                  f"(attempt {attempt}/{WIKI_MAX_RETRIES})")
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            wait = WIKI_BACKOFF_BASE * attempt
            print(f"  wiki {r.status_code} on '{title}'; backing off {wait}s "
                  f"(attempt {attempt}/{WIKI_MAX_RETRIES})")
            time.sleep(wait)
            continue
        if not r.ok:
            print(f"  wiki HTTP {r.status_code} on '{title}' "
                  f"(attempt {attempt}/{WIKI_MAX_RETRIES})")
            time.sleep(WIKI_BACKOFF_BASE * attempt)
            continue

        try:
            data = r.json()
        except ValueError:
            print(f"  wiki non-JSON body on '{title}' "
                  f"(attempt {attempt}/{WIKI_MAX_RETRIES})")
            time.sleep(WIKI_BACKOFF_BASE * attempt)
            continue

        # API-level error (bad params, malformed title). Permanent for this title.
        if "error" in data:
            print(f"  wiki API error on '{title}': "
                  f"{data['error'].get('code')} — {data['error'].get('info')}")
            return None

        pages = data.get("query", {}).get("pages")
        if not pages:
            # Unexpected shape, not a stated absence — treat as transient.
            print(f"  wiki unexpected response shape on '{title}' "
                  f"(attempt {attempt}/{WIKI_MAX_RETRIES})")
            time.sleep(WIKI_BACKOFF_BASE * attempt)
            continue

        page = pages[0]
        if page.get("missing") or page.get("invalid"):
            return None  # genuinely no article — correct to remember
        extract = page.get("extract") or None

        # Small wikis consolidate: a sitelink can resolve to a collective/parent
        # article (ten mosques -> one page with ten sections). The extract is then
        # grounded text about the WRONG building, which the grounding contract
        # can't catch. Print-and-continue — never auto-reject, the extract may
        # still be usable and this is a human signal.
        returned = page.get("title", "")
        if returned != title:
            print(f"  TITLE MISMATCH [{lang}]: asked '{title}', got '{returned}'")

        # No-redirect variant of the same problem: sitelink points straight at the
        # parent, no redirect logged. If neither the full title nor its first
        # significant word appears in the lead, warn.
        if extract:
            words = [w for w in title.split() if len(w) > 2] or title.split()
            head = extract[:200].lower()
            if title.lower() not in head and (not words or words[0].lower() not in head):
                print(f"  NAME NOT IN EXTRACT [{lang}]: '{title}' absent from lead")

        return extract

    return _FETCH_FAILED

def add_wiki_extracts(clean_df, verbose=True):
    """Add `wiki_extract` + `wiki_extract_lang` columns.

    Wikidata is thin on descriptive prose; the Wikipedia lead fills the gap so
    generation has real text to ground on. Content often lives in az/ru rather
    than en, so this falls back across the three sitelinks:

        en = fetch(enwiki) if present
        if en and len(en) >= 400:  use en   (400 is just below the en median —
                                             healthy rows are never touched)
        else: pick the LONGEST of {en, az, ru}

    LONGEST WINS — never concatenate: language editions disagree on dates and
    attributions, so two sources means the model silently picks one and you can't
    tell which. One source per landmark. `wiki_extract_lang` records the winner,
    required for the CC BY-SA attribution line ("from Wikipedia" is insufficient;
    the specific edition must be named).

    Cached + resumable, keyed on {lang}:{title}. Only genuine 404/empty results
    are cached; transient failures (_FETCH_FAILED) are left uncached so a re-run
    retries them — a failed az fetch is NOT recorded as 'az has no article.'"""
    cache = _load_wiki_cache()
    failed = []

    def fetch(title, lang):
        """Cache-aside single-article fetch. None on absence/blank/transient
        failure; transient failures are logged in `failed` and never cached."""
        if not isinstance(title, str) or not title.strip():
            return None
        title = title.strip()
        key = f"{lang}:{title}"
        if key in cache:
            return cache[key]
        extract = _fetch_wiki_extract(title, lang)
        if extract is _FETCH_FAILED:
            failed.append(key)
            return None  # not cached — next run retries
        cache[key] = extract
        with open(WIKI_CACHE_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"lang": lang, "title": title, "extract": extract},
                                ensure_ascii=False) + "\n")
        time.sleep(0.5)  # polite pacing, only on a real network fetch
        return extract

    extracts = []
    langs = []
    for _, row in clean_df.iterrows():
        en = fetch(row.get("enwiki_title"), "en")
        if en and len(en) >= 400:
            extracts.append(en)
            langs.append("en")
            continue

        candidates = [
            ("en", en),
            ("az", fetch(row.get("azwiki_title"), "az")),
            ("ru", fetch(row.get("ruwiki_title"), "ru")),
        ]
        best_lang, best = None, None
        for lang, cand in candidates:
            if cand and (best is None or len(cand) > len(best)):
                best_lang, best = lang, cand
        extracts.append(best)
        langs.append(best_lang)

    clean_df = clean_df.copy()
    clean_df["wiki_extract"] = extracts
    clean_df["wiki_extract_lang"] = langs
    if verbose:
        got = sum(1 for e in extracts if e)
        none_count = sum(1 for e in extracts if not e)
        by_lang = {}
        for l in langs:
            if l:
                by_lang[l] = by_lang.get(l, 0) + 1
        print(f"Wiki extracts: {got} present.")
        for l in ("en", "az", "ru"):
            print(f"  {l}: {by_lang.get(l, 0)} row(s) won by this edition")
        print(f"  no extract in any of en/az/ru: {none_count}")
        if failed:
            print(f"  WARNING: {len(failed)} fetch(es) failed after retries and were "
                  f"NOT cached (transient errors):")
            for k in failed:
                print(f"    - {k}")
            print("  Run is INCOMPLETE — re-run with --wiki to retry these.")
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
