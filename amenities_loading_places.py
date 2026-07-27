"""Load baku_amenities_places.csv (Google Places source) into Supabase.

Replaces amenities_loading.py (OSM source). Same table shape with place_id
replacing osm_id, and the known idempotency bug FIXED: place_id is UNIQUE and
the insert uses ON CONFLICT (place_id) DO NOTHING, so double-runs can't
duplicate rows (the OSM loader ran twice and silently produced exact 2x rows).

DATABASE_URL must be the Supabase SESSION POOLER URL
(...pooler.supabase.com, user postgres.<project-ref>) — the direct connection
is IPv6-only and fails on this network.

Targets its OWN table, `amenities_places`. The OSM-shaped `amenities` table is
left alone on purpose: it is the comparison baseline for measuring how stale
the OSM data actually was. Nothing here drops anything.

Run:
    python amenities_loading_places.py    # idempotent — safe to re-run
"""

import csv
import os

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

load_dotenv()

CSV_FILE = "baku_amenities_places.csv"

DDL = """
CREATE TABLE IF NOT EXISTS amenities_places (
    id              BIGSERIAL PRIMARY KEY,
    place_id        TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    category        TEXT,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    opening_hours   JSONB,
    address         TEXT,
    business_status TEXT,
    fetched_at      DATE,
    geom            geography(Point, 4326)
)
"""


def main():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()

    cur.execute(DDL)
    conn.commit()

    # ---------- 1. LOAD ----------
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        for r in reader:
            rows.append((
                r["place_id"], r["name"], r["category"] or None,
                r["lat"] or None, r["lng"] or None,
                r["opening_hours"] or None, r["address"] or None,
                r["business_status"] or None, r["fetched_at"] or None,
            ))

    execute_values(cur, """
        INSERT INTO amenities_places (
            place_id, name, category, latitude, longitude,
            opening_hours, address, business_status, fetched_at
        ) VALUES %s
        ON CONFLICT (place_id) DO NOTHING
    """, rows)
    inserted = cur.rowcount
    conn.commit()
    print(f"CSV rows: {len(rows)}, inserted: {inserted}, "
          f"skipped as duplicates: {len(rows) - inserted}")

    # ---------- 2. BUILD GEOM ----------
    # longitude FIRST — ST_MakePoint takes (x=lng, y=lat).
    cur.execute("""
        UPDATE amenities_places
        SET geom = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography
        WHERE geom IS NULL AND longitude IS NOT NULL AND latitude IS NOT NULL
    """)
    conn.commit()

    # ---------- 3. GIST INDEX ----------
    cur.execute("""
        CREATE INDEX IF NOT EXISTS amenities_places_geom_gist
        ON amenities_places USING GIST (geom)
    """)
    conn.commit()
    print("Geom built and GIST index ready")

    # ---------- 4. SANITY CHECK ----------
    cur.execute("SELECT category, COUNT(*) FROM amenities_places GROUP BY category ORDER BY 2 DESC")
    print("\nPer-category counts in DB:")
    for category, count in cur.fetchall():
        print(f"  {category or 'NULL':15s} {count}")
    cur.execute("SELECT COUNT(*) FROM amenities_places")
    total = cur.fetchone()[0]
    print(f"\nDB total: {total}  vs CSV rows: {len(rows)}"
          + ("  (MISMATCH — investigate)" if total != len(rows) else "  (match)"))

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
