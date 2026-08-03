"""GoAI Baku — one-off top-up fetch for amenity categories added after the
main Places census ran.

WHY THIS EXISTS, AND WHY IT IS NOT AN EDIT TO get_amenities_places.py:
that module's plan_fingerprint() hashes NEARBY_QUERIES, so appending a single
category invalidates baku_places_checkpoint.jsonl and forces the whole 812-task
plan to be re-fetched and re-billed to gain a few dozen rows. The guard is
right to be conservative — it cannot tell "added a type" from "moved the grid"
— but for a pure addition the existing task ids keep their exact meaning, so
re-fetching them buys nothing.

This script therefore runs ONLY the new categories, on the coarse grid, with
its OWN checkpoint file. The main checkpoint is never opened or written.

Same discipline as the parent module:
  - Null, never guess: missing address -> None, never "" or a guess.
  - Flag, don't drop: only rows missing id/name/coords are dropped, counted
    with reasons. Non-OPERATIONAL places are excluded but COUNTED.
  - Failures fail loudly: an unexpected response shape raises, it is not an
    empty success. An invalid Places type will 400 here rather than silently
    returning nothing — that is intended.
  - No price data, no opening hours: field mask is inherited unchanged from the
    parent so the billing SKU stays Pro.

Output is a separate CSV with the same columns as baku_amenities_places.csv, so
it loads through the existing staging path and ON CONFLICT (place_id) DO
NOTHING dedupes it against what is already in amenities_places.

Run:
    python get_amenities_extra.py --dry-run
    python get_amenities_extra.py
"""

import argparse
import json
import os
import sys
import time
from datetime import date

import pandas as pd

from get_amenities_places import (
    CORE_BOX,
    GRIDS,
    MAX_RESULT_COUNT,
    NEARBY_URL,
    POLITE_DELAY,
    _api_key,
    _in_box,
    _post,
    make_grid,
)

# Places type -> our category vocabulary, for the types the census did not run.
# Keep these in sync with CATEGORY_MAP in get_amenities_places.py if the main
# plan is ever re-run from scratch; until then this file is the only source.
EXTRA_TYPES = {
    "shopping_mall":      "shopping_mall",
    "tourist_attraction": "tourist_attraction",
}

# Most specific first. tourist_attraction is a catch-all Google applies broadly
# (a mall or a market will often carry it too), so it loses every tie.
EXTRA_PRIORITY = ["shopping_mall", "tourist_attraction"]

# All three are sparse: a 2.4km tile will not hit the 20-result cap. If one does,
# the saturation report at the end will say so and that tile needs splitting.
TIER = "coarse"

CHECKPOINT_FILE = "baku_places_extra_checkpoint.jsonl"
OUTPUT_CSV = "baku_amenities_extra.csv"


def build_tasks():
    cfg = GRIDS[TIER]
    centers = make_grid(CORE_BOX, cfg["spacing_m"])
    types = list(EXTRA_TYPES)
    return [
        {"id": f"extra|{TIER}|{'+'.join(types)}|{i}",
         "types": types, "lat": lat, "lng": lng,
         "radius": cfg["radius_m"]}
        for i, (lat, lng) in enumerate(centers)
    ]


def fetch(task, key):
    body = {
        "includedTypes": task["types"],
        "maxResultCount": MAX_RESULT_COUNT,
        "locationRestriction": {"circle": {
            "center": {"latitude": task["lat"], "longitude": task["lng"]},
            "radius": float(task["radius"]),
        }},
    }
    return _post(NEARBY_URL, body, key)


def load_checkpoint():
    """Append-only JSONL, one record per completed task. No fingerprint: this
    plan is a fixed one-off, so there is nothing for a fingerprint to protect
    against. Delete the file to start over."""
    done = {}
    if not os.path.exists(CHECKPOINT_FILE):
        return done
    with open(CHECKPOINT_FILE, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                print(f"  checkpoint line {lineno} truncated — that task will "
                      f"be re-fetched.")
                continue
            done[rec["id"]] = rec["places"]
    return done


def save_task(task_id, places):
    with open(CHECKPOINT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": task_id, "places": places},
                           ensure_ascii=False) + "\n")


