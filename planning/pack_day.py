import argparse
import json
import os
from pathlib import Path
import psycopg2
from dotenv import load_dotenv
import json
import ast

with open('distance_matrix.json', 'r') as f:
    matrix = json.load(f)
matrix = {ast.literal_eval(k): v for k, v in matrix.items()}

def get_db_sitelinks(wikidata_ids, db_url):
    """Fetches sitelinks for the given Wikidata IDs from Supabase."""
    conn = psycopg2.connect(db_url)
    cursor = conn.cursor()
    
    query = """
        SELECT wikidata_id, sitelinks 
        FROM landmarks 
        WHERE wikidata_id = ANY(%s);
    """
    # psycopg2 expects a list for ANY()
    cursor.execute(query, (list(wikidata_ids),))
    rows = cursor.fetchall()
    
    cursor.close()
    conn.close()
    
    sitelinks_lookup = {}
    for wid, sitelinks in rows:
        sitelinks_lookup[wid] = int(sitelinks) if sitelinks is not None else 0
        
    return sitelinks_lookup

def load_and_filter_units(json_filepath):
    with open(json_filepath, 'r', encoding='utf-8') as f:
        units = json.load(f)
    
    scheduled_units = []
    all_wids = set()
    
    for u in units:
        if u.get('tier') in ['primary', 'secondary']:
            if u.get('duration_minutes') is None:
                print(f"WARNING: {u['unit_name']} is tier={u['tier']} with null duration — LLM contract violation, flagging for review")
                u['duration_minutes'] = 45  # fallback, but now visible
            
            scheduled_units.append(u)
            for wid in u.get('wikidata_ids', []):
                all_wids.add(wid)
                
    return scheduled_units, list(all_wids)

def select_units_for_day(units, max_visiting_minutes=350):
    """
    Greedy knapsack solver using handbook rules: 
    Sorts by (Tier Rank, -Sitelinks)
    """
    tier_order = {'primary': 0, 'secondary': 1}
    
    sorted_units = sorted(
        units, 
        key=lambda u: (tier_order.get(u['tier'], 2), -u.get('sitelinks', 0))
    )
    
    selected_units = []
    unallocated_units = []
    accumulated_minutes = 0
    
    for u in sorted_units:
        duration = u['duration_minutes']
        
        if accumulated_minutes + duration <= max_visiting_minutes:
            selected_units.append(u)
            accumulated_minutes += duration
        else:
            unallocated_units.append(u)
            
    return selected_units, unallocated_units, accumulated_minutes

# --- Add this to your Processing Functions ---

def order_units_tsp(day_units, dist_matrix):
    """Stage 3: Orders a single day's units to minimize walking."""
    # Grab just the names for the permutation generator
    unit_names = [u['unit_name'] for u in day_units]
    
    best_path = None
    best_cost = float('inf')
    
    # 4-6 stops is microscopic, so permutations is instant
    from itertools import permutations
    for perm in permutations(unit_names):
        cost = 0
        # Sum the walk times between the units in this permutation
        for i in range(len(perm) - 1):
            pair = (perm[i], perm[i+1])
            # The matrix is symmetric and keys are sorted tuples in your check, 
            # so we ensure we look up the key correctly:
            lookup_key = tuple(sorted(pair)) if tuple(sorted(pair)) in dist_matrix else pair
            
            # Fallback to 0 if pair missing (e.g. same location)
            cost += dist_matrix.get(lookup_key, dist_matrix.get((pair[1], pair[0]), 0)) 
            
        if cost < best_cost:
            best_cost = cost
            best_path = perm
            
    # Reconstruct the ordered unit dictionaries
    name_to_unit = {u['unit_name']: u for u in day_units}
    ordered_units = [name_to_unit[name] for name in best_path]
    
    return ordered_units, best_cost

def generate_timeline(ordered_units, dist_matrix, start_offset=0):
    """Stage 4: Emits durations and offsets, including wikidata_ids for downstream consumption."""
    t = start_offset
    timeline = []
    
    for i, u in enumerate(ordered_units):
        # If it's not the first stop, calculate the walk from the previous stop
        if i > 0:
            prev_u = ordered_units[i-1]
            pair = (prev_u['unit_name'], u['unit_name'])
            lookup_key = tuple(sorted(pair)) if tuple(sorted(pair)) in dist_matrix else pair
            walk_mins = dist_matrix.get(lookup_key, dist_matrix.get((pair[1], pair[0]), 0))
            
            timeline.append({
                "type": "transport",
                "mode": "walk",
                "offset": round(t),
                "duration_minutes": round(walk_mins)
            })
            t += walk_mins
            
        # Add the actual visit stop with wikidata_ids included
        timeline.append({
            "type": "stop",
            "unit_name": u['unit_name'],
            "wikidata_ids": u.get('wikidata_ids', []),
            "tier": u['tier'],
            "offset": round(t),
            "duration_minutes": u['duration_minutes']
        })
        t += u['duration_minutes']
        
    return timeline, round(t)


