import psycopg2
import json
from pathlib import Path
import os
from dotenv import load_dotenv
from pair_matrix_checker import get_unit_centroid, get_db_coordinates

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
ENV_PATH = ROOT_DIR / ".env"

MEAL_CATEGORIES = ['restaurant', 'cafe', 'food', 'fast_food']
MEAL_DURATION = 75

# Per-city transit lookup table (Baku)
BAKU_TRANSIT_TABLE = {
    tuple(sorted(["Icherisheher", "Flame Towers"])): {
        "mode": "funicular",
        "duration_minutes": 20,
        "notes": "Funicular or 20 min walk uphill (~1.0 km)"
    },
    tuple(sorted(["Icherisheher", "Opera and Ballet"])): {
        "mode": "walk",
        "duration_minutes": 15,
        "notes": "Walk (~1.3 km)"
    },
    tuple(sorted(["Icherisheher", "Heydar Aliyev Center"])): {
        "mode": "metro_walk",
        "duration_minutes": 30,
        "notes": "Metro + Walk (~4.2 km, ~30 min)"
    }
}


# --- STAGE 5: MEAL INJECTION HELPERS ---

def fetch_nearest_meal_amenity(mid_lat, mid_lng, db_url):
    """Queries PostGIS for the single nearest food amenity."""
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
    """Stage 5: Injects a lunch slot at the boundary closest to midday (~240m)."""
    stop_indices = [idx for idx, item in enumerate(timeline) if item['type'] == 'stop']
    if len(stop_indices) < 2:
        return timeline, timeline[-1]['offset'] + timeline[-1]['duration_minutes']

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
            best_boundary_idx = idx_prev + 2 if (idx_prev + 1 < len(timeline) and timeline[idx_prev + 1]['type'] == 'transport') else idx_prev + 1
            prev_stop_name = item_prev['unit_name']
            next_stop_name = timeline[stop_indices[k+1]]['unit_name']

    c1 = centroids.get(prev_stop_name, (0.0, 0.0))
    c2 = centroids.get(next_stop_name, (0.0, 0.0))

    valid_lats = [c[0] for c in [c1, c2] if c[0] != 0.0]
    valid_lngs = [c[1] for c in [c1, c2] if c[1] != 0.0]

    if not valid_lats:
        return timeline, timeline[-1]['offset'] + timeline[-1]['duration_minutes']

    mid_lat = sum(valid_lats) / len(valid_lats)
    mid_lng = sum(valid_lngs) / len(valid_lngs)

    meal = fetch_nearest_meal_amenity(mid_lat, mid_lng, db_url)
    meal_label = f"Lunch near {prev_stop_name} ({meal['name']})" if meal else f"Lunch near {prev_stop_name}"

    insert_offset = timeline[best_boundary_idx - 1]['offset'] + timeline[best_boundary_idx - 1]['duration_minutes']

    meal_item = {
        "type": "meal",
        "unit_name": meal_label,
        "offset": insert_offset,
        "duration_minutes": MEAL_DURATION
    }

    timeline.insert(best_boundary_idx, meal_item)
    for i in range(best_boundary_idx + 1, len(timeline)):
        timeline[i]['offset'] += MEAL_DURATION

    final_offset = timeline[-1]['offset'] + timeline[-1]['duration_minutes']
    return timeline, final_offset


# --- STAGE 6: TRANSPORT HOPS ---

def apply_transport_hops(timeline, start_offset=0):
    """Stage 6: Replaces local walks with inter-anchor transit where applicable."""
    refined_timeline = []
    current_time = start_offset

    for i, item in enumerate(timeline):
        if item['type'] == 'transport':
            prev_stop = timeline[i - 1] if i > 0 else None
            next_stop = timeline[i + 1] if i + 1 < len(timeline) else None

            anchor_a = prev_stop.get('anchor', prev_stop.get('unit_name')) if prev_stop else None
            anchor_b = next_stop.get('anchor', next_stop.get('unit_name')) if next_stop else None

            lookup_key = tuple(sorted([anchor_a, anchor_b])) if (anchor_a and anchor_b and anchor_a != anchor_b) else None
            
            if lookup_key:
                anchor_hop = BAKU_TRANSIT_TABLE.get(lookup_key)
                if not anchor_hop:
                    raise ValueError(
                        f"Missing BAKU_TRANSIT_TABLE entry for cross-anchor hop: "
                        f"'{anchor_a}' <-> '{anchor_b}'. Missing config."
                    )
                
                updated_item = {
                    "type": "transport",
                    "mode": anchor_hop["mode"],
                    "duration_minutes": anchor_hop["duration_minutes"],
                    "offset": round(current_time),
                    "notes": anchor_hop["notes"],
                    "inter_anchor": True
                }
            else:
                # Standard walk inside an anchor
                updated_item = dict(item)
                updated_item['offset'] = round(current_time)
                updated_item['notes'] = f"Walk {item['duration_minutes']} mins"
                updated_item['inter_anchor'] = False

            refined_timeline.append(updated_item)
            current_time += updated_item['duration_minutes']
        else:
            item_copy = dict(item)
            item_copy['offset'] = round(current_time)
            refined_timeline.append(item_copy)
            current_time += item_copy['duration_minutes']

    final_duration = round(current_time)
    return refined_timeline, final_duration


