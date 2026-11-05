import argparse
import ast
import json
import math
import os
from itertools import permutations
from pathlib import Path
import psycopg2
from dotenv import load_dotenv


# --- 1. SPATIAL & HAVERSINE HELPERS ---

def haversine_walk_minutes(coord1: tuple, coord2: tuple, speed_m_per_min=80.0, detour_factor=1.4) -> float:
    """
    Calculates walk time in minutes between two (lat, lon) coordinates using
    Haversine distance and a detour factor.
    """
    lat1, lon1 = coord1
    lat2, lon2 = coord2
    R = 6371000.0  # Earth radius in meters
    
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    
    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    dist_meters = R * c
    return (dist_meters * detour_factor) / speed_m_per_min


def parse_start_point(coord_str: str) -> tuple:
    """Parses CLI input formatted as 'lat,lon' or '(lat,lon)' into a float tuple."""
    try:
        clean_str = coord_str.strip("()[]{} ")
        parts = clean_str.split(",")
        if len(parts) != 2:
            raise ValueError
        lat, lon = float(parts[0].strip()), float(parts[1].strip())
        return (lat, lon)
    except Exception:
        raise argparse.ArgumentTypeError(
            "start_point must be formatted as 'lat,lon' (e.g., '40.3701,49.8352')"
        )


# --- 2. SUPABASE POSTGIS HELPERS ---

def get_db_sitelinks(wikidata_ids: list, db_url: str) -> dict:
    """Fetches sitelink counts from Supabase."""
    if not wikidata_ids:
        return {}

    conn = psycopg2.connect(db_url)
    cursor = conn.cursor()
    query = "SELECT wikidata_id, sitelinks FROM landmarks WHERE wikidata_id = ANY(%s);"
    cursor.execute(query, (list(wikidata_ids),))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    
    return {row[0]: (int(row[1]) if row[1] is not None else 0) for row in rows}


def fetch_anchor_mapping(wikidata_ids: list, db_url: str) -> dict:
    """Matches landmarks directly against landmarks.anchor_id, enforcing schema constraints."""
    if not wikidata_ids:
        return {}

    query = """
    SELECT l.wikidata_id, a.name AS anchor_name
    FROM landmarks l
    LEFT JOIN anchors a ON l.anchor_id = a.id
    WHERE l.wikidata_id = ANY(%s);
    """

    conn = psycopg2.connect(db_url)
    cursor = conn.cursor()
    cursor.execute(query, (list(wikidata_ids),))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    # If anchor_name is NULL in the DB, it stays None here. No fallback.
    return {row[0]: row[1] for row in rows}

def fetch_landmark_coords(wikidata_ids: list, db_url: str) -> dict:
    """Fetches (lat, lon) centroids for landmarks from PostGIS."""
    if not wikidata_ids:
        return {}

    query = """
        SELECT wikidata_id, 
               ST_Y(geom::geometry) AS lat, 
               ST_X(geom::geometry) AS lon
        FROM landmarks 
        WHERE wikidata_id = ANY(%s);
    """
    conn = psycopg2.connect(db_url)
    cursor = conn.cursor()
    cursor.execute(query, (list(wikidata_ids),))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    return {row[0]: (float(row[1]), float(row[2])) for row in rows if row[1] is not None and row[2] is not None}


def determine_anchor_sequence(start_point: tuple, db_url: str, city_id: int = 1) -> list[str]:
    """
    Stage 0: Decide the anchor sequence for the trip based on the user's start_point.
    Returns an ordered list of anchor names.
    """
    from multi_daying import BAKU_TRANSIT_TABLE
    import psycopg2
    from itertools import permutations

    conn = psycopg2.connect(db_url)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name, ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng, radius_m "
        "FROM anchors WHERE city_id = %s;", 
        (city_id,)
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    if not rows:
        raise ValueError(f"No anchors found for city_id {city_id}")

    anchors = {row[0]: (float(row[1]), float(row[2])) for row in rows}

    # Day 1 Anchor: Nearest to user's start_point
    best_start = None
    best_dist = float('inf')
    for name, coords in anchors.items():
        dist = haversine_walk_minutes(start_point, coords)
        if dist < best_dist:
            best_dist = dist
            best_start = name

    remaining_anchors = [name for name in anchors.keys() if name != best_start]
    
    if not remaining_anchors:
        return [best_start]

    # Permute remainder by total transition cost
    best_seq = None
    best_cost = float('inf')

    for perm in permutations(remaining_anchors):
        seq = [best_start] + list(perm)
        cost = 0
        for i in range(len(seq) - 1):
            pair = (seq[i], seq[i+1])
            lookup_key = tuple(sorted(pair))
            
            if lookup_key in BAKU_TRANSIT_TABLE:
                cost += BAKU_TRANSIT_TABLE[lookup_key]["duration_minutes"]
            else:
                # Purely for sequence generation; never used as real transit time.
                cost += haversine_walk_minutes(anchors[seq[i]], anchors[seq[i+1]])
                
        if cost < best_cost:
            best_cost = cost
            best_seq = seq

    return best_seq