def categorize(place):
    types = place.get("types") or []
    for t in EXTRA_PRIORITY:
        if t in types:
            return EXTRA_TYPES[t]
    # Reached only if Places returned a place carrying none of the types we
    # asked for. Loud, not silent: we do not want to guess a category.
    return None


def parse(done, tasks_by_id):
    rows = {}
    dropped = {"no_id": 0, "no_name": 0, "no_coords": 0, "no_category": 0}
    closed = 0
    out_of_box = 0
    saturated = []
    today = date.today().isoformat()

    for task_id, places in done.items():
        if len(places) >= MAX_RESULT_COUNT:
            saturated.append(task_id)
        for p in places:
            pid = p.get("id")
            if not pid:
                dropped["no_id"] += 1
                continue
            name = (p.get("displayName") or {}).get("text")
            if not name:
                dropped["no_name"] += 1
                continue
            loc = p.get("location") or {}
            lat, lng = loc.get("latitude"), loc.get("longitude")
            if lat is None or lng is None:
                dropped["no_coords"] += 1
                continue
            if not _in_box(lat, lng):
                out_of_box += 1
                continue
            status = p.get("businessStatus")
            if status and status != "OPERATIONAL":
                closed += 1
                continue
            category = categorize(p)
            if category is None:
                dropped["no_category"] += 1
                continue
            rows[pid] = {
                "place_id": pid,
                "name": name,
                "category": category,
                "lat": lat,
                "lng": lng,
                "opening_hours": None,
                "address": p.get("formattedAddress"),
                "business_status": status,
                "fetched_at": today,
                "coverage": f"extra:{TIER}",
            }

    df = pd.DataFrame(list(rows.values()))
    print(f"\nkept {len(df)} unique places")
    print(f"  excluded non-OPERATIONAL: {closed}")
    print(f"  outside CORE_BOX:         {out_of_box}")
    print(f"  dropped: {dropped}")
    if not df.empty:
        print("\nby category:")
        print(df["category"].value_counts().to_string())
    return df, saturated


def run(dry_run=False):
    tasks = build_tasks()
    tasks_by_id = {t["id"]: t for t in tasks}
    done = load_checkpoint()
    pending = [t for t in tasks if t["id"] not in done]

    print(f"plan: {len(tasks)} tasks on the {TIER} grid "
          f"({GRIDS[TIER]['spacing_m']}m spacing, {GRIDS[TIER]['radius_m']}m radius)")
    print(f"types: {', '.join(EXTRA_TYPES)}")
    print(f"already checkpointed: {len(done)}   to fetch now: {len(pending)}")
    if dry_run:
        return

    if pending:
        key = _api_key()
        for n, task in enumerate(pending, 1):
            try:
                places = fetch(task, key)
            except RuntimeError as e:
                print(f"\nFATAL on {task['id']}: {e}")
                print(f"Progress checkpointed to {CHECKPOINT_FILE}; re-run to resume.")
                sys.exit(1)
            done[task["id"]] = places
            save_task(task["id"], places)
            if n % 10 == 0 or n == len(pending):
                print(f"  {n}/{len(pending)} tasks done")
            time.sleep(POLITE_DELAY)

    df, saturated = parse(done, tasks_by_id)
    df.to_csv(OUTPUT_CSV, index=False)
    print(f"\nWrote {len(df)} rows -> {OUTPUT_CSV}")

    if saturated:
        print(f"\n{len(saturated)} tile(s) hit the {MAX_RESULT_COUNT}-result cap "
              f"and are TRUNCATED — {OUTPUT_CSV} is incomplete for those areas:")
        for t in saturated:
            print(f"  {t} @ {tasks_by_id[t]['lat']:.4f},{tasks_by_id[t]['lng']:.4f}")
        print("Re-run these on the fine grid, or split them, before trusting "
              "the counts.")
    else:
        print("No tile saturated — this is a full census of the box for these "
              "types.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="One-off Places fetch for amenity categories added after the main census.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the task count and stop")
    run(dry_run=ap.parse_args().dry_run)
