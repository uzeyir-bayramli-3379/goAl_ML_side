"""Generate grounded overviews for Baku landmarks, one Gemini Flash call each.

Offline batch script. Reads baku_landmarks_clean.csv, produces per-landmark
{"card_summary", "narrative", "retrieval"} via gemini-2.5-flash, writes
landmark_overviews.csv. Resumable via an append-only JSONL checkpoint keyed on
Wikidata_ID:register, so a second voice register regenerates alongside the first.

This script reads a CSV and writes a CSV. It NEVER connects to a database and
must NEVER be imported by the backend — generate_overview(row, client) is the
single reusable unit if the backend ever wants cache-aside generation.

Run:
    python generate_overviews.py               # all 305 rows
    python generate_overviews.py --limit 10     # first 10 rows
    python generate_overviews.py --thinnest 20  # 20 rows with the thinnest extract
    python generate_overviews.py --thinnest-with-extract 20  # 20 shortest NON-NULL extracts
    python generate_overviews.py --median 20    # 20 rows nearest the median extract length
    python generate_overviews.py --fattest 20   # 20 rows with the longest extracts
    python generate_overviews.py --dry-run      # print prompts, no API calls
"""

import argparse
import json
import os
import time

import pandas as pd
from dotenv import load_dotenv
from google import genai
from google.genai import types

from prompts import (
    FACT_COLUMNS,
    REGISTERS,
    DEFAULT_REGISTER,
    build_facts_block,
)

# --- Tunables -------------------------------------------------------------
INPUT_CSV = "baku_landmarks_clean.csv"
DEFAULT_OUT = "landmark_overviews.csv"
CHECKPOINT_FILE = "landmark_overviews_checkpoint.jsonl"

MODEL = "gemini-2.5-flash"
PACING = 0.5              # seconds between calls — sequential, be polite
MAX_RETRIES = 4          # same budget as enrich_landmarks.py::_fetch_wiki_extract
BACKOFF_BASE = 2         # seconds; wait grows BACKOFF_BASE * attempt

EXPECTED_KEYS = ("card_summary", "narrative", "retrieval")

# Failure/completion discipline (same shape as enrich_landmarks.py's sentinel,
# expressed through the return type): generate_overview returns a dict on success
# and None on failure. A valid result whose "narrative" is null is still a dict,
# so it is a COMPLETION and gets checkpointed; only a None return (API error or
# unparseable JSON after retries) is skipped, so a re-run retries it. The two are
# disjoint, so None is an unambiguous failure signal and no extra sentinel object
# is needed.


# --- JSON parsing ---------------------------------------------------------

def _strip_fences(text):
    """Gemini wraps JSON in ```json fences despite being told not to. Strip a
    leading ```json / ``` and a trailing ``` if present."""
    t = text.strip()
    if t.startswith("```"):
        t = t[3:]
        if t[:4].lower() == "json":
            t = t[4:]
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _parse_response(text):
    """Parsed dict with exactly the expected keys, or None if unusable.

    Missing any of card_summary/narrative/retrieval is a FAILURE, not a partial
    success — a response without "retrieval" cannot be served, so we refuse it."""
    try:
        data = json.loads(_strip_fences(text))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if any(k not in data for k in EXPECTED_KEYS):
        return None
    return {k: data[k] for k in EXPECTED_KEYS}


# --- Core: one landmark in, parsed dict out -------------------------------

def generate_overview(row, client, system_prompt):
    """Generate one landmark's overview under the given voice register.

    `system_prompt` is the register's system instruction (REGISTERS[name]); the
    caller owns register selection so the same row can be regenerated in a second
    voice without this function knowing about registers.

    Returns the parsed {"card_summary", "narrative", "retrieval"} dict, or None
    if the call failed / stayed unparseable after a retry. No file I/O, no CSV
    or cache awareness — the caller owns batching, checkpointing and pacing. Kept
    this shape so the backend can reuse it cache-aside without untangling it.

    Retry policy: bounded loop (never recursion). Network / 429 / 5xx get backoff
    and a retry. A parseable-but-wrong response (fences, missing key) gets ONE
    extra attempt with a "valid JSON only" nudge appended, then fails."""
    facts = build_facts_block(row, FACT_COLUMNS)
    base_contents = f"FACTS:\n{facts}"

    for attempt in range(1, MAX_RETRIES + 1):
        contents = base_contents
        if attempt > 1:
            contents += ("\n\nReturn ONLY valid JSON with keys "
                         "card_summary, narrative, retrieval. No markdown fences.")
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.3,
                    response_mime_type="application/json",
                ),
            )
        except Exception as e:  # SDK raises for 429/5xx/network alike
            msg = str(e)
            transient = any(s in msg for s in ("429", "500", "502", "503", "504",
                                               "RESOURCE_EXHAUSTED", "UNAVAILABLE",
                                               "DEADLINE", "timeout", "Timeout"))
            print(f"  API error (attempt {attempt}/{MAX_RETRIES}): {msg[:160]}")
            if transient and attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE * attempt)
                continue
            return None

        parsed = _parse_response(resp.text)
        if parsed is not None:
            return parsed

        print(f"  unparseable/incomplete JSON (attempt {attempt}/{MAX_RETRIES})")
        if attempt < MAX_RETRIES:
            time.sleep(BACKOFF_BASE * attempt)
            continue

    return None


