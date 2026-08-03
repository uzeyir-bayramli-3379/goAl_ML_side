"""Collapse nearby landmark entities into visitable stops via one Gemini call.

Offline, single-shot. Reads the entity CSV (wikidata_id, name, primary_class,
sitelinks, meters_away), sends the whole set to gemini-2.5-flash under
PLANNING_SYSTEM_PROMPT, and writes the returned VISIT UNITS as JSON. Every
input id must land in exactly one unit and no id may be invented — this is
validated before writing, and the run fails loudly if it is violated.

Run (use the conda env python — base python lacks google-genai / pandas):
    python plan_units.py --dry-run          # print the prompt, no API call
    python plan_units.py --csv_name data_file.csv   # run on a different CSV
    python plan_units.py --limit 3          # only the first 3 entities
    python plan_units.py --thinnest 10      # 10 with the fewest sitelinks
    python plan_units.py --fattest 10       # 10 with the most sitelinks
    python plan_units.py --ids Q842822 Q338889   # only these wikidata_ids
    python plan_units.py                    # all 100 -> visit_units.json
"""

import argparse
import json
import os

import pandas as pd
from dotenv import load_dotenv
from google import genai
from google.genai import types

from planning_prompt_v1 import PLANNING_SYSTEM_PROMPT

INPUT_CSV = "rows_11_testing.csv"
DEFAULT_OUT = "visit_units.json"
ANCHOR = (49.845397, 40.374701)          # (lon, lat) — anchor the meters_away is from
MODEL = "gemini-2.5-flash"
MIN_ANCHOR_N = 5
MAX_OUTPUT_TOKENS = 16000            # cap guards a runaway; raise if a big input truncates.
# gemini-2.5-flash thinks by default, and thinking tokens are billed against
# max_output_tokens — unbounded thinking ate the budget and truncated the JSON
# mid-array. Cap thinking so most of the budget is left for the actual answer.
# 0 disables thinking entirely (cheapest, fully reproducible); a positive value
# keeps some reasoning for the quota-based ranking. -1 = unbounded (the old bug).
THINKING_BUDGET = 4096

INPUT_COLS = ["wikidata_id", "name", "primary_class", "sitelinks",
              "meters_away", "wiki_extract"]

EXTRACT_CHARS = 400


def select_rows(df, limit=None, thinnest=None, fattest=None, ids=None):
    """Subset the entities before grouping. Tiering is relative to THIS input
    (per the prompt), so a subset stays coherent. Modes are mutually exclusive.

    - ids:      exactly the given wikidata_ids, in the CSV's order.
    - thinnest: the N with the FEWEST sitelinks (least-notable end).
    - fattest:  the N with the MOST sitelinks (headline destinations).
    - limit:    the first N rows as they appear in the CSV."""
    if ids is not None:
        wanted = set(ids)
        found = set(df["wikidata_id"]) & wanted
        for missing in sorted(wanted - found):
            print(f"  warning: id not in CSV, skipped: {missing}")
        return df[df["wikidata_id"].isin(wanted)]
    if thinnest is not None:
        return df.sort_values("sitelinks", ascending=True).head(thinnest)
    if fattest is not None:
        return df.sort_values("sitelinks", ascending=False).head(fattest)
    if limit is not None:
        return df.head(limit)
    return df


