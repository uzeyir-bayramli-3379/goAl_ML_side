"""Load baku_amenities_places.csv (Google Places source) into Supabase.

Replaces amenities_loading.py (OSM source). Same table shape with place_id
replacing osm_id, and the known idempotency bug FIXED: place_id is UNIQUE and
the insert uses ON CONFLICT (place_id) DO NOTHING, so double-runs can't
duplicate rows (the OSM loader ran twice and silently produced exact 2x rows).

DATABASE_URL must be the Supabase SESSION POOLER URL
(...pooler.supabase.com, user postgres.<project-ref>) — the direct connection
is IPv6-only and fails on this network.

The existing `amenities` table has the OSM column set; this loader targets
the same table name with the new Places schema. Run with --recreate ONCE to
drop the OSM-shaped table and create the new one (destructive — the OSM data
is superseded, but the flag makes that an explicit choice, not a side effect).

Run:
    python amenities_loading_places.py --recreate   # first run: drop old OSM table, create new
    python amenities_loading_places.py              # subsequent runs: idempotent upsert
"""

import argparse
import csv
import os
import sys

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

load_dotenv()

CSV_FILE = "baku_amenities_places.csv"

DDL = """
CREATE TABLE IF NOT EXISTS amenities (
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


def main(recreate=False):
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()

    if recreate:
        answer = input("--recreate will DROP TABLE amenities (OSM data lost). Proceed? [y/N] ")
        if answer.strip().lower() != "y":
            print("Aborted.")
            sys.exit(0)
        cur.execute("DROP TABLE IF EXISTS amenities")
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
        INSERT INTO amenities (
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
        UPDATE amenities
        SET geom = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography
        WHERE geom IS NULL AND longitude IS NOT NULL AND latitude IS NOT NULL
    """)
    conn.commit()

    # ---------- 3. GIST INDEX ----------
    cur.execute("""
        CREATE INDEX IF NOT EXISTS amenities_geom_gist
        ON amenities USING GIST (geom)
    """)
    conn.commit()
    print("Geom built and GIST index ready")

    # ---------- 4. SANITY CHECK ----------
    cur.execute("SELECT category, COUNT(*) FROM amenities GROUP BY category ORDER BY 2 DESC")
    print("\nPer-category counts in DB:")
    for category, count in cur.fetchall():
        print(f"  {category or 'NULL':15s} {count}")
    cur.execute("SELECT COUNT(*) FROM amenities")
    total = cur.fetchone()[0]
    print(f"\nDB total: {total}  vs CSV rows: {len(rows)}"
          + ("  (MISMATCH — investigate)" if total != len(rows) else "  (match)"))

    cur.close()
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Load Google Places amenities into Supabase.")
    ap.add_argument("--recreate", action="store_true",
                    help="DROP the existing (OSM-shaped) amenities table and recreate")
    args = ap.parse_args()
    main(recreate=args.recreate)