# --- Checkpointing (append-only JSONL, keyed by Wikidata_ID:register) -------

def _ckpt_key(qid, register):
    """Composite key so a second register generates ALONGSIDE the first rather
    than colliding — the same landmark can hold one record per voice."""
    return f"{qid}:{register}"


def _load_checkpoint(path=CHECKPOINT_FILE):
    """Return {f"{Wikidata_ID}:{register}": record} for every overview already
    generated. A torn final line (interrupted write) costs one record,
    re-generated next run. Records predating the register column are treated as
    DEFAULT_REGISTER so old checkpoints resume cleanly."""
    done = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                register = rec.get("register", DEFAULT_REGISTER)
                done[_ckpt_key(rec["Wikidata_ID"], register)] = rec
    return done


def _append_checkpoint(rec, path=CHECKPOINT_FILE):
    """Append one record. No os.replace / whole-file rewrite: os.replace raises
    PermissionError (WinError 5) when OneDrive or an open editor holds the target,
    and rewriting a growing file 305 times is quadratic. Appending is constant
    time and has no swap window to corrupt."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()


# --- Row selection --------------------------------------------------------

def _extract_len(row):
    """wiki_extract length; a missing/blank extract counts as 0 (sorts first —
    thinnest grounding is the worst case we most want to test)."""
    val = row.get("wiki_extract")
    return len(val) if isinstance(val, str) else 0


def select_rows(df, limit=None, thinnest=None, thinnest_with_extract=None,
                median=None, fattest=None):
    if thinnest_with_extract is not None:
        # SHORTEST NON-NULL extract. --thinnest sorts the 31 null-extract rows to
        # length 0, so it can never reach a real-but-short row (p25 ~= 255 chars) —
        # the case covering most of the 305 and never yet tested. Exclude nulls.
        idx = [i for i in range(len(df)) if _extract_len(df.iloc[i]) > 0]
        idx.sort(key=lambda i: _extract_len(df.iloc[i]))
        return df.iloc[idx[:thinnest_with_extract]]
    if median is not None:
        # Rows whose NON-NULL extract length is closest to the median — the
        # typical case, covering most of the 305. Nulls excluded so the median
        # reflects real source text, not the 31 empties dragging it toward 0.
        idx = [i for i in range(len(df)) if _extract_len(df.iloc[i]) > 0]
        med = pd.Series([_extract_len(df.iloc[i]) for i in idx]).median()
        idx.sort(key=lambda i: abs(_extract_len(df.iloc[i]) - med))
        return df.iloc[idx[:median]]
    if fattest is not None:
        # LONGEST extract (max ~2822 chars). Tests whether the model info-dumps
        # when given abundant material — "choose one detail and make it land" has
        # only ever been tested against thin rows.
        order = sorted(range(len(df)), key=lambda i: _extract_len(df.iloc[i]),
                       reverse=True)
        return df.iloc[order[:fattest]]
    if thinnest is not None:
        order = sorted(range(len(df)), key=lambda i: _extract_len(df.iloc[i]))
        return df.iloc[order[:thinnest]]
    if limit is not None:
        return df.iloc[:limit]
    return df


# --- Orchestration --------------------------------------------------------

def run(df, out_path, register=DEFAULT_REGISTER, dry_run=False):
    system_prompt = REGISTERS[register]
    if dry_run:
        for _, row in df.iterrows():
            print("=" * 70)
            print(f"[{row['Wikidata_ID']}] {row['Name']}  (register: {register})")
            print("-" * 70)
            print(f"SYSTEM:\n{system_prompt}\n")
            print(f"USER:\nFACTS:\n{build_facts_block(row, FACT_COLUMNS)}")
        print("=" * 70)
        print(f"Dry run: {len(df)} prompt(s) built, no API calls made.")
        return

    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY not set in environment (.env).")
    client = genai.Client(api_key=api_key)

    done = _load_checkpoint()
    print(f"{len(done)} already in checkpoint; {len(df)} row(s) selected "
          f"(register: {register}).")

    failed = []
    for _, row in df.iterrows():
        qid = row["Wikidata_ID"]
        key = _ckpt_key(qid, register)
        if key in done:
            continue

        result = generate_overview(row, client, system_prompt)
        if result is None:
            failed.append((qid, row["Name"]))
            print(f"FAILED  {qid}  {row['Name']}")
            continue

        rec = {"Wikidata_ID": qid, "name": row["Name"],
               "register": register, **result}
        _append_checkpoint(rec)
        done[key] = rec
        tag = "narrative=null" if result["narrative"] is None else "ok"
        print(f"done    {qid}  {row['Name']}  ({tag})")
        time.sleep(PACING)

    _write_csv(df, done, out_path, register)

    generated = sum(1 for _, row in df.iterrows()
                    if _ckpt_key(row["Wikidata_ID"], register) in done)
    null_narr = sum(1 for _, row in df.iterrows()
                    if _ckpt_key(row["Wikidata_ID"], register) in done
                    and done[_ckpt_key(row["Wikidata_ID"], register)]["narrative"] is None)
    print("\n--- Summary ---")
    print(f"Generated (this + prior runs, for selected rows): {generated}")
    print(f"  narrative=null: {null_narr}")
    print(f"  failed this run: {len(failed)}")
    if failed:
        print("  FAILED (not cached — re-run to retry):")
        for qid, name in failed:
            print(f"    - {qid}  {name}")
        print("  Run is INCOMPLETE.")


def _write_csv(df, done, out_path, register):
    """Emit the selected rows that have a checkpoint record for THIS register, in
    df order. register is a plain string column on each row."""
    cols = ["Wikidata_ID", "name", "register",
            "card_summary", "narrative", "retrieval"]
    rows = [done[_ckpt_key(row["Wikidata_ID"], register)]
            for _, row in df.iterrows()
            if _ckpt_key(row["Wikidata_ID"], register) in done]
    pd.DataFrame(rows, columns=cols).to_csv(out_path, index=False)
    print(f"Wrote {len(rows)} row(s) -> {out_path}")


def main():
    ap = argparse.ArgumentParser(description="Generate grounded landmark overviews.")
    ap.add_argument("--limit", type=int, help="only the first N rows")
    ap.add_argument("--thinnest", type=int,
                    help="the N rows with the shortest wiki_extract (worst-case "
                         "grounding test; no-extract rows count as length 0)")
    ap.add_argument("--thinnest-with-extract", type=int,
                    help="the N rows with the shortest NON-NULL wiki_extract "
                         "(excludes rows with no extract; tests real-but-short text)")
    ap.add_argument("--median", type=int,
                    help="the N rows whose NON-NULL extract length is closest to "
                         "the median (the typical case, most of the 305)")
    ap.add_argument("--fattest", type=int,
                    help="the N rows with the longest wiki_extract (tests whether "
                         "the model info-dumps on abundant material)")
    ap.add_argument("--register", default=DEFAULT_REGISTER, choices=sorted(REGISTERS),
                    help=f"voice register / system prompt (default {DEFAULT_REGISTER}); "
                         "checkpointed per-register so voices coexist")
    ap.add_argument("--dry-run", action="store_true",
                    help="build and print prompts, make no API calls")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output CSV path")
    args = ap.parse_args()

    modes = [args.limit, args.thinnest, args.thinnest_with_extract,
             args.median, args.fattest]
    if sum(m is not None for m in modes) > 1:
        raise SystemExit("--limit, --thinnest, --thinnest-with-extract, --median "
                         "and --fattest are mutually exclusive.")

    df = pd.read_csv(INPUT_CSV)
    df = select_rows(df, limit=args.limit, thinnest=args.thinnest,
                     thinnest_with_extract=args.thinnest_with_extract,
                     median=args.median, fattest=args.fattest)
    run(df, out_path=args.out, register=args.register, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
