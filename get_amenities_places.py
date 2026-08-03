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
coarse grid where the 20-cap doesn't bind. Any tile that STILL saturates is
refined reactively — split into a finer sub-grid and re-queried — until nothing
truncates (see MAX_REFINE_DEPTH / make_children), so dense pockets like the Old
City self-correct instead of being silently under-collected.

Discipline carried over from the OSM pipeline:
  - Null, never guess: missing address -> None, never "" or a guess. Opening
    hours are not fetched at all (Enterprise SKU) so opening_hours is always
    None — the serving layer must never claim a place is open.
  - Flag, don't drop: only rows missing id/name/coords are dropped, counted
    with reasons. Non-OPERATIONAL places are excluded but COUNTED — the
    closed counts are our staleness metric, we want to see them.
  - Failures fail loudly: a response missing the expected shape is a failure,
    not an empty success. Progress checkpoints per tile-task to append-only
    JSONL (one line per task) so a failed run resumes instead of restarting —
    the same pattern as the Wikidata enrichment checkpoint, and for the same
    reason: appending has no rewrite/lock window for a sync client to corrupt.
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
import hashlib
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

# The field mask determines the billing SKU, and a request bills at the HIGHEST
# SKU any requested field belongs to. Every field below is Nearby Search Pro
# (5,000 free calls/month). Adding one Enterprise field — regularOpeningHours is
# the trap, it was in here originally — drops the whole run to the Enterprise
# allowance (1,000 free calls/month), an 80% cut. Before adding ANY field, check
# it against Google's Enterprise/Enterprise+Atmosphere trigger list first.
# businessStatus stays: it is Pro-tier and it is our staleness metric.
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.location",
    "places.types",
    "places.businessStatus",
    "places.formattedAddress",
])

# Same core box as the Overpass query in get_amenities.py, so "the core"
# means the same thing app-wide.
CORE_BOX = {"lat": (40.34, 40.44), "lon": (49.79, 49.90)}

# Hard API cap: Nearby/Text Search return at most 20 places and the new API has
# no pagination. A task that comes back with exactly this many was TRUNCATED —
# see the saturation check in parse_places.
MAX_RESULT_COUNT = 20

# Adaptive refinement: rather than guess a grid fine enough for the densest
# spots up front, we refine reactively — any tile that saturates is split into a
# REFINE_SUBGRID x REFINE_SUBGRID grid at REFINE_FACTOR of its spacing/radius and
# re-queried for the SAME type-group, repeating until nothing saturates or
# MAX_REFINE_DEPTH is hit. Halving keeps radius >= spacing/sqrt(2) at every level
# (600>=566, 300>=283, 150>=141, ...). Density is finite, so it converges.
REFINE_SUBGRID = 2
REFINE_FACTOR = 0.5
MAX_REFINE_DEPTH = 3   # depth 0=800m, 1=400m, 2=200m, 3=100m spacing; stop here

REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
BACKOFF_BASE = 2          # seconds; wait grows BACKOFF_BASE * attempt
POLITE_DELAY = 0.1        # seconds between requests
CONFIRM_THRESHOLD = 500   # ask before running more requests than this

CHECKPOINT_FILE = "baku_places_checkpoint.jsonl"
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
    "shopping_mall": "shopping_mall",
}

