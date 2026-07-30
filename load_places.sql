-- load_places.sql
-- Loads baku_amenities_places.csv (Google Places API New) into amenities_places.
-- Idempotent and safe to re-run. Run with:  psql "$DATABASE_URL" -f load_places.sql
-- (see LOAD_INSTRUCTIONS.md). Requires PostGIS.
--
-- This file NEVER touches the OSM `amenities` table/baseline.

BEGIN;

CREATE EXTENSION IF NOT EXISTS postgis;

-- Target table. Columns mirror baku_amenities_places.csv, plus geom.
-- place_id is the natural unique key (only Places field storable indefinitely under ToS).
CREATE TABLE IF NOT EXISTS amenities_places (
    id              BIGSERIAL PRIMARY KEY,
    place_id        TEXT UNIQUE,
    name            TEXT,
    category        TEXT,
    lat             DOUBLE PRECISION,
    lng             DOUBLE PRECISION,
    opening_hours   JSONB,                       -- always NULL (Enterprise SKU field excluded from fetch)
    address         TEXT,
    business_status TEXT,
    fetched_at      DATE,
    coverage        TEXT,                         -- complete / partial / unreliable
    geom            geography(Point, 4326)
);

CREATE INDEX IF NOT EXISTS amenities_places_geom_gist ON amenities_places USING GIST (geom);

-- Stage the raw CSV, then upsert. \copy runs client-side (no server file access),
-- and staging lets us apply ON CONFLICT + build geom, which \copy alone cannot do.
-- ON COMMIT DROP cleans the temp table up automatically.
CREATE TEMP TABLE _places_staging (
    place_id        TEXT,
    name            TEXT,
    category        TEXT,
    lat             DOUBLE PRECISION,
    lng             DOUBLE PRECISION,
    opening_hours   TEXT,
    address         TEXT,
    business_status TEXT,
    fetched_at      DATE,
    coverage        TEXT
) ON COMMIT DROP;

-- Column list matches the CSV header exactly. Empty unquoted fields import as NULL.
\copy _places_staging (place_id,name,category,lat,lng,opening_hours,address,business_status,fetched_at,coverage) FROM 'baku_amenities_places.csv' WITH (FORMAT csv, HEADER true)

INSERT INTO amenities_places
    (place_id, name, category, lat, lng, opening_hours, address, business_status, fetched_at, coverage, geom)
SELECT
    s.place_id,
    s.name,
    s.category,
    s.lat,
    s.lng,
    NULLIF(s.opening_hours, '')::jsonb,                              -- always NULL in practice
    s.address,
    s.business_status,
    s.fetched_at,
    s.coverage,
    ST_SetSRID(ST_MakePoint(s.lng, s.lat), 4326)::geography          -- LONGITUDE FIRST
FROM _places_staging s
WHERE s.place_id IS NOT NULL
ON CONFLICT (place_id) DO NOTHING;

COMMIT;