def compute_quotas(n):
    """Scale the primary/secondary tier quotas to the input size instead of
    hardcoding them. Returns (primary, secondary), or None when n < 5 — such a
    cluster is not a full anchor day but a solo stop attached to another
    anchor's day, so it should be skipped rather than tiered."""
    if n < MIN_ANCHOR_N:
        return None
    primary = min(6, max(2, n // 8))
    secondary = min(12, max(3, n // 4))
    return primary, secondary


def build_user_message(df, primary, secondary):
    """The entities as a compact JSON records block plus the anchor and the
    per-run tier quotas. Facts go in the USER message, never the system prompt
    (which holds literal braces)."""
    records = df[INPUT_COLS].to_dict(orient="records")
    for r in records:
        e = r.get("wiki_extract")
        r["wiki_extract"] = (e[:EXTRACT_CHARS] if isinstance(e, str) else None)
    return (
        f"ANCHOR (lon, lat): {ANCHOR[0]}, {ANCHOR[1]}\n"
        f"QUOTAS: primary = {primary}, secondary = {secondary}\n"
        f"ENTITIES ({len(records)}):\n"
        f"{json.dumps(records, ensure_ascii=False, indent=0)}"
    )


def _strip_fences(text):
    t = text.strip()
    if t.startswith("```"):
        t = t[3:]
        if t[:4].lower() == "json":
            t = t[4:]
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _validate(units, input_ids):
    """Every input id in exactly one unit, no invented ids. Returns list of
    problem strings (empty == clean)."""
    problems = []
    seen = []
    for u in units:
        for qid in u.get("wikidata_ids", []):
            seen.append(qid)
    seen_set = set(seen)
    invented = seen_set - input_ids
    missing = input_ids - seen_set
    dupes = {q for q in seen if seen.count(q) > 1}
    if invented:
        problems.append(f"invented ids not in input: {sorted(invented)}")
    if missing:
        problems.append(f"input ids missing from output: {sorted(missing)}")
    if dupes:
        problems.append(f"ids assigned to more than one unit: {sorted(dupes)}")
    return problems

def _as_bool(s):
    """CSV booleans arrive as strings and bool('false') is True, so a naive
    astype(bool) passes every row and the blocklist silently stops working."""
    if s.dtype == bool:
        return s
    return (s.astype(str).str.strip().str.lower()
             .isin(["true", "t", "1", "yes"]))


def split_eligible(df):
    """Rows the blocklist already ruled out never reach the model: a metro
    station cannot be a stop, so asking the model to rank it is asking a
    question with one correct answer we already know. They come back as
    ambient afterwards, which keeps the output a complete partition of the
    input and keeps the id validator meaningful."""
    if "stop_eligible" not in df.columns:
        raise SystemExit("Input CSV missing stop_eligible — re-export with it.")
    flag = _as_bool(df["stop_eligible"])
    eligible = df[flag]
    blocked = df[~flag]
    if len(blocked):
        print(f"  blocklist: {len(blocked)} row(s) forced ambient, not sent to the model")
        for n in blocked["name"]:
            print(f"    - {n}")
    return eligible, blocked


def blocked_units(blocked):
    return [
        {"unit_name": r["name"], "wikidata_ids": [r["wikidata_id"]],
         "tier": "ambient", "duration_minutes": None}
        for _, r in blocked.iterrows()
    ]


SATURATION_THRESHOLD = 0.15   # share of an anchor's rows
MAX_PER_CATEGORY = 4          # across primary + secondary combined
MAX_LONG_UNITS = 1            # 180-minute units per anchor

# Wikidata's coarse top-level buckets. Frequent everywhere by construction —
# Baku Olympic Stadium and a caravanserai are both 'architectural structure' —
# so frequency here means "the taxonomy gave up", not "these are alike".
CATCH_ALL_CLASSES = {"building", "architectural structure", "monument", "structure"}


def saturating_classes(df):
    """Classes to cap, derived from THIS anchor's composition rather than a
    hardcoded list. Baku saturates on mosques, Rome on churches, Kyoto on
    temples — a whitelist tuned to one city silently fails in the others.
    A class qualifies when it is both frequent and specific."""
    counts = df["primary_class"].value_counts(normalize=True)
    out = {c for c, share in counts.items()
           if share >= SATURATION_THRESHOLD and c not in CATCH_ALL_CLASSES}
    if out:
        print(f"  saturating classes: "
              f"{', '.join(f'{c} ({counts[c]:.0%})' for c in sorted(out))}")
    return out


def enforce(units, df):
    """Rules the prompt states and the model does not reliably follow. Applied
    here rather than argued harder in the prompt: quotas it now respects,
    these it does not.

    Order matters — demotions push units into ambient, and ambient must end up
    with a null duration, so that rule runs last."""
    cls = dict(zip(df["wikidata_id"], df["primary_class"]))

    def unit_class(u):
        for qid in u.get("wikidata_ids", []):
            if qid in cls:
                return cls[qid]      # first id wins; merged units are one stop
        return None

    scheduled = [u for u in units if u["tier"] in ("primary", "secondary")]

    # Category cap, applied only to classes that saturate THIS anchor. The fifth
    # mosque in a radius is not worth the first, and Icherisheher returns 15
    # mosque units — without this the Old City reports a week of content. Demoted
    # slots are NOT backfilled: slightly under quota is fine, reordering the
    # ranking here is not.
    capped = saturating_classes(df)
    seen = {}
    for u in scheduled:
        c = unit_class(u)
        if c not in capped:
            continue
        seen[c] = seen.get(c, 0) + 1
        if seen[c] > MAX_PER_CATEGORY:
            print(f"  cap: {u['unit_name']} -> ambient ({c} #{seen[c]})")
            u["tier"] = "ambient"

    # One 180 per anchor. The model reaches for 180 whenever a unit feels
    # important; three of them turns a half-day district into two days.
    longs = [u for u in units
         if u.get("duration_minutes") == 180 and u["tier"] in ("primary", "secondary")]
    for u in longs[MAX_LONG_UNITS:]:
        print(f"  180-cap: {u['unit_name']} -> 90")
        u["duration_minutes"] = 90

    for u in units:
        if u["tier"] in ("ambient", "anchor_self"):
            u["duration_minutes"] = None

    return units

def run(df, out_path, dry_run=False):
    eligible, blocked = split_eligible(df)
    n = len(eligible)
    quotas = compute_quotas(n)
    if quotas is None:
        print(f"{n} eligible entit(y/ies) (< {MIN_ANCHOR_N}): not an anchor — "
              f"solo stop to attach to another anchor's day. Skipping.")
        return
    primary, secondary = quotas
    user_msg = build_user_message(eligible, primary, secondary)
    if dry_run:
        print("=" * 70)
        print("SYSTEM:\n" + PLANNING_SYSTEM_PROMPT)
        print("=" * 70)
        print("USER:\n" + user_msg)
        print("=" * 70)
        print(f"Dry run: {len(eligible)} entit(y/ies), quotas primary={primary} "
              f"secondary={secondary}, no API call made.")
        return

    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY not set in environment (.env).")
    client = genai.Client(api_key=api_key)

    config = types.GenerateContentConfig(
        system_instruction=PLANNING_SYSTEM_PROMPT,
        temperature=0,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        thinking_config=types.ThinkingConfig(thinking_budget=THINKING_BUDGET),
        response_mime_type="application/json",
    )
    # Stream so the tokens are watchable as they arrive; accumulate the chunks
    # into the full JSON text, then parse once at the end (streamed JSON is only
    # valid when complete).
    chunks = []
    finish_reason = None
    for chunk in client.models.generate_content_stream(
        model=MODEL, contents=user_msg, config=config
    ):
        if chunk.text:
            chunks.append(chunk.text)
            print(chunk.text, end="", flush=True)
        if chunk.candidates and chunk.candidates[0].finish_reason:
            finish_reason = chunk.candidates[0].finish_reason
    print()  # newline after the stream
    raw = "".join(chunks)

    if str(finish_reason).endswith("MAX_TOKENS"):
        raise SystemExit(
            f"Response truncated at MAX_TOKENS ({MAX_OUTPUT_TOKENS}). Thinking "
            f"(budget {THINKING_BUDGET}) plus output exceeded the cap — lower "
            f"THINKING_BUDGET or raise MAX_OUTPUT_TOKENS.")

    try:
        units = json.loads(_strip_fences(raw)) + blocked_units(blocked)
    except (ValueError, TypeError) as e:
        raise SystemExit(f"Unparseable model response: {e}\n---\n{raw[:2000]}")
    if not isinstance(units, list):
        raise SystemExit(f"Expected a JSON array, got {type(units).__name__}.")

    units = enforce(units, df)

    input_ids = set(df["wikidata_id"])
    problems = _validate(units, input_ids)
    if problems:
        print("VALIDATION FAILED:")
        for p in problems:
            print(f"  - {p}")
        print("Writing output anyway for inspection; treat as INCOMPLETE.")

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(units, fh, ensure_ascii=False, indent=2)

    tiers = {}
    for u in units:
        tiers[u.get("tier", "?")] = tiers.get(u.get("tier", "?"), 0) + 1
    print(f"Wrote {len(units)} unit(s) -> {out_path}")
    print(f"  tiers: {tiers}")
    print(f"  validation: {'clean' if not problems else 'PROBLEMS (see above)'}")


def main():
    ap = argparse.ArgumentParser(description="Group landmark entities into visit units.")
    ap.add_argument("--in", "--csv_name", dest="in_csv", default=INPUT_CSV,
                    help="input entity CSV (e.g. --csv_name data_file.csv)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output JSON path")
    ap.add_argument("--limit", type=int, help="only the first N entities")
    ap.add_argument("--thinnest", type=int,
                    help="the N entities with the FEWEST sitelinks")
    ap.add_argument("--fattest", type=int,
                    help="the N entities with the MOST sitelinks")
    ap.add_argument("--ids", nargs="+",
                    help="only these wikidata_ids (space-separated, e.g. Q842822 Q338889)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prompt, make no API call")
    args = ap.parse_args()

    modes = [args.limit, args.thinnest, args.fattest, args.ids]
    if sum(m is not None for m in modes) > 1:
        raise SystemExit("--limit, --thinnest, --fattest and --ids are mutually exclusive.")

    df = pd.read_csv(args.in_csv)
    missing = [c for c in INPUT_COLS if c not in df.columns]
    if missing:
        raise SystemExit(f"Input CSV missing columns: {missing}")
    df = select_rows(df, limit=args.limit, thinnest=args.thinnest,
                     fattest=args.fattest, ids=args.ids)
    if len(df) == 0:
        raise SystemExit("No entities selected.")
    run(df, out_path=args.out, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