# When a place carries several mapped types (a pub is often also typed
# restaurant), the most specific wins — restaurant is the catch-all, so last.
TYPE_PRIORITY = [
    "night_club", "casino", "movie_theater", "amusement_park", "water_park",
    "bowling_alley", "amusement_center","shopping_mall", "food_court", "bakery", "cafe",
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
    ("coarse", ["shopping_mall"]),   
    ("coarse", ["market", "supermarket"]),   # Green Market, Yaşıl Bazar
    ("coarse", ["tourist_attraction"]),
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
        spacing = GRIDS[tier]["spacing_m"]
        for i, (lat, lng) in enumerate(grids[tier]):
            task_id = f"nearby|{tier}|{'+'.join(types)}|{i}"
            tasks.append({"id": task_id, "kind": "nearby", "types": types,
                          "lat": lat, "lng": lng, "radius": radius,
                          "spacing": spacing, "depth": 0})
    for tier, query, category in TEXT_QUERIES:
        radius = GRIDS[tier]["radius_m"]
        spacing = GRIDS[tier]["spacing_m"]
        for i, (lat, lng) in enumerate(grids[tier]):
            task_id = f"text|{tier}|{query}|{i}"
            tasks.append({"id": task_id, "kind": "text", "query": query,
                          "category": category, "lat": lat, "lng": lng,
                          "radius": radius, "spacing": spacing, "depth": 0})
    return tasks, grids


def make_children(task):
    """Split a saturated tile into a REFINE_SUBGRID x REFINE_SUBGRID grid at
    REFINE_FACTOR of its spacing/radius, tiling the parent's cell. Children
    inherit kind/types/query/category, so categorization is unchanged; only the
    id, position, spacing, radius and depth differ. Deterministic: the same
    parent always yields the same child ids, which is what makes resume safe."""
    lat, lng = task["lat"], task["lng"]
    child_spacing = task["spacing"] * REFINE_FACTOR
    child_radius = task["radius"] * REFINE_FACTOR
    # Child centers tile the parent cell: for a 2x2 split they sit at
    # +/- (parent_spacing / 4) from the parent center on each axis.
    span = task["spacing"] * (REFINE_SUBGRID - 1) / (2.0 * REFINE_SUBGRID)
    dlat = span / 111320.0
    dlon = span / (111320.0 * math.cos(math.radians(lat)))

    lat0, lng0 = lat - dlat, lng - dlon
    step_lat = (2 * dlat) / (REFINE_SUBGRID - 1) if REFINE_SUBGRID > 1 else 0
    step_lon = (2 * dlon) / (REFINE_SUBGRID - 1) if REFINE_SUBGRID > 1 else 0

    children = []
    k = 0
    for r in range(REFINE_SUBGRID):
        for c in range(REFINE_SUBGRID):
            child = dict(task)
            child.update({
                "id": f"{task['id']}~{k}",
                "lat": round(lat0 + r * step_lat, 6),
                "lng": round(lng0 + c * step_lon, 6),
                "spacing": child_spacing,
                "radius": child_radius,
                "depth": task["depth"] + 1,
            })
            children.append(child)
            k += 1
    return children


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
            "maxResultCount": MAX_RESULT_COUNT,
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
        "pageSize": MAX_RESULT_COUNT,
        "locationBias": {"circle": {
            "center": {"latitude": task["lat"], "longitude": task["lng"]},
            "radius": float(task["radius"]),
        }},
    }
    return _post(TEXT_URL, body, key)


# --- Checkpointing ----------------------------------------------------------

