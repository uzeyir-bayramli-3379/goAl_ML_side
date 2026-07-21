"""GoAI Baku amenity discovery: cafes, restaurants, and leisure/niche spots.

This is a SIBLING pipeline to data_getting.py + enrich_landmarks.py, not an
extension of it. Landmarks are sparse, fact-rich, and narrated (Gemini writes
a paragraph grounded in verified history — hallucination risk = invented
history). Amenities are dense, fact-poor, and SERVED not narrated (a cafe
gets structured retrieval — category, cuisine, price if available, distance —
not a generated blurb, because "cozy atmosphere with locally roasted beans"
invented from a name and a category is the exact same hallucination shape,
just for lower-stakes content).

Source is OpenStreetMap via Overpass, not Wikidata: Wikidata only has entries
that clear an ENCYCLOPEDIA notability bar, so cafes/restaurants are near-zero
there structurally, not due to a bad query. OSM is the mirror image — huge
POI density, almost no descriptive text.

Design notes carried over from the landmark pipeline:
  - Missing tag => None, never "" or a guess (grounding/serving depends on
    absence being visible downstream).
  - Drop only unambiguous junk (no name). Flag ambiguous cases (coord
    collisions, chain-location clusters) rather than deleting them.
  - Structured error handling: Overpass timeouts and rate limits get
    surfaced with their actual body, not swallowed as a generic failure.

NOT yet run against the live Overpass endpoint from this environment — the
sandbox's network allowlist doesn't include overpass-api.de. Run this
locally; the parsing logic below is smoke-tested against synthetic Overpass
JSON in test_get_amenities.py so the shape is verified even without a live call.

Run:
    python get_amenities.py             # core walkable zone (CORE_BOX)
    python get_amenities.py --wide      # full sanity box (whole city + Absheron)
"""

import argparse
import sys
import time

import pandas as pd
import requests

# Mirror instances run the same API; a single flaky one shouldn't be a hard
# block, so the fetch layer falls over to the next on repeated failure.
# OVERPASS_URL stays as the default endpoint constant (swap the order to prefer
# a different mirror).
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OVERPASS_URL = OVERPASS_MIRRORS[0]

# Descriptive User-Agent is MANDATORY for Overpass — the front end answers 406
# Not Acceptable to the bare python-requests default. Same headers style as
# data_getting.py so the two pipelines stay consistent (Accept swapped to JSON).
HEADERS = {
    "User-Agent": "GoAI/1.0 (https://github.com/uzeyir-bayramli-3379; contact@goai.app)",
    "Accept": "application/json",
}

# Reuse the same two boxes as the landmark pipeline for consistency, so a
# landmark and a cafe both inside "the core" mean the same thing app-wide.
CORE_BOX   = {"lat": (40.34, 40.44), "lon": (49.79, 49.90)}   # walkable tourist core
SANITY_BOX = {"lat": (40.00, 40.65), "lon": (49.30, 50.90)}   # whole city + Absheron

OVERPASS_QL_TIMEOUT = 60  # seconds, passed inside the query itself ([timeout:N])
# Client-side timeout kept comfortably above the in-query timeout so we never cut
# off a query the server would otherwise finish.
REQUEST_TIMEOUT = OVERPASS_QL_TIMEOUT + 30

MAX_RETRIES = 4      # per mirror, for transient failures (429 / 504 / remark)
BACKOFF_BASE = 2     # seconds; wait grows BACKOFF_BASE * attempt

