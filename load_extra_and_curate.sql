-- load_extra_and_curate.sql
-- Adds the amenity curation columns, loads baku_amenities_extra.csv, and marks
-- the hand-reviewed rows as schedulable stops.
--
-- STEPS 1, 2 and 4 run together here:
--     psql "$DATABASE_URL" -f load_extra_and_curate.sql
-- from the directory containing baku_amenities_extra.csv (the \copy path is
-- relative, same as LOAD_INSTRUCTIONS.md).
--
-- STEP 3 (the duplicate checks) is at the bottom, commented out — run those by
-- hand in the Supabase editor AFTER this file completes and eyeball the output.
--
-- Nothing is ever deleted. Junk rows load with stop_eligible = false (the
-- default) and stay in the table as the labelled eval set for the classifier
-- that will replace this hand pass.

BEGIN;

-- ============================================================ 1. SCHEMA

-- Can this row fill a STOP slot in an itinerary? Not the same question as
-- "is this row real" or "can it fill a MEAL slot". Restaurants are real and
-- fill meal slots but are never stops; a roofing supplier is neither.
-- Default false: nothing is schedulable until something asserts it is, so a
-- bad Places type can never leak into a plan by accident.
ALTER TABLE amenities_places ADD COLUMN IF NOT EXISTS
    stop_eligible boolean NOT NULL DEFAULT false;

-- Who decided. 'auto' = never reviewed, 'hand' = a human looked at this row,
-- 'llm' = a classifier decided. Lets you tell "reviewed and rejected" from
-- "never looked at", which is the difference between an eval label and a gap.
ALTER TABLE amenities_places ADD COLUMN IF NOT EXISTS
    curation_source text NOT NULL DEFAULT 'auto';

