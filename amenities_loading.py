import os
import csv
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

load_dotenv()

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()

# NOTE: amenities.id is a surrogate BIGSERIAL PK, so there is NO natural key to
# dedup on — this INSERT has no ON CONFLICT. Running it twice DOUBLES the rows.
# Run it exactly ONCE, or TRUNCATE first if you need to reload:
#     cur.execute("TRUNCATE amenities RESTART IDENTITY")


# ---------- 1. LOAD AMENITIES ----------
with open("baku_amenities_clean.csv", newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    rows = []
    for r in reader:
        rows.append((
            r["OSM_ID"] or None, r["OSM_Type"] or None, r["Name"] or None,
            r["Latitude"] or None, r["Longitude"] or None,
            r["Category"] or None, r["cuisine"] or None,
            r["opening_hours"] or None, r["phone"] or None,
            r["website"] or None, r["wheelchair"] or None,
            r["outdoor_seating"] or None, r["smoking"] or None,
            r["addr_street"] or None, r["addr_housenumber"] or None,
            r["coord_group"] or None,
        ))

execute_values(cur, """
    INSERT INTO amenities (
        osm_id, osm_type, name, latitude, longitude, category, cuisine,
        opening_hours, phone, website, wheelchair, outdoor_seating, smoking,
        addr_street, addr_housenumber, coord_group
    ) VALUES %s
""", rows)
conn.commit()
print(f"Loaded {len(rows)} amenities")

# ---------- 2. BUILD GEOM ----------
# longitude FIRST — ST_MakePoint takes (x=lng, y=lat).
cur.execute("""
    UPDATE amenities
    SET geom = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography
    WHERE geom IS NULL AND longitude IS NOT NULL AND latitude IS NOT NULL
""")
conn.commit()

# ---------- 3. GIST INDEX ON GEOM ----------
# Makes ST_DWithin geo filtering fast at retrieval time. IF NOT EXISTS so a
# re-run is harmless.
cur.execute("""
    CREATE INDEX IF NOT EXISTS amenities_geom_gist
    ON amenities USING GIST (geom)
""")
conn.commit()
print("Geom built and GIST index ready")

cur.execute("""
    UPDATE amenities
    SET geom = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography
    WHERE geom IS NULL AND longitude IS NOT NULL AND latitude IS NOT NULL
""")
conn.commit()

cur.close()
conn.close()


