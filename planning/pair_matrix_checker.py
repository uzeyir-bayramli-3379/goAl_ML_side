import json
import math
import psycopg2
from itertools import combinations
import os
from pathlib import Path
from dotenv import load_dotenv  # pip install python-dotenv if you haven't already
import argparse
# Find the directory where this script actually lives
SCRIPT_DIR = Path(__file__).resolve().parent

# Point to .env in the parent directory (main project root)
ROOT_DIR = SCRIPT_DIR.parent
ENV_PATH = ROOT_DIR / ".env"

load_dotenv(dotenv_path=ENV_PATH)
# --- Constants ---
WALK_SPEED_M_PER_MIN = 80  
DETOUR_FACTOR = 1.4    

# --- Math Functions ---
def haversine(lat1, lng1, lat2, lng2):
    """Calculates the straight-line distance between two points in meters."""
    R = 6371000
    phi_1, phi_2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)
    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi_1) * math.cos(phi_2) * math.sin(delta_lambda / 2.0) ** 2
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

def walk_minutes(meters):
    """Converts straight-line meters to walk minutes using the detour factor[cite: 2]."""
    return (meters * DETOUR_FACTOR) / WALK_SPEED_M_PER_MIN

# --- Processing Functions ---
def load_and_filter_units(json_filepath):
    """Loads JSON, filters for scheduled tiers, and fixes null durations[cite: 1, 5]."""
    with open(json_filepath, 'r', encoding='utf-8') as f:
        units = json.load(f)
    
    scheduled_units = []
    for u in units:
        if u['tier'] in ['primary', 'secondary']:
            # Apply the fallback for null durations (e.g., Muhammad Mosque)[cite: 1, 5]
            if u['duration_minutes'] is None:
                u['duration_minutes'] = 45
            scheduled_units.append(u)
            
    return scheduled_units

def get_db_coordinates(wikidata_ids, db_url):
    """Fetches latitude and longitude for the given Wikidata IDs from Supabase."""
    conn = psycopg2.connect(db_url)
    cursor = conn.cursor()
    
    # Query the database using the schema provided
    query = """
        SELECT wikidata_id, latitude, longitude 
        FROM landmarks 
        WHERE wikidata_id = ANY(%s);
    """
    cursor.execute(query, (wikidata_ids,))
    rows = cursor.fetchall()
    
    cursor.close()
    conn.close()
    
    return {row[0]: (row[1], row[2]) for row in rows if row[1] is not None and row[2] is not None}

def get_unit_centroid(unit, db_coordinates):
    """Averages the coordinates of all landmarks in a unit[cite: 2]."""
    lats, lngs = [], []
    for wid in unit['wikidata_ids']:
        if wid in db_coordinates:
            lat, lng = db_coordinates[wid]
            lats.append(lat)
            lngs.append(lng)
            
    if not lats: 
        return 0.0, 0.0 
        
    return sum(lats) / len(lats), sum(lngs) / len(lngs)

def build_distance_matrix(scheduled_units, db_coordinates):
    """Builds the symmetric matrix of walk minutes between all unit pairs[cite: 2]."""
    dist_matrix = {}
    
    centroids = {u['unit_name']: get_unit_centroid(u, db_coordinates) for u in scheduled_units}
    
    for u1, u2 in combinations(scheduled_units, 2):
        name1, name2 = u1['unit_name'], u2['unit_name']
        lat1, lng1 = centroids[name1]
        lat2, lng2 = centroids[name2]
        
        m = haversine(lat1, lng1, lat2, lng2)
        mins = walk_minutes(m)
        
        dist_matrix[(name1, name2)] = mins
        dist_matrix[(name2, name1)] = mins
        
    return dist_matrix

# --- Execution & Sanity Checks ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pack visit units into a single day time budget.")
    parser.add_argument("--json_name", type=str, required=True, help="Name or path of the visit units JSON file")
    
    args = parser.parse_args()
    # Ensure you are using the SESSION POOLER string for Supabase (IPv4 compatible)
    DATABASE_URL = os.environ.get("DATABASE_URL")
    if not DATABASE_URL:
        raise ValueError("Please set the DATABASE_URL environment variable.")
    
    print("Loading visit units...")
    scheduled_units = load_and_filter_units(args.json_name)
    
    # Extract all unique wikidata_ids needed for the SQL query
    all_wids = list(set(wid for u in scheduled_units for wid in u['wikidata_ids']))
    
    print(f"Fetching coordinates for {len(all_wids)} landmarks...")
    db_coords = get_db_coordinates(all_wids, DATABASE_URL)
    
    print("Building distance matrix...\n")
    matrix = build_distance_matrix(scheduled_units, db_coords)

    with open('distance_matrix.json', 'w') as f:
        # Convert tuple keys to strings (JSON doesn't support tuple keys)
        matrix_serializable = {str(k): v for k, v in matrix.items()}
        json.dump(matrix_serializable, f)
    
    print("Distance matrix saved to distance_matrix.json")

    
    # --- SANITY CHECK 1: Maiden Tower ↔ Shirvanshahs ---
    print("--- Check 1: Maiden Tower ↔ Shirvanshahs ---")
    mt_shirvan = matrix.get(("Maiden Tower", "Palace of the Shirvanshahs Complex"))
    if mt_shirvan:
        print(f"Walk time: {mt_shirvan:.1f} minutes")
        print("Expected: 4-5 minutes[cite: 2]\n")
    else:
        print("Pair not found!\n")
        
    # --- SANITY CHECK 2: Top 5 Longest Pairs ---
    print("--- Check 2: Top 5 Longest Pairs in Icherisheher ---")
    # De-duplicate the symmetric matrix pairs by sorting the tuple keys
    unique_pairs = {tuple(sorted(k)): v for k, v in matrix.items()}
    sorted_pairs = sorted(unique_pairs.items(), key=lambda x: x[1], reverse=True)
    
    for i, (pair, mins) in enumerate(sorted_pairs[:5]):
        print(f"{i+1}. {pair[0]} ↔ {pair[1]}: {mins:.1f} minutes")
    print("Expected: All pairs should be well under 20 minutes (likely 12-15 mins)[cite: 2].\n")

    