-- Same physical place, second Places entry (old name, translated name, or a
-- tenant listed at the venue's coordinates). Points at the row we keep.
ALTER TABLE amenities_places ADD COLUMN IF NOT EXISTS
    duplicate_of text REFERENCES amenities_places(place_id);

-- Already in `landmarks` under a Wikidata QID. The packer schedules these from
-- landmarks, never from here, so they must stay stop_eligible = false.
ALTER TABLE amenities_places ADD COLUMN IF NOT EXISTS
    is_landmark boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS amenities_places_stop_eligible_idx
    ON amenities_places (stop_eligible) WHERE stop_eligible;

-- ============================================================ 2. LOAD

CREATE TEMP TABLE _extra_staging (
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

\copy _extra_staging (place_id,name,category,lat,lng,opening_hours,address,business_status,fetched_at,coverage) FROM 'baku_amenities_extra.csv' WITH (FORMAT csv, HEADER true)

INSERT INTO amenities_places
    (place_id, name, category, lat, lng, opening_hours, address,
     business_status, fetched_at, coverage, geom)
SELECT
    s.place_id, s.name, s.category, s.lat, s.lng,
    NULLIF(s.opening_hours, '')::jsonb,
    s.address, s.business_status, s.fetched_at, s.coverage,
    ST_SetSRID(ST_MakePoint(s.lng, s.lat), 4326)::geography   -- LONGITUDE FIRST
FROM _extra_staging s
WHERE s.place_id IS NOT NULL
ON CONFLICT (place_id) DO NOTHING;

-- ============================================================ 3. CURATE

-- 3a. Malls. Eight survivors of ~130 shopping_mall rows: Google applies that
-- type to any retail premises, so the category is mostly car dealerships,
-- furniture shops and one traffic police station. Park Bulvar is deliberately
-- absent — it is Q187512 in `landmarks` already.
UPDATE amenities_places
SET stop_eligible = true, curation_source = 'hand'
WHERE place_id IN (
    'ChIJM2EO0P99MEARjDz4fIKDdBM',  -- Dəniz Mall
    'ChIJL0BroKd9MEAREWHDnvd8idg',  -- 28 Mall
    'ChIJM-DJn-Z9MEARWSc0gkxaSkE',  -- Nizami Mall
    'ChIJV113dcN9MEARm5dyfjPM2Tk',  -- Crescent Mall
    'ChIJucXkuHF9MEARUrJFVhx4UP4',  -- Port Baku Mall
    'ChIJz14idRJ9MEARfZ3HlTJ83uM',  -- Şərq Bazarı
    'ChIJIQzM2Il9MEARMXazMi9IJ8Y',  -- Baku Mall
    'ChIJBRAsWl19MEARVMyMQcEc3l4'   -- Ganjlik Mall
);

-- 3b. Attractions with no Wikidata entity. This is what the tourist_attraction
-- experiment was for: parks, squares, viewpoints and modern sights the
-- Wikidata-only landmark corpus does not cover.
-- REVIEW THIS LIST BEFORE RUNNING — local knowledge beats my guess on several.
UPDATE amenities_places
SET stop_eligible = true, curation_source = 'hand'
WHERE place_id IN (
    'ChIJnZLDhC18MEARRi2mrQG3saY',  -- Highland Park
    'ChIJd2b2mTR8MEARl5incNuFjQE',  -- Mini Venice
    'ChIJY94vLct9MEART--XQFdWh1g',  -- Azneft Square
    'ChIJpW8LFjB8MEARisk62ZfvFug',  -- Baku Eye
    'ChIJ-dKtrst9MEARJtAqWE8ATlo',  -- Philarmonia Garden
    'ChIJy3-srrF9MEARcp2puDCCOr0',  -- Khagani Garden
    'ChIJ98qpbeR9MEAR7pUucjGR2r0',  -- Swans Fountain
    'ChIJSQWpqbN9MEAREX0-W2PKtlY',  -- Saat Qülləsi
    'ChIJKQKgVKd-MEAR9hvsYR99X9g',  -- Stone Chronicle Museum
    'ChIJaan1JRV9MEAR_yVNSDuZ9sY',  -- Təhsil Muzeyi
    'ChIJiSV7_uB8MEARJEI3c2JUYbg',  -- Baku Nobel Heritage Fund
    'ChIJhfA1HAh9MEAR-4IQc1WnBSQ',  -- Bakı Buxtası
    'ChIJ1_KGcwB9MEAR8KDjK9cOkcI',  -- Amateur Fishermen Bridge
    'ChIJHSiQIVB9MEARM3fSlC0IqSY',  -- Caspian Sea Cruise
    'ChIJHZqQfAB9MEAR4rye9zw8g78',  -- Bakı Gəmi turu
    'ChIJ___zysF9MEAR_hRHz9XaCZI',  -- Huseyn Cavid Park
    'ChIJc-oDUp59MEARq01FLsOpR5k',  -- Officers Park
    'ChIJ7QCI6qR9MEARwPzYHx9cfHc',  -- Füzuli Park
    'ChIJ5VZuGW59MEARoIIEoKtCyXE',  -- Dədə Qorqud Park
    'ChIJ3_92k119MEARZinsSljeirY',  -- Atatürk Parkı
    'ChIJ_xhPH3d9MEAR7Kudk-Nd7V4',  -- Zorge Park
    'ChIJGXx1f39-MEARSJad4oy23Wc',  -- Mərkəzi Nəbatat Bağı
    'ChIJw22GPAB9MEARM5WS3jDN9ek',  -- White City Bridge
    'ChIJqUUJPQB9MEAR-bIk0QOqrkc',  -- Fəvvarələr Meydanı, Ağ Şəhər
    'ChIJdeoFlTV9MEAR5VfUXpCKe00',  -- Mosaic Wall
    'ChIJFbkgRLh9MEARsVrI6h8iGfg',  -- L and M viewpoint
    'ChIJ_bgGs619MEAR6KIB8sMb3nA',  -- Baku view point
    'ChIJI0GBbgB_MEARwt-T6B2_TuU',  -- Panoramic Baku Bibi Heybat
    'ChIJi8b8DFl9MEARMjnsTK5l_cU',  -- I Love Baku sign
    'ChIJpRTscAB9MEAR7YImFGg-osg',  -- 2015 European Games Flame
    'ChIJJydeE4R9MEARrpLW5nSo3iU'   -- Russian Orthodox Church
);

-- 3c. Duplicates. Same venue, second entry — kept for audit, never scheduled.
UPDATE amenities_places SET duplicate_of = 'ChIJM2EO0P99MEARjDz4fIKDdBM',
       curation_source = 'hand'
WHERE place_id = 'ChIJKVx_XzF8MEARUTbSpvngBTE';   -- Caspian Waterfront = Dəniz Mall

UPDATE amenities_places SET duplicate_of = 'ChIJw22GPAB9MEARM5WS3jDN9ek',
       curation_source = 'hand'
WHERE place_id = 'ChIJa9B0LAB9MEARXt_l7AU-J4M';   -- Ağ şəhər körpü = White City Bridge

UPDATE amenities_places SET duplicate_of = 'ChIJ9f311W99MEARdmLcywxEo70',
       curation_source = 'hand'
WHERE place_id = 'ChIJ3eHEG-h9MEAR3U3heDu0Qds';   -- Zefir Mall Baku = ZEFİR MALL

-- 3d. Already landmarks. Flagged, not deleted: the Places row carries a real
-- street address the Wikidata row often lacks, so it is worth keeping as a
-- cross-reference. Matched by proximity — names differ across languages
-- ("Təzəpir məscidi" vs "Taza Pir Mosque") but coordinates do not.
UPDATE amenities_places a
SET is_landmark = true, curation_source = 'hand'
FROM landmarks_live l
WHERE ST_DWithin(a.geom, l.geom, 60)
  AND a.category = 'tourist_attraction'
  AND a.coverage LIKE 'extra%';

-- Belt and braces: a landmark row must never be schedulable from here.
UPDATE amenities_places SET stop_eligible = false WHERE is_landmark;

-- Everything else in the extra load was reviewed and rejected. Recording that
-- explicitly is what makes this a labelled set rather than an unexamined pile.
UPDATE amenities_places
SET curation_source = 'hand'
WHERE coverage LIKE 'extra%' AND curation_source = 'auto';

COMMIT;

-- ============================================================ 4. VERIFY
-- Run these afterwards and read the output.

-- Counts by category and flag. Expect ~8 malls + ~31 attractions eligible.
--   SELECT category, stop_eligible, is_landmark, COUNT(*)
--   FROM amenities_places WHERE coverage LIKE 'extra%'
--   GROUP BY 1,2,3 ORDER BY 1,2,3;

-- Which rows matched a landmark. Sanity-check the 60m threshold did not
-- over-match: two different things 50m apart would be a false positive.
--   SELECT a.name AS amenity, l.name AS landmark,
--          ST_Distance(a.geom, l.geom) AS m
--   FROM amenities_places a JOIN landmarks_live l ON ST_DWithin(a.geom, l.geom, 60)
--   WHERE a.is_landmark ORDER BY m DESC;

-- Remaining duplicate pairs among schedulable rows. a.place_id < b.place_id
-- stops each pair appearing twice.
--   SELECT a.name, b.name, ST_Distance(a.geom, b.geom) AS m
--   FROM amenities_places a
--   JOIN amenities_places b ON a.place_id < b.place_id
--                          AND ST_DWithin(a.geom, b.geom, 75)
--   WHERE a.stop_eligible AND b.stop_eligible
--   ORDER BY m;

-- The eval set for the future classifier: 39 positives, ~174 negatives.
--   SELECT stop_eligible, COUNT(*) FROM amenities_places
--   WHERE curation_source = 'hand' GROUP BY 1;