def plan_fingerprint():
    """Hash of everything that changes what a cached response MEANS: the field
    mask (which fields the response contains) and the query/grid plan (which
    task_id maps to which category)."""
    blob = json.dumps({
        "field_mask": FIELD_MASK,
        "grids": GRIDS,
        "nearby": NEARBY_QUERIES,
        "text": TEXT_QUERIES,
        "refine": [REFINE_SUBGRID, REFINE_FACTOR, MAX_REFINE_DEPTH],
    }, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load_checkpoint():
    """Append-only JSONL, matching the landmark enrichment checkpoint. Line 1 is
    the plan fingerprint; every later line is either a task record
    {"id","places"} or a refinement marker {"refined"}. Appending never rewrites
    the file, so there is NO os.replace lock window for a sync client (Dropbox/
    OneDrive) to collide with, and each write is constant-time no matter how big
    the checkpoint has grown. (The old whole-file-JSON version rewrote the entire
    accumulated file on every one of 800+ tasks — quadratic disk churn, and every
    replace was a chance for a mid-scan sync client to corrupt it.)

    Refuse to resume a checkpoint built under a different plan: stale cached
    responses carry the wrong field set, and stale task_ids no longer in the plan
    would silently fall through parse_places' fallback to 'restaurant'."""
    if not os.path.exists(CHECKPOINT_FILE):
        return {"fingerprint": plan_fingerprint(), "tasks": {}, "refined": []}

    stored = None
    tasks = {}
    refined = []
    with open(CHECKPOINT_FILE, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                # A torn final line from an interrupted append: tolerate it — the
                # task just gets re-fetched. This is the whole point of append-
                # only; a partial write costs one record, never the file.
                print(f"  checkpoint line {lineno} is truncated — skipping "
                      f"(that task will be re-fetched).")
                continue
            if "fingerprint" in rec:
                stored = rec["fingerprint"]
            elif "id" in rec:
                tasks[rec["id"]] = rec["places"]   # last write wins on dup ids
            elif "refined" in rec:
                refined.append(rec["refined"])

    current = plan_fingerprint()
    if stored != current:
        print(f"\nCheckpoint {CHECKPOINT_FILE} was written under a different "
              f"query plan (fingerprint {stored or 'ABSENT (pre-fingerprint file)'} "
              f"!= {current}).")
        print("FIELD_MASK, GRIDS, NEARBY_QUERIES, TEXT_QUERIES or the refine "
              "policy changed since it was written, so its cached responses have "
              "the wrong fields and/or its task ids no longer match the plan. "
              "Resuming would write silently wrong categories.")
        print(f"Delete {CHECKPOINT_FILE} and re-run to start fresh. "
              "Not deleting it automatically — that is your call.")
        sys.exit(1)

    return {"fingerprint": current, "tasks": tasks, "refined": sorted(set(refined))}


def _append_checkpoint(record):
    with open(CHECKPOINT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()


def init_checkpoint():
    """Write the fingerprint header line once, when starting a fresh file."""
    if not os.path.exists(CHECKPOINT_FILE):
        _append_checkpoint({"fingerprint": plan_fingerprint()})


def save_task(task_id, places):
    _append_checkpoint({"id": task_id, "places": places})


def save_refined(task_id):
    _append_checkpoint({"refined": task_id})


# --- Parsing / filtering ----------------------------------------------------

def _in_box(lat, lng, box=CORE_BOX):
    return box["lat"][0] <= lat <= box["lat"][1] and box["lon"][0] <= lng <= box["lon"][1]


def _categorize(place, fallback):
    for t in TYPE_PRIORITY:
        if t in (place.get("types") or []):
            return CATEGORY_MAP[t]
    return fallback


def parse_places(ckpt, tasks_by_id, verbose=True, refined=None):
    """Dedupe by place id across overlapping circles, filter to OPERATIONAL,
    drop only rows missing id/name/coords (counted), keep NULLs elsewhere.

    Returns (df, saturated_ids) where saturated_ids are RAW-truncated tiles.
    A saturated tile that has since been refined (its id in `refined`) is no
    longer a coverage gap — its sub-tiles cover the same area — so the INCOMPLETE
    verdict is based on saturated MINUS refined. Only tiles that saturate and
    cannot be refined (depth floor) are reported as truly incomplete."""
    seen = {}
    status_counts = {}
    drop_counts = {}
    out_of_box = 0
    saturated_ids = []
    today = date.today().isoformat()

    for task_id, places in ckpt["tasks"].items():
        task = tasks_by_id.get(task_id, {})
        is_text = task.get("kind") == "text"
        fallback = (task.get("category") if is_text
                    else CATEGORY_MAP[task.get("types", ["restaurant"])[0]])

        # Saturation is measured on the RAW response, before dedup/filtering:
        # a full page means the API truncated, regardless of how many rows
        # survive downstream.
        if len(places) >= MAX_RESULT_COUNT:
            saturated_ids.append(task_id)

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

            seen[pid] = {
                "place_id": pid,
                "name": name,
                "category": _categorize(p, fallback),
                "lat": lat,
                "lng": lng,
                # Always None: regularOpeningHours is an Enterprise-SKU field and
                # is no longer requested (see FIELD_MASK). Column kept so the CSV
                # and DB schema stay stable if we ever pay for it. NULL = unknown,
                # which is what the serving layer already assumes.
                "opening_hours": None,
                "address": p.get("formattedAddress"),  # None if absent — no fabrication
                "business_status": status,
                "fetched_at": today,
            }

    df = pd.DataFrame(seen.values()).sort_values(["category", "name"]) if seen else pd.DataFrame()

    if verbose:
        refined_set = set(refined or ())
        unresolved = [s for s in saturated_ids if s not in refined_set]
        if unresolved:
            groups = {}
            for tid in unresolved:
                parts = tid.split("|")
                groups["|".join(parts[1:3])] = groups.get("|".join(parts[1:3]), 0) + 1
            breakdown = ", ".join(f"{g}: {n}" for g, n in
                                  sorted(groups.items(), key=lambda kv: -kv[1]))
            print(f"\nWARNING: {len(unresolved)} tiles hit the "
                  f"{MAX_RESULT_COUNT}-result cap ({breakdown}) — results "
                  f"truncated, these tiles need a finer grid.")
            print(f"Saturated task ids: {', '.join(sorted(unresolved))}")
            print("RUN IS INCOMPLETE — places were missed. The counts below are "
                  "a LOWER BOUND, not a coverage report.")
        elif saturated_ids and refined_set:
            print(f"\nAll {len(saturated_ids)} tiles that hit the cap were split "
                  f"by adaptive refinement — coverage converged, no truncation remains.")

        print(f"\nUnique places kept: {len(seen)}")
        print(f"businessStatus counts (staleness metric): {status_counts}")
        if drop_counts:
            print(f"Dropped (missing essentials): {drop_counts}")
        if out_of_box:
            print(f"Text-search hits outside CORE_BOX discarded: {out_of_box}")
        if len(df):
            print(f"\nBy category:\n{df['category'].value_counts().to_string()}")
    return df, saturated_ids


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


def run(dry_run=False, sample=False, sample_tile=None, assume_yes=False):
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
        ckpt = {"fingerprint": plan_fingerprint(), "tasks": {task["id"]: places}}
        df, _ = parse_places(ckpt, {t["id"]: t for t in tasks})
        if len(df):
            print(f"\nSample parsed rows:\n{df.head(20).to_string(index=False)}")
        else:
            print("Sample tile returned no places.")
        return

    if sample_tile is not None:
        # Probe ONE fine-grid restaurant tile to see if the 20-result cap binds
        # there (dense areas like Icherisheher are where the grid may be too
        # coarse). One real request. restaurant is the densest single type.
        task_id = f"nearby|fine|restaurant|{sample_tile}"
        task = next((t for t in tasks if t["id"] == task_id), None)
        if task is None:
            n_fine = len(grids["fine"])
            print(f"\nNo such tile: {task_id}. Fine grid has {n_fine} tiles (0..{n_fine - 1}).")
            return
        print(f"\n--sample-tile {sample_tile}: fetching {task_id} @ "
              f"({task['lat']}, {task['lng']})")
        places = fetch_task(task, key)
        ckpt = {"fingerprint": plan_fingerprint(), "tasks": {task_id: places}}
        df, saturated = parse_places(ckpt, {t["id"]: t for t in tasks})
        print(f"\nRaw places returned by this tile: {len(places)} (cap = {MAX_RESULT_COUNT})")
        if saturated:
            print("=> CAP BINDS: this tile is saturated — results were truncated, "
                  "the fine grid is too coarse for this area.")
        else:
            print("=> Cap does not bind: tile returned fewer than the cap, "
                  "so coverage is complete here.")
        return

    ckpt = load_checkpoint()
    tasks_by_id = {t["id"]: t for t in tasks}
    refined = set(ckpt["refined"])

    # Resume safety: refinement is deterministic, so regenerate the sub-tile tree
    # of every already-refined parent. Without this, checkpointed child responses
    # would have no entry in tasks_by_id and parse_places would silently fall back
    # to category "restaurant" for them.
    frontier = [t for t in tasks if t["id"] in refined]
    while frontier:
        nxt = []
        for parent in frontier:
            for child in make_children(parent):
                tasks_by_id[child["id"]] = child
                if child["id"] in refined:
                    nxt.append(child)
        frontier = nxt

    if ckpt["tasks"]:
        print(f"\nResuming: {len(ckpt['tasks'])} tasks already checkpointed, "
              f"{len(refined)} already refined.")

    def fetch_pending(batch, label):
        pending = [t for t in batch if t["id"] not in ckpt["tasks"]]
        if not pending:
            return
        if len(pending) > CONFIRM_THRESHOLD and not assume_yes:
            answer = input(f"\n[{label}] {len(pending)} requests exceed the "
                           f"{CONFIRM_THRESHOLD} threshold. Proceed? [y/N] ")
            if answer.strip().lower() != "y":
                print("Aborted.")
                sys.exit(0)
        for n, task in enumerate(pending, 1):
            try:
                places = fetch_task(task, key)
            except RuntimeError as e:
                print(f"\nFATAL on {task['id']}: {e}")
                print(f"Progress checkpointed to {CHECKPOINT_FILE}; re-run to resume.")
                sys.exit(1)
            ckpt["tasks"][task["id"]] = places
            save_task(task["id"], places)
            if n % 25 == 0 or n == len(pending):
                print(f"  [{label}] {n}/{len(pending)} tasks done")
            time.sleep(POLITE_DELAY)

    # Pass 0 is the static 812-request plan; every later pass re-queries only the
    # tiles that saturated in the previous pass, at half the spacing.
    init_checkpoint()
    fetch_pending(tasks, "pass 0")
    pass_num = 0
    while True:
        _, saturated = parse_places(ckpt, tasks_by_id, verbose=False)
        to_refine = [s for s in saturated
                     if s not in refined and tasks_by_id[s]["depth"] < MAX_REFINE_DEPTH]
        if not to_refine:
            break
        pass_num += 1
        new_children = []
        for tid in to_refine:
            new_children.extend(make_children(tasks_by_id[tid]))
            refined.add(tid)
            save_refined(tid)
        for c in new_children:
            tasks_by_id[c["id"]] = c
        depth = new_children[0]["depth"]
        print(f"\nPass {pass_num}: {len(to_refine)} saturated tiles -> "
              f"{len(new_children)} sub-tiles at depth {depth} "
              f"(~{int(round(new_children[0]['spacing']))}m spacing). Fetching...")
        fetch_pending(new_children, f"pass {pass_num}")

    df, saturated = parse_places(ckpt, tasks_by_id, refined=refined)
    df.to_csv(OUTPUT_CSV, index=False)
    print(f"\nWrote {len(df)} rows -> {OUTPUT_CSV}")
    unresolved = [s for s in saturated if s not in refined]
    if unresolved:
        print(f"Reminder: {len(unresolved)} tiles STILL saturated at the depth "
              f"floor (MAX_REFINE_DEPTH={MAX_REFINE_DEPTH}) — {OUTPUT_CSV} is "
              f"INCOMPLETE for those hotspots. Lower REFINE_FACTOR or raise the "
              f"depth cap if this fires.")
    else:
        print(f"Coverage converged after {pass_num} refinement pass(es): no tile "
              f"remains saturated. {OUTPUT_CSV} is a full census of the box under "
              f"the current type list.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Discover Baku amenities via Google Places API (New).")
    ap.add_argument("--dry-run", action="store_true", help="print the request plan and stop")
    ap.add_argument("--sample", action="store_true", help="fetch one tile, print parsed rows, stop")
    ap.add_argument("--sample-tile", type=int, metavar="N",
                    help="fetch the fine restaurant tile at index N, report if the cap binds, stop")
    ap.add_argument("--yes", action="store_true", help="skip the request-count confirmation")
    args = ap.parse_args()
    run(dry_run=args.dry_run, sample=args.sample, sample_tile=args.sample_tile,
        assume_yes=args.yes)