# --- Category taxonomy ------------------------------------------------------
# (osm_key, osm_value) -> our category label. Priority list, same pattern as
# CLASS_LABELS/CLASS_PRIORITY in data_getting.py: earliest wins if an element
# somehow carries multiple matching tags (rare here, but keeps it deterministic).
CATEGORY_TAGS = {
    ("amenity", "restaurant"):       "restaurant",
    ("amenity", "cafe"):             "cafe",
    ("amenity", "fast_food"):        "fast_food",
    ("amenity", "bar"):              "bar",
    ("amenity", "pub"):              "pub",
    ("amenity", "ice_cream"):        "ice_cream",
    ("amenity", "cinema"):           "cinema",
    ("amenity", "nightclub"):        "nightclub",
    ("amenity", "casino"):           "casino",
    # De-facto tag, verified on the OSM wiki — expect near-zero hits in Baku,
    # keep it in rather than assume; costs nothing to ask Overpass for.
    ("amenity", "photo_booth"):      "photo_booth",
    ("leisure", "amusement_arcade"): "arcade",
    ("leisure", "bowling_alley"):    "bowling",
    ("leisure", "escape_game"):      "escape_room",
    ("leisure", "water_park"):       "water_park",
    ("leisure", "miniature_golf"):   "mini_golf",
    ("tourism", "theme_park"):       "theme_park",
    ("tourism", "aquarium"):         "aquarium",
    # --- Wider taxonomy: cast wide now, filter later. Tags on any key slot in
    # because _build_query loops over arbitrary (key, value) pairs. ---
    ("leisure", "fitness_centre"):   "gym",
    ("leisure", "sports_centre"):    "sports_centre",
    ("leisure", "dance"):            "dance_venue",
    ("leisure", "park"):             "park",
    ("leisure", "garden"):           "garden",
    ("shop", "pastry"):              "pastry_shop",
    ("shop", "confectionery"):       "confectionery",
    ("amenity", "food_court"):       "food_court",
    ("amenity", "hookah_lounge"):    "hookah",
    ("tourism", "zoo"):              "zoo",
}
CATEGORY_PRIORITY = list(CATEGORY_TAGS)

# Tags pulled onto every element, null if absent. Kept flat and honest about
# what OSM actually has: no standardized price field exists in practice
# (price_range/cost tags are used inconsistently and shouldn't be trusted as
# structured data), so budget-tier info is NOT fabricated here — it stays
# None until you have a real source (hand curation, or a review-site API).
TAG_FIELDS = [
    "cuisine", "opening_hours", "phone", "website",
    "wheelchair", "outdoor_seating", "smoking",
    "addr:street", "addr:housenumber",
]


def _build_query(box, timeout=OVERPASS_QL_TIMEOUT):
    south, north = box["lat"]
    west, east = box["lon"]
    bbox = f"{south},{west},{north},{east}"

    clauses = []
    for key, value in CATEGORY_TAGS:
        for elem in ("node", "way", "relation"):
            clauses.append(f'  {elem}["{key}"="{value}"]({bbox});')

    return f"""
[out:json][timeout:{timeout}];
(
{chr(10).join(clauses)}
);
out center tags;
"""


def _elem_id(el):
    return f'{el["type"][0]}{el["id"]}'  # e.g. "n123", "w456" — OSM ids collide across types


def _coords(el):
    if el["type"] == "node":
        return el.get("lat"), el.get("lon")
    center = el.get("center") or {}
    return center.get("lat"), center.get("lon")  # way/relation: centroid, via `out center`


def _category(tags):
    for key, value in CATEGORY_PRIORITY:
        if tags.get(key) == value:
            return CATEGORY_TAGS[(key, value)]
    return "unknown"  # shouldn't happen — every returned element matched a VALUES clause


def _parse_element(el):
    tags = el.get("tags", {})
    name = tags.get("name")  # no name => can't ground or display => hard-drop candidate

    lat, lon = _coords(el)

    rec = {
        "OSM_ID": _elem_id(el),
        "OSM_Type": el["type"],
        "Name": name,
        "Latitude": lat,
        "Longitude": lon,
        "Category": _category(tags),
    }
    for field in TAG_FIELDS:
        rec[field.replace(":", "_")] = tags.get(field)  # None if absent — no fabrication
    return rec


def _fetch_elements(query, endpoints=OVERPASS_MIRRORS, verbose=True):
    """POST the Overpass QL and return the parsed `elements` list.

    Hardening: descriptive headers (avoids the 406), retry-with-backoff on the
    transient failures Overpass throws under load (429 rate limit, 504 gateway
    timeout, and the HTTP-200 `remark` runtime error), and mirror fallover so a
    single flaky instance isn't a hard block. Fails LOUDLY (sys.exit) if every
    mirror is exhausted — never returns partial/empty data as success."""
    for endpoint in endpoints:
        if verbose:
            print(f"Querying {endpoint} ...")

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = requests.post(endpoint, data={"data": query},
                                  headers=HEADERS, timeout=REQUEST_TIMEOUT)
            except requests.exceptions.RequestException as e:
                print(f"  network error (attempt {attempt}/{MAX_RETRIES}): {e}")
                time.sleep(BACKOFF_BASE * attempt)
                continue

            # Transient: rate-limited -> respect Retry-After when present.
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After") or BACKOFF_BASE * attempt)
                print(f"  429 rate-limited; waiting {wait}s (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            # Transient: gateway timeout under load -> exponential-ish backoff.
            if r.status_code == 504:
                wait = BACKOFF_BASE * attempt
                print(f"  504 gateway timeout; backing off {wait}s (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                continue

            # Non-transient HTTP error (400 bad query, 406 bad headers, ...):
            # retrying this mirror won't help — surface the body, try next mirror.
            if not r.ok:
                print(f"  HTTP {r.status_code} on {endpoint}. Body (first 500 chars):\n{r.text[:500]}")
                break

            try:
                payload = r.json()
            except ValueError as e:
                print(f"  non-JSON 200 body (attempt {attempt}/{MAX_RETRIES}): {e}\n{r.text[:500]}")
                time.sleep(BACKOFF_BASE * attempt)
                continue

            # Overpass reports runtime errors (query timeout / out-of-memory) as a
            # `remark` field WITH HTTP 200 and empty/partial elements. Treat it as
            # a transient failure and retry — accepting it would silently write a
            # short CSV and look like success.
            remark = payload.get("remark")
            if remark:
                print(f"  Overpass remark (attempt {attempt}/{MAX_RETRIES}): {remark}")
                time.sleep(BACKOFF_BASE * attempt)
                continue

            if "elements" not in payload:
                print(f"  unexpected response shape (no 'elements'):\n{r.text[:500]}")
                break

            return payload["elements"]

        if verbose:
            print(f"  giving up on {endpoint}; trying next mirror if any.")

    print("All Overpass mirrors failed. Aborting without writing partial data.")
    sys.exit(1)


def discover_amenities(box=CORE_BOX, verbose=True):
    """Run the Overpass discovery query and return the resolved `amenities`
    dict keyed by OSM_ID (type-prefixed, since node/way/relation ids collide).
    """
    query = _build_query(box)
    elements = _fetch_elements(query, verbose=verbose)

    amenities = {}
    for el in elements:
        rec = _parse_element(el)
        amenities[rec["OSM_ID"]] = rec

    if verbose:
        print(f"Discovered {len(amenities)} raw elements.")
    return amenities


def clean_amenities(amenities: dict, verbose=True):
    """Drop only unambiguous junk (no name, no coords). Flag everything else —
    coord collisions (common in OSM: a building outline AND a POI node both
    tagged, or a real duplicate chain location) are kept, not merged, since a
    wrong auto-merge silently hides a real second location."""
    kept, dropped = [], []

    def drop(rec, reason):
        dropped.append((rec["OSM_ID"], rec.get("Name"), reason))

    for rec in amenities.values():
        if not rec["Name"]:
            drop(rec, "no_name"); continue
        if rec["Latitude"] is None or rec["Longitude"] is None:
            drop(rec, "no_coords"); continue
        kept.append(rec)

    # Flag (not drop) coordinate collisions — same discipline as landmarks.
    coord_counts = {}
    for rec in kept:
        ck = (round(rec["Latitude"], 5), round(rec["Longitude"], 5))
        coord_counts.setdefault(ck, []).append(rec["OSM_ID"])
    collisions = {ck: i + 1 for i, (ck, ids) in enumerate(coord_counts.items()) if len(ids) > 1}
    for rec in kept:
        ck = (round(rec["Latitude"], 5), round(rec["Longitude"], 5))
        rec["coord_group"] = collisions.get(ck)

    clean_df = pd.DataFrame(kept).sort_values(["Category", "Name"])
    report = pd.DataFrame(dropped, columns=["OSM_ID", "Name", "Reason"])

    if verbose:
        print(f"Kept {len(kept)} / {len(amenities)}  ({len(dropped)} dropped)")
        if len(dropped):
            print(report["Reason"].value_counts().to_string())
        print(f"\nBy category:\n{clean_df['Category'].value_counts().to_string()}")
        print(f"\npin collisions: {len(collisions)} groups")

    return clean_df, report


def main(wide=False):
    box = SANITY_BOX if wide else CORE_BOX
    amenities = discover_amenities(box)
    clean_df, report = clean_amenities(amenities)
    clean_df.to_csv("baku_amenities_clean.csv", index=False)
    report.to_csv("baku_amenities_dropped.csv", index=False)
    print(f"\nWrote {len(clean_df)} rows -> baku_amenities_clean.csv")
    print(f"Wrote {len(report)} rows -> baku_amenities_dropped.csv")
    return clean_df, report


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Discover Baku cafes/restaurants/leisure spots via Overpass.")
    ap.add_argument("--wide", action="store_true",
                    help="use the full city+Absheron box instead of the walkable core")
    args = ap.parse_args()
    main(wide=args.wide)