# --- MASTER MULTI-DAY PIPELINE ---

def process_multi_day_itinerary(raw_multi_day_timelines, db_url):
    """
    Executes Stage 5 (Meals) and Stage 6 (Hops) sequentially for every day in the plan.
    """
    final_itinerary = {}

    for day_num, day_timeline in raw_multi_day_timelines.items():
        print(f"\nProcessing Day {day_num}...")

        # 1. Fetch WIDs & Centroids for this specific day
        stop_items = [u for u in day_timeline if u.get('type') == 'stop']
        all_wids = list(set(wid for u in stop_items for wid in u.get('wikidata_ids', [])))
        db_coords = get_db_coordinates(all_wids, db_url)
        centroids = {u['unit_name']: get_unit_centroid(u, db_coords) for u in stop_items}

        # 2. Stage 5: Inject Meal Slot
        timeline_with_meal, _ = insert_meal_slot(
            timeline=day_timeline,
            centroids=centroids,
            db_url=db_url,
            target_offset=240
        )

        # 3. Stage 6: Apply Transport Hops & Fix Offsets
        final_day_timeline, total_duration = apply_transport_hops(
            timeline=timeline_with_meal,
            start_offset=0
        )

        # --- NEW DEFENSIVE ASSERTIONS ---
        inter_anchor_hops = [item for item in final_day_timeline if item.get('inter_anchor')]
        if len(inter_anchor_hops) > 1:
            raise AssertionError(f"Day {day_num} exceeds 1-hop-per-day ceiling: {len(inter_anchor_hops)} hops found.")
            
        sum_durations = sum(item.get('duration_minutes', 0) for item in final_day_timeline)
        if final_day_timeline:
            final_item = final_day_timeline[-1]
            final_expected = final_item['offset'] + final_item['duration_minutes']
            if sum_durations != final_expected:
                raise AssertionError(f"Timeline mismatch on Day {day_num}: sum({sum_durations}) != final({final_expected})")
        
        if str(day_num) == '1' and final_day_timeline:
            first_item = final_day_timeline[0]
            if first_item['type'] == 'transport' and first_item.get('duration_minutes', 0) > 25:
                print(f"[WARNING] anchor choice for day 1 may be wrong: initial transport is {first_item['duration_minutes']} min.")
        # --------------------------------

        final_itinerary[f"day_{day_num}"] = {
            "timeline": final_day_timeline,
            "total_duration_minutes": total_duration
        }

    return final_itinerary


if __name__ == "__main__":
    load_dotenv(ENV_PATH)
    DATABASE_URL = os.environ.get("DATABASE_URL")

    # Load multi-day timeline payload (e.g. {"1": [...], "2": [...], "3": [...]})
    with open('multi_day_raw_timelines.json', 'r', encoding='utf-8') as f:
        raw_timelines = json.load(f)

    print("\n==========================================")
    print("  RUNNING MULTI-DAY STAGE 5 & 6 ENGINE    ")
    print("==========================================")

    full_itinerary = process_multi_day_itinerary(raw_timelines, DATABASE_URL)

    # Output formatted multi-day itinerary
    for day_key, day_data in full_itinerary.items():
        print(f"\n=== {day_key.upper()} (Total: {day_data['total_duration_minutes']} mins / ~{day_data['total_duration_minutes']/60:.1f} hrs) ===")
        for item in day_data['timeline']:
            offset = item['offset']
            dur = item['duration_minutes']
            t_type = item['type']

            # Safely fall back to 'name' or a default string if neither exists
            display_name = item.get('unit_name') or item.get('name') or "Transit/Break"
            print(f"[{offset}m] {t_type} {display_name} ({dur} mins)")

    # Save final enriched multi-day payload to disk
    with open('final_multi_day_itinerary.json', 'w', encoding='utf-8') as f:
        json.dump(full_itinerary, f, indent=2, ensure_ascii=False)
        print("\nSaved complete itinerary to 'final_multi_day_itinerary.json'.")