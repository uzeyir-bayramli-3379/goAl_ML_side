"""GoAI Baku amenity discovery — Google Places API (New) replacement source.

Replaces the Overpass/OSM sourcing in get_amenities.py: ground-truthing showed
the OSM data for Baku is badly stale (~25% of amenities closed, up to half
nonexistent). Landmarks (Wikidata pipeline) are untouched.

Uses the v1 endpoints (places.googleapis.com/v1/places:searchNearby and
:searchText), NOT the legacy Places API. Key: GOOGLE_PLACES_API_KEY in .env.

Coverage strategy: Nearby Search caps at 20 results per request with no
pagination in the new API, so one big-radius query silently truncates. We tile
the same CORE_BOX the Overpass query used into overlapping search circles
(radius >= spacing/sqrt(2) so square cells are fully covered) and query per
(circle x type-group), deduping across overlaps by place id. Dense categories
(restaurant/cafe/...) get a fine grid; sparse ones (cinema/casino/...) a
coarse grid where the 20-cap doesn't bind.

Discipline carried over from the OSM pipeline:
  - Null, never guess: missing hours/address -> None, never "" or a guess.
  - Flag, don't drop: only rows missing id/name/coords are dropped, counted
    with reasons. Non-OPERATIONAL places are excluded but COUNTED — the
    closed counts are our staleness metric, we want to see them.
  - Failures fail loudly: a response missing the expected shape is a failure,
    not an empty success. Progress checkpoints per tile-task to JSON so a
    failed run resumes instead of restarting (same pattern as the Wikidata
    enrichment checkpointing).
  - No price data: not requested in the field mask, not stored. Project
    decision: never claim affordability.

COMPLIANCE (revisit before launch, do not silently ignore): Google Places ToS
restricts long-term caching of most Places content; place IDs are explicitly
storable indefinitely. We store a minimal field set for an internal prototype
— this needs a proper ToS review (refresh policy / attribution) before any
public launch.

Run:
    python get_amenities_places.py --dry-run    # print the request plan only
    python get_amenities_places.py --sample     # fetch ONE tile, print parsed rows
    python get_amenities_places.py              # full run (asks confirmation >500 reqs)
    python get_amenities_places.py --yes        # full run, skip confirmation
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import date

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
TEXT_URL = "https://places.googleapis.com/v1/places:searchText"

# Minimal field mask — field masks determine the billing SKU. Do NOT add
# atmosphere/contact fields we don't use.
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.location",
    "places.types",
    "places.businessStatus",
    "places.regularOpeningHours",
    "places.formattedAddress",
])

# Same core box as the Overpass query in get_amenities.py, so "the core"
# means the same thing app-wide.
CORE_BOX = {"lat": (40.34, 40.44), "lon": (49.79, 49.90)}

REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
BACKOFF_BASE = 2          # seconds; wait grows BACKOFF_BASE * attempt
POLITE_DELAY = 0.1        # seconds between requests
CONFIRM_THRESHOLD = 500   # ask before running more requests than this

CHECKPOINT_FILE = "baku_places_checkpoint.json"
OUTPUT_CSV = "baku_amenities_places.csv"

# --- Category taxonomy ------------------------------------------------------
# Places type -> our internal category vocabulary (kept stable with the DB).
# pastry_shop is folded into confectionery for this source: Places has one
# "bakery" type covering both OSM shop=pastry and shop=confectionery.
CATEGORY_MAP = {
    "restaurant":            "restaurant",
    "cafe":                  "cafe",
    "fast_food_restaurant":  "fast_food",
    "bar":                   "bar",
    "pub":                   "pub",
    "night_club":            "nightclub",
    "casino":                "casino",
    "movie_theater":         "cinema",
    "amusement_park":        "theme_park",
    "amusement_center":      "arcade",
    "bowling_alley":         "bowling",
    "water_park":            "water_park",
    "food_court":            "food_court",
    "bakery":                "confectionery",
    "gym":                   "gym",
    "sports_complex":        "sports_centre",
    "park":                  "park",
}

# When a place carries several mapped types (a pub is often also typed
# restaurant), the most specific wins — restaurant is the catch-all, so last.
TYPE_PRIORITY = [
    "night_club", "casino", "movie_theater", "amusement_park", "water_park",
    "bowling_alley", "amusement_center", "food_court", "bakery", "cafe",
    "fast_food_restaurant", "pub", "bar", "gym", "sports_complex", "park",
    "restaurant",
]

# Nearby query plan: (grid_tier, [includedTypes]). Dense types run alone or in
# small groups on the fine grid (the 20-result cap bites there); sparse types
# share requests on the coarse grid where a whole tile rarely has 20 venues.
NEARBY_QUERIES = [
    ("fine",   ["restaurant"]),
    ("fine",   ["cafe"]),
    ("fine",   ["fast_food_restaurant", "bakery"]),
    ("fine",   ["bar", "pub"]),
    ("coarse", ["gym", "sports_complex"]),
    ("coarse", ["park"]),
    ("coarse", ["night_club", "casino", "movie_theater"]),
    ("coarse", ["amusement_park", "bowling_alley", "water_park"]),
    ("coarse", ["food_court", "amusement_center"]),
]

# No clean Places type exists for these -> searchText scoped to the tile.
# (query, our category)
TEXT_QUERIES = [
    ("coarse", "hookah lounge", "hookah"),
    ("coarse", "dance hall", "dance_venue"),
]

# radius >= spacing / sqrt(2) guarantees full coverage of the square cells.
GRIDS = {
    "fine":   {"spacing_m": 800,  "radius_m": 600},
    "coarse": {"spacing_m": 2400, "radius_m": 1800},
}


def make_grid(box, spacing_m):
    """Return a list of (lat, lng) circle centers tiling the box."""
    south, north = box["lat"]
    west, east = box["lon"]
    lat_mid = (south + north) / 2
    dlat = spacing_m / 111320.0
    dlon = spacing_m / (111320.0 * math.cos(math.radians(lat_mid)))

    centers = []
    lat = south + dlat / 2
    while lat < north + dlat / 2:
        lng = west + dlon / 2
        while lng < east + dlon / 2:
            centers.append((round(lat, 6), round(lng, 6)))
            lng += dlon
        lat += dlat
    return centers


def build_plan():
    """Expand the query plan into concrete (task_id, kind, payload-parts)."""
    grids = {tier: make_grid(CORE_BOX, cfg["spacing_m"]) for tier, cfg in GRIDS.items()}
    tasks = []
    for tier, types in NEARBY_QUERIES:
        radius = GRIDS[tier]["radius_m"]
        for i, (lat, lng) in enumerate(grids[tier]):
            task_id = f"nearby|{tier}|{'+'.join(types)}|{i}"
            tasks.append({"id": task_id, "kind": "nearby", "types": types,
                          "lat": lat, "lng": lng, "radius": radius})
    for tier, query, category in TEXT_QUERIES:
        radius = GRIDS[tier]["radius_m"]
        for i, (lat, lng) in enumerate(grids[tier]):
            task_id = f"text|{tier}|{query}|{i}"
            tasks.append({"id": task_id, "kind": "text", "query": query,
                          "category": category, "lat": lat, "lng": lng,
                          "radius": radius})
    return tasks, grids


# --- HTTP layer -------------------------------------------------------------

def _api_key():
    key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not key:
        print("GOOGLE_PLACES_API_KEY missing from .env — aborting.")
        sys.exit(1)
    return key


def _post(url, body, key):
    """POST with retry/backoff. Returns the parsed `places` list, or raises
    RuntimeError after exhausting retries. A 200 without a dict body is a
    failure, not success; a 200 with no `places` key is a legitimate empty
    result (the new API returns {} for zero hits)."""
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(url, json=body, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            print(f"  network error (attempt {attempt}/{MAX_RETRIES}): {e}")
            time.sleep(BACKOFF_BASE * attempt)
            continue

        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After") or BACKOFF_BASE * attempt)
            print(f"  429 rate-limited; waiting {wait}s (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            wait = BACKOFF_BASE * attempt
            print(f"  {r.status_code} server error; backing off {wait}s (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(wait)
            continue
        if not r.ok:
            # 400/403 etc: retrying won't help — surface the body (never the key).
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")

        try:
            payload = r.json()
        except ValueError:
            print(f"  non-JSON 200 body (attempt {attempt}/{MAX_RETRIES}): {r.text[:300]}")
            time.sleep(BACKOFF_BASE * attempt)
            continue
        if not isinstance(payload, dict):
            print(f"  unexpected response shape (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(BACKOFF_BASE * attempt)
            continue

        return payload.get("places", [])

    raise RuntimeError(f"exhausted {MAX_RETRIES} retries for {url}")


def fetch_task(task, key):
    if task["kind"] == "nearby":
        body = {
            "includedTypes": task["types"],
            "maxResultCount": 20,
            "locationRestriction": {"circle": {
                "center": {"latitude": task["lat"], "longitude": task["lng"]},
                "radius": float(task["radius"]),
            }},
        }
        return _post(NEARBY_URL, body, key)
    # searchText only supports circles as a *bias*, not a restriction, so
    # out-of-box hits are possible — filtered against CORE_BOX in parsing.
    body = {
        "textQuery": task["query"],
        "pageSize": 20,
        "locationBias": {"circle": {
            "center": {"latitude": task["lat"], "longitude": task["lng"]},
            "radius": float(task["radius"]),
        }},
    }
    return _post(TEXT_URL, body, key)


# --- Checkpointing ----------------------------------------------------------

def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"tasks": {}}


def save_checkpoint(ckpt):
    tmp = CHECKPOINT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ckpt, f, ensure_ascii=False)
    os.replace(tmp, CHECKPOINT_FILE)


# --- Parsing / filtering ----------------------------------------------------

def _in_box(lat, lng, box=CORE_BOX):
    return box["lat"][0] <= lat <= box["lat"][1] and box["lon"][0] <= lng <= box["lon"][1]


def _categorize(place, fallback):
    for t in TYPE_PRIORITY:
        if t in (place.get("types") or []):
            return CATEGORY_MAP[t]
    return fallback


def parse_places(ckpt, tasks_by_id, verbose=True):
    """Dedupe by place id across overlapping circles, filter to OPERATIONAL,
    drop only rows missing id/name/coords (counted), keep NULLs elsewhere."""
    seen = {}
    status_counts = {}
    drop_counts = {}
    out_of_box = 0
    today = date.today().isoformat()

    for task_id, places in ckpt["tasks"].items():
        task = tasks_by_id.get(task_id, {})
        is_text = task.get("kind") == "text"
        fallback = (task.get("category") if is_text
                    else CATEGORY_MAP[task.get("types", ["restaurant"])[0]])

        for p in places:
            pid = p.get("id")
            name = (p.get("displayName") or {}).get("text")
            loc = p.get("location") or {}
            lat, lng = loc.get("latitude"), loc.get("longitude")

            if not pid:
                drop_counts["no_id"] = drop_counts.get("no_id", 0) + 1
                continue
            if pid in seen:
                continue
            if not name:
                drop_counts["no_name"] = drop_counts.get("no_name", 0) + 1
                continue
            if lat is None or lng is None:
                drop_counts["no_coords"] = drop_counts.get("no_coords", 0) + 1
                continue
            if is_text and not _in_box(lat, lng):
                out_of_box += 1  # locationBias can wander; keep the box honest
                continue

            status = p.get("businessStatus")  # may be absent -> NULL, counted
            status_counts[status or "MISSING"] = status_counts.get(status or "MISSING", 0) + 1
            if status in ("CLOSED_TEMPORARILY", "CLOSED_PERMANENTLY"):
                continue  # excluded but counted — this is the staleness metric

            hours = p.get("regularOpeningHours")
            seen[pid] = {
                "place_id": pid,
                "name": name,
                "category": _categorize(p, fallback),
                "lat": lat,
                "lng": lng,
                "opening_hours": json.dumps(hours, ensure_ascii=False) if hours else None,
                "address": p.get("formattedAddress"),  # None if absent — no fabrication
                "business_status": status,
                "fetched_at": today,
            }

    df = pd.DataFrame(seen.values()).sort_values(["category", "name"]) if seen else pd.DataFrame()

    if verbose:
        print(f"\nUnique places kept: {len(seen)}")
        print(f"businessStatus counts (staleness metric): {status_counts}")
        if drop_counts:
            print(f"Dropped (missing essentials): {drop_counts}")
        if out_of_box:
            print(f"Text-search hits outside CORE_BOX discarded: {out_of_box}")
        if len(df):
            print(f"\nBy category:\n{df['category'].value_counts().to_string()}")
    return df


# --- Entry points -----------------------------------------------------------

def print_plan(tasks, grids):
    fine_n, coarse_n = len(grids["fine"]), len(grids["coarse"])
    nearby_n = sum(1 for t in tasks if t["kind"] == "nearby")
    text_n = sum(1 for t in tasks if t["kind"] == "text")
    print(f"Grid tiles: fine={fine_n} (spacing {GRIDS['fine']['spacing_m']}m, "
          f"r={GRIDS['fine']['radius_m']}m), coarse={coarse_n} "
          f"(spacing {GRIDS['coarse']['spacing_m']}m, r={GRIDS['coarse']['radius_m']}m)")
    print("Nearby query groups:")
    for tier, types in NEARBY_QUERIES:
        n = len(grids[tier])
        print(f"  [{tier:6s}] {'+'.join(types):45s} -> {n} requests")
    print("Text queries:")
    for tier, query, category in TEXT_QUERIES:
        n = len(grids[tier])
        print(f"  [{tier:6s}] {query!r} -> {category:12s} -> {n} requests")
    print(f"\nTOTAL requests: {len(tasks)}  (nearby={nearby_n}, text={text_n})")
    return len(tasks)


def run(dry_run=False, sample=False, assume_yes=False):
    tasks, grids = build_plan()
    total = print_plan(tasks, grids)

    if dry_run:
        print("\n--dry-run: no requests made.")
        return

    key = _api_key()

    if sample:
        task = tasks[0]
        print(f"\n--sample: fetching one tile only -> {task['id']}")
        places = fetch_task(task, key)
        ckpt = {"tasks": {task["id"]: places}}
        df = parse_places(ckpt, {t["id"]: t for t in tasks})
        if len(df):
            print(f"\nSample parsed rows:\n{df.head(20).to_string(index=False)}")
        else:
            print("Sample tile returned no places.")
        return

    ckpt = load_checkpoint()
    done = set(ckpt["tasks"])
    remaining = [t for t in tasks if t["id"] not in done]
    if done:
        print(f"\nResuming: {len(done)} tasks already checkpointed, {len(remaining)} remaining.")

    if len(remaining) > CONFIRM_THRESHOLD and not assume_yes:
        answer = input(f"\n{len(remaining)} requests exceed the {CONFIRM_THRESHOLD} "
                       f"threshold. Proceed? [y/N] ")
        if answer.strip().lower() != "y":
            print("Aborted.")
            return

    for n, task in enumerate(remaining, 1):
        try:
            places = fetch_task(task, key)
        except RuntimeError as e:
            print(f"\nFATAL on {task['id']}: {e}")
            print(f"Progress checkpointed to {CHECKPOINT_FILE}; re-run to resume.")
            sys.exit(1)
        ckpt["tasks"][task["id"]] = places
        save_checkpoint(ckpt)
        if n % 25 == 0 or n == len(remaining):
            print(f"  {n}/{len(remaining)} tasks done ({len(done) + n} total)")
        time.sleep(POLITE_DELAY)

    df = parse_places(ckpt, {t["id"]: t for t in tasks})
    df.to_csv(OUTPUT_CSV, index=False)
    print(f"\nWrote {len(df)} rows -> {OUTPUT_CSV}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Discover Baku amenities via Google Places API (New).")
    ap.add_argument("--dry-run", action="store_true", help="print the request plan and stop")
    ap.add_argument("--sample", action="store_true", help="fetch one tile, print parsed rows, stop")
    ap.add_argument("--yes", action="store_true", help="skip the request-count confirmation")
    args = ap.parse_args()
    run(dry_run=args.dry_run, sample=args.sample, assume_yes=args.yes)
