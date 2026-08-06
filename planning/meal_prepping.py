import psycopg2
import json
from pair_matrix_checker import get_unit_centroid, get_db_coordinates
from pathlib import Path
from dotenv import load_dotenv
import os

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
ENV_PATH = ROOT_DIR / ".env"

MEAL_CATEGORIES = ['restaurant', 'cafe', 'food', 'fast_food']
MEAL_DURATION = 75  # 1 hour 15 min sit-down meal


def fetch_nearest_meal_amenity(mid_lat, mid_lng, db_url):
    """
    Queries PostGIS for the single nearest food amenity from amenities_places 
    using expanding search radiuses (300m -> 500m -> 800m).
    """
    conn = psycopg2.connect(db_url)
    cursor = conn.cursor()
    
    query = """
        SELECT place_id, name, category, lat, lng
        FROM amenities_places
        WHERE category = ANY(%s)
          AND duplicate_of IS NULL
          AND (business_status IS NULL OR business_status != 'CLOSED_PERMANENTLY')
          AND ST_DWithin(
              geom,
              ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
              %s
          )
        ORDER BY ST_Distance(
            geom,
            ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
        )
        LIMIT 1;
    """
    
    chosen_amenity = None
    for radius in [300, 500, 800]:
        cursor.execute(query, (MEAL_CATEGORIES, mid_lng, mid_lat, radius, mid_lng, mid_lat))
        row = cursor.fetchone()
        if row:
            chosen_amenity = {
                "place_id": row[0],
                "name": row[1],
                "category": row[2],
                "lat": row[3],
                "lng": row[4],
                "radius_matched": radius
            }
            break

    cursor.close()
    conn.close()
    
    return chosen_amenity


def insert_meal_slot(timeline, centroids, db_url, target_offset=240):
    """
    Stage 5: Inserts a 75-minute lunch slot into the timeline at the stop 
    boundary closest to target_offset (~240m / midday).
    """
    # 1. Extract indices of all 'stop' items in the timeline
    stop_indices = [idx for idx, item in enumerate(timeline) if item['type'] == 'stop']
    if len(stop_indices) < 2:
        print("Not enough stops to insert a meal between.")
        return timeline, timeline[-1]['offset'] + timeline[-1]['duration_minutes']

    # 2. Find the stop boundary closest to midday (240 mins)
    best_boundary_idx = None
    best_diff = float('inf')
    prev_stop_name = None
    next_stop_name = None
    
    for k in range(len(stop_indices) - 1):
        idx_prev = stop_indices[k]
        item_prev = timeline[idx_prev]
        
        boundary_offset = item_prev['offset'] + item_prev['duration_minutes']
        diff = abs(boundary_offset - target_offset)
        
        if diff < best_diff:
            best_diff = diff
            # Target insertion position immediately following the transport item (if present)
            best_boundary_idx = idx_prev + 2 if (idx_prev + 1 < len(timeline) and timeline[idx_prev + 1]['type'] == 'transport') else idx_prev + 1
            prev_stop_name = item_prev['unit_name']
            next_stop_name = timeline[stop_indices[k+1]]['unit_name']

    # 3. Compute midpoint coordinates, dropping any invalid (0.0, 0.0) points
    c1 = centroids.get(prev_stop_name, (0.0, 0.0))
    c2 = centroids.get(next_stop_name, (0.0, 0.0))

    valid_lats = [c[0] for c in [c1, c2] if c[0] != 0.0]
    valid_lngs = [c[1] for c in [c1, c2] if c[1] != 0.0]

    if not valid_lats:
        print("[WARNING] Missing coordinates for adjacent stops. Skipping meal injection.")
        return timeline, timeline[-1]['offset'] + timeline[-1]['duration_minutes']

    mid_lat = sum(valid_lats) / len(valid_lats)
    mid_lng = sum(valid_lngs) / len(valid_lngs)

    # 4. Fetch nearest meal amenity
    meal = fetch_nearest_meal_amenity(mid_lat, mid_lng, db_url)
    meal_label = f"Lunch near {prev_stop_name} ({meal['name']})" if meal else f"Lunch near {prev_stop_name}"

    insert_offset = timeline[best_boundary_idx - 1]['offset'] + timeline[best_boundary_idx - 1]['duration_minutes']

    meal_item = {
        "type": "meal",
        "unit_name": meal_label,
        "offset": insert_offset,
        "duration_minutes": MEAL_DURATION
    }

    # 5. Insert meal item into timeline
    timeline.insert(best_boundary_idx, meal_item)

    # 6. Shift offsets of all subsequent items by MEAL_DURATION
    for i in range(best_boundary_idx + 1, len(timeline)):
        timeline[i]['offset'] += MEAL_DURATION

    # Calculate final day duration
    last_item = timeline[-1]
    final_offset = last_item['offset'] + last_item['duration_minutes']

    return timeline, final_offset


if __name__ == "__main__":
    load_dotenv(dotenv_path=ENV_PATH)
    DATABASE_URL = os.environ.get("DATABASE_URL")
    
    with open('timeline_no_meal.json', 'r', encoding='utf-8') as f:
        day_1_timeline = json.load(f)
        
    print("\n--- Running Stage 5 (Meal Slot Injection) ---")
    
    # Safely extract wikidata_ids ONLY from 'stop' items
    stop_items = [u for u in day_1_timeline if u.get('type') == 'stop']
    all_wids = list(set(wid for u in stop_items for wid in u.get('wikidata_ids', [])))
    
    print(f"Fetching coordinates for {len(all_wids)} landmark WIDs...")
    db_coords = get_db_coordinates(all_wids, DATABASE_URL)
    
    # Safely map centroids ONLY for 'stop' items
    centroids = {u['unit_name']: get_unit_centroid(u, db_coords) for u in stop_items}

    # Run meal injection for Day 1
    updated_timeline, final_day_duration = insert_meal_slot(
        timeline=day_1_timeline,
        centroids=centroids,
        db_url=DATABASE_URL,
        target_offset=240
    )

    print("\n=== FINAL DAY 1 ITINERARY ===")
    for item in updated_timeline:
        if item['type'] == 'transport':
            print(f"  ↓ Walk {item['duration_minutes']} mins")
        elif item['type'] == 'meal':
            print(f"[{item['offset']}m] {item['unit_name']} ({item['duration_minutes']} mins)")
        else:
            print(f"[{item['offset']}m] {item['unit_name']} ({item['duration_minutes']} mins)")

    print(f"\nDay 1 total duration: {final_day_duration} minutes (~{final_day_duration/60:.1f} hours)")