def run_packer(json_name):
    # 1. Setup paths and load environment variables
    script_dir = Path(__file__).resolve().parent
    root_dir = script_dir.parent if (script_dir.parent / ".env").exists() else script_dir
    load_dotenv(root_dir / ".env")
    
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL not found in environment variables. Please check your .env file.")

    json_path = script_dir / json_name
    if not json_path.exists():
        json_path = Path(json_name)
        
    if not json_path.exists():
        raise FileNotFoundError(f"Could not find JSON file: '{json_name}'")

    # 2. Load units and collect Wikidata IDs
    units, all_wids = load_and_filter_units(json_path)
    
    # 3. Fetch sitelinks from DB
    sitelinks_lookup = get_db_sitelinks(all_wids, db_url)
    
    # 4. Inject highest sitelink count into each unit
    for u in units:
        unit_sitelinks = [
            sitelinks_lookup[wid] 
            for wid in u.get('wikidata_ids', []) 
            if wid in sitelinks_lookup
        ]
        # Use the max sitelinks of its component buildings as the unit's overall popularity
        u['sitelinks'] = max(unit_sitelinks) if unit_sitelinks else 0

    print(f"File loaded: {json_path.name}")
    print(f"Total scheduled units in inventory: {len(units)}\n")
    
    # 5. Run Step 2 selection
    day_1, remaining, total_mins = select_units_for_day(units, max_visiting_minutes=350)
    print(f"=== DAY 1 PACKED UNITS ({total_mins} / 350 mins) ===")
    for idx, u in enumerate(day_1, 1):
        print(f"{idx}. [{u['tier'].upper()}] {u['unit_name']} (Sitelinks: {u['sitelinks']}, {u['duration_minutes']}m)")
    print(f"\n=== REMAINING FOR DAY 2+ ({len(remaining)} units) ===")
    for u in remaining:
        print(f" - [{u['tier'].upper()}] {u['unit_name']} (Sitelinks: {u['sitelinks']}, {u['duration_minutes']}m)")

    print("\n--- Running Stage 3 (TSP) ---")
    ordered_day_1, total_walk = order_units_tsp(day_1, matrix)
    print(f"Optimal route found! Total walking: {total_walk:.1f} minutes")

    print("\n--- Running Stage 4 (Timing Timeline) ---")
    timeline, final_t = generate_timeline(ordered_day_1, matrix)

    with open('timeline_no_meal.json', 'w', encoding='utf-8') as f:
        json.dump(timeline, f, indent=2, ensure_ascii=False)

    for item in timeline:
        if item['type'] == 'transport':
            print(f"  ↓ Walk {item['duration_minutes']} mins")
        else:
            print(f"[{item['offset']}m] {item['unit_name']} ({item['duration_minutes']} mins)")
    print(f"\nDay 1 concludes at offset: {final_t} minutes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pack visit units into a single day time budget.")
    parser.add_argument("--json_name", type=str, required=True, help="Name or path of the visit units JSON file")
    
    args = parser.parse_args()
    run_packer(args.json_name)


# --- How to run it in your execution block ---
# Assuming 'day_1' is the array of 4 units outputted by your knapsack packer
"""
print("\n--- Running Stage 3 (TSP) ---")
ordered_day_1, total_walk = order_units_tsp(day_1, matrix)
print(f"Optimal route found! Total walking: {total_walk:.1f} minutes")

print("\n--- Running Stage 4 (Timing Timeline) ---")
timeline, final_t = generate_timeline(ordered_day_1, matrix)

for item in timeline:
    if item['type'] == 'transport':
        print(f"  ↓ Walk {item['duration_minutes']} mins")
    else:
        print(f"[{item['offset']}m] {item['unit_name']} ({item['duration_minutes']} mins)")
print(f"\nDay 1 concludes at offset: {final_t} minutes")
"""