# --- 3. DATA PREPARATION ---

def load_and_prepare_units(json_filepath: Path, db_url: str):
    """Loads units, filters scheduled tiers, and attaches sitelinks, anchors, and coordinates."""
    with open(json_filepath, 'r', encoding='utf-8') as f:
        raw_units = json.load(f)
    
    scheduled_units = []
    all_wids = set()
    
    for u in raw_units:
        if u.get('tier') in ['primary', 'secondary']:
            if u.get('duration_minutes') is None:
                u['duration_minutes'] = 45
            
            scheduled_units.append(u)
            for wid in u.get('wikidata_ids', []):
                all_wids.add(wid)

    wids_list = list(all_wids)
    print(f"Querying Supabase for {len(wids_list)} landmark metadata records...")
    
    sitelinks_lookup = get_db_sitelinks(wids_list, db_url)
    anchor_lookup = fetch_anchor_mapping(wids_list, db_url)
    coords_lookup = fetch_landmark_coords(wids_list, db_url)
    
    scheduled_units_final = []
    excluded_units = []

    for u in scheduled_units:
        unit_wids = u.get('wikidata_ids', [])
        
        unit_sitelinks = [sitelinks_lookup[wid] for wid in unit_wids if wid in sitelinks_lookup]
        u['sitelinks'] = max(unit_sitelinks) if unit_sitelinks else 0
        
        unit_anchors = [anchor_lookup[wid] for wid in unit_wids if wid in anchor_lookup and anchor_lookup[wid] is not None]
        u['anchor'] = unit_anchors[0] if unit_anchors else None
        
        unit_coords = [coords_lookup[wid] for wid in unit_wids if wid in coords_lookup]
        u['coords'] = unit_coords[0] if unit_coords else None

        # Route out-of-scope units (like day-trips) explicitly
        if u['anchor'] is None:
            excluded_units.append(u)
        else:
            scheduled_units_final.append(u)

    return scheduled_units_final, excluded_units


# --- 4. TSP & TIMELINE GENERATION ---

def select_units_for_day(pool_by_anchor: dict, anchor_sequence: list, seq_ptr: int, budget_min: int = 350):
    """
    Phase 1: Exhaust the current anchor's units.
    Phase 2: If exhausted and >=90m remains, consider a single hop.
    Returns: (chosen_units, new_seq_ptr)
    """
    from multi_daying import BAKU_TRANSIT_TABLE
    MIN_HOP_WORTH = 90
    tier_order = {'primary': 0, 'secondary': 1}
    
    chosen_units = []
    spent = 0
    
    # --- PHASE 1: Current Anchor ---
    current_anchor = anchor_sequence[seq_ptr]
    current_pool = pool_by_anchor.get(current_anchor, [])
    
    sorted_current = sorted(
        current_pool, 
        key=lambda u: (tier_order.get(u.get('tier'), 2), -u.get('sitelinks', 0))
    )
    
    unallocated_current = []
    for u in sorted_current:
        if spent + u['duration_minutes'] <= budget_min:
            chosen_units.append(u)
            spent += u['duration_minutes']
        else:
            unallocated_current.append(u)
            
    pool_by_anchor[current_anchor] = unallocated_current
    
    # --- PHASE 2: Check for a Hop ---
    remaining_budget = budget_min - spent
    
    # Only hop if Phase 1 anchor is fully exhausted AND budget remains
    if len(unallocated_current) == 0 and remaining_budget >= MIN_HOP_WORTH and seq_ptr + 1 < len(anchor_sequence):
        next_anchor = anchor_sequence[seq_ptr + 1]
        lookup_key = tuple(sorted([current_anchor, next_anchor]))
        
        # Hard fail on missing transit table entry (end day, don't guess)
        if lookup_key in BAKU_TRANSIT_TABLE:
            hop = BAKU_TRANSIT_TABLE[lookup_key]
            transit_cost = hop["duration_minutes"]
            
            if remaining_budget - transit_cost > 0:
                next_pool = pool_by_anchor.get(next_anchor, [])
                sorted_next = sorted(
                    next_pool, 
                    key=lambda u: (tier_order.get(u.get('tier'), 2), -u.get('sitelinks', 0))
                )
                
                next_spent = transit_cost
                next_chosen = []
                next_unallocated = []
                
                for u in sorted_next:
                    if next_spent + u['duration_minutes'] <= remaining_budget:
                        next_chosen.append(u)
                        next_spent += u['duration_minutes']
                    else:
                        next_unallocated.append(u)
                        
                # Only commit to hop if it nets at least 1 stop
                if len(next_chosen) > 0:
                    hop_item = {
                        "type": "transport",
                        "mode": hop["mode"],
                        "duration_minutes": transit_cost,
                        "notes": hop["notes"],
                        "inter_anchor": True,
                        "anchor_a": current_anchor,
                        "anchor_b": next_anchor
                    }
                    chosen_units.append(hop_item)
                    chosen_units.extend(next_chosen)
                    pool_by_anchor[next_anchor] = next_unallocated
                    return chosen_units, seq_ptr + 1

    return chosen_units, seq_ptr


def order_units_tsp(day_units, dist_matrix, start_coords=None):
    # Partition by anchor — TSP runs independently per anchor group
    anchors_seen = []
    by_anchor = {}
    for u in day_units:
        a = u.get('anchor', 'unknown')
        if a not in by_anchor:
            anchors_seen.append(a)
            by_anchor[a] = []
        by_anchor[a].append(u)

    ordered_all = []
    total_cost = 0.0

    for idx, anchor in enumerate(anchors_seen):
        group = by_anchor[anchor]
        if len(group) == 1:
            ordered_all.extend(group)
            continue

        names = [u['unit_name'] for u in group]
        name_to_unit = {u['unit_name']: u for u in group}
        best_path, best_cost = None, float('inf')

        for perm in permutations(names):
            cost = 0.0
            # Start-point walk only on the very first stop of the whole day
            if idx == 0 and start_coords and name_to_unit[perm[0]].get('coords'):
                cost += haversine_walk_minutes(start_coords, name_to_unit[perm[0]]['coords'])
            for i in range(len(perm) - 1):
                pair = (perm[i], perm[i + 1])
                key = tuple(sorted(pair))
                cost += dist_matrix.get(key, dist_matrix.get((pair[1], pair[0]), 0))
            if cost < best_cost:
                best_cost, best_path = cost, perm

        ordered_all.extend([name_to_unit[n] for n in best_path])
        total_cost += best_cost

    return ordered_all, total_cost


def generate_day_timeline(ordered_units, dist_matrix, start_coords=None, start_offset=0, hop_items=None):
    t = start_offset
    timeline = []
    hop_injected = False

    # Initial walk leg from start_point
    if start_coords and ordered_units and ordered_units[0].get('coords'):
        first_walk = haversine_walk_minutes(start_coords, ordered_units[0]['coords'])
        timeline.append({
            "type": "transport", "mode": "walk",
            "offset": round(t), "duration_minutes": round(first_walk)
        })
        t += first_walk

    for i, u in enumerate(ordered_units):
        if i > 0:
            prev_u = ordered_units[i - 1]
            
            # If anchor changes and we have an unused hop item, insert it here
            if (not hop_injected
                    and hop_items
                    and u.get('anchor') != prev_u.get('anchor')):
                hop = hop_items[0]
                timeline.append({**hop, "offset": round(t)})
                t += hop['duration_minutes']
                hop_injected = True
            else:
                # Normal intra-anchor walk
                pair = (prev_u['unit_name'], u['unit_name'])
                lookup_key = tuple(sorted(pair)) if tuple(sorted(pair)) in dist_matrix else pair
                walk_mins = dist_matrix.get(lookup_key, dist_matrix.get((pair[1], pair[0]), 0))
                timeline.append({
                    "type": "transport", "mode": "walk",
                    "offset": round(t), "duration_minutes": round(walk_mins)
                })
                t += walk_mins

        timeline.append({
            "type": "stop",
            "unit_name": u['unit_name'],
            "wikidata_ids": u.get('wikidata_ids', []),
            "anchor": u.get('anchor'),
            "tier": u['tier'],
            "offset": round(t),
            "duration_minutes": u['duration_minutes']
        })
        t += u['duration_minutes']

    return timeline, round(t)


# --- 5. MASTER RUNNER ---

def run_packer(json_name: str, matrix_file: str = 'distance_matrix.json', start_coords: tuple = None):
    script_dir = Path(__file__).resolve().parent
    root_dir = script_dir.parent if (script_dir.parent / ".env").exists() else script_dir
    load_dotenv(root_dir / ".env")
    
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL not found in environment variables.")

    # Load distance matrix
    matrix_path = script_dir / matrix_file
    if not matrix_path.exists():
        raise FileNotFoundError(f"Distance matrix file not found: {matrix_path}")
        
    with open(matrix_path, 'r', encoding='utf-8') as f:
        raw_matrix = json.load(f)
    dist_matrix = {ast.literal_eval(k): v for k, v in raw_matrix.items()}

    # Load units
    json_path = script_dir / json_name
    if not json_path.exists():
        json_path = Path(json_name)
    if not json_path.exists():
        raise FileNotFoundError(f"Could not find JSON file: '{json_name}'")

    units, excluded_units = load_and_prepare_units(json_path, db_url)
    print(f"Loaded {len(units)} scheduled units from {json_path.name}")
    if start_coords:
        print(f"Using starting location: Lat {start_coords[0]}, Lon {start_coords[1]}")
    print()

    # Group units by anchor
    pool_by_anchor = {}
    for u in units:
        anchor_name = u.get('anchor')
        if anchor_name:
            pool_by_anchor.setdefault(anchor_name, []).append(u)

    # Determine anchor sequence
    if start_coords:
        anchor_sequence = determine_anchor_sequence(start_coords, db_url)
    else:
        anchor_sequence = list(pool_by_anchor.keys())

    multi_day_raw_timelines = {}
    seq_ptr = 0
    day_num = 1

    # Multi-day packing loop
    while any(pool_by_anchor.values()) and seq_ptr < len(anchor_sequence):
        selected_day, seq_ptr = select_units_for_day(
            pool_by_anchor=pool_by_anchor,
            anchor_sequence=anchor_sequence,
            seq_ptr=seq_ptr,
            budget_min=350
            )

        if not selected_day:
            break
        
        # Separate stops from the inter-anchor hop item before TSP
        hop_items = [u for u in selected_day if u.get('type') == 'transport']
        stop_items = [u for u in selected_day if u.get('type') != 'transport']
        
        total_visit_mins = sum(u.get('duration_minutes', 0) for u in stop_items)
        
        day_start = start_coords if day_num == 1 else None
        
        ordered_stops, total_walk_mins = order_units_tsp(stop_items, dist_matrix, start_coords=day_start)
        day_timeline, _ = generate_day_timeline(ordered_stops, dist_matrix, start_coords=day_start, hop_items=hop_items)
        
        multi_day_raw_timelines[str(day_num)] = day_timeline
        print(f"Day {day_num} Packed: {len(selected_day)} items | Visit: {total_visit_mins}m | Walk: {total_walk_mins:.1f}m")
        day_num += 1

    output_filename = 'multi_day_raw_timelines.json'
    with open(output_filename, 'w', encoding='utf-8') as f:
        json.dump(multi_day_raw_timelines, f, indent=2, ensure_ascii=False)

    print(f"\nSaved raw multi-day timelines to '{output_filename}'.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pack visit units into multi-day raw timelines.")
    parser.add_argument("--json_name", type=str, required=True, help="Path to input visit units JSON")
    parser.add_argument("--matrix_file", type=str, default="distance_matrix.json", help="Path to distance matrix JSON")
    parser.add_argument(
        "--start_point", 
        type=parse_start_point, 
        default=None, 
        help="User starting location formatted as 'lat,lon' or 'latitude,longitude' (e.g. --start_point 40.3701,49.8352)"
    )
    
    args = parser.parse_args()
    run_packer(args.json_name, args.matrix_file, args.start_point)