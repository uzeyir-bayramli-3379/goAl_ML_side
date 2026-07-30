# Handoff — Places loader + ground-truth cross-tab (2026-07-29)

## What this task was
Two independent deliverables against the Google Places amenity data:
- **Part A** — emit a SQL loader for `baku_amenities_places.csv` (do NOT run it, no DB access).
- **Part B** — CSV-only cross-tab of `ground_truth_50.csv` (50 hand-checked Icherisheher
  places) against Places vs the OSM baseline. No fetches, no DB.

Stop condition (honored): files/report produced, `load_places.sql` **not run**, DB untouched,
OSM `amenities`/`baku_amenities_clean.csv` untouched.

## Files produced
| File | Purpose | State |
|---|---|---|
| `load_places.sql` | `CREATE TABLE IF NOT EXISTS amenities_places` (+ geom, coverage, GIST), staged `\copy` then `INSERT ... ON CONFLICT (place_id) DO NOTHING`, geom **longitude-first**. Idempotent. | Ready to run — **not run yet** |
| `LOAD_INSTRUCTIONS.md` | Exact `psql "$DATABASE_URL" -f load_places.sql` steps (session-pooler note, verify query) | — |
| `groundtruth_crosstab.py` | Part B matcher (name-sim + proximity). Reads only. Regenerates the report. | Working |
| `places_vs_osm_groundtruth.md` | The cross-tab report | Current |

Input `ground_truth_50.csv` (do not modify) has columns: row, Name, Lat, Lng, Category, dist_m,
verdict, current_name. Verdicts: operational, closed, unverifiable, chain, **renamed**.

## How to run Part B
```
PYTHONIOENCODING=utf-8 ~/anaconda3/envs/goAI_env/python.exe groundtruth_crosstab.py
```
Prints the report and rewrites `places_vs_osm_groundtruth.md`. `unreliable` coverage rows and
`chain` ground-truth rows are excluded from scoring, per spec.

## Matching rules (as implemented)
- Confident match = within **50 m** AND name ratio (difflib on diacritic/emoji-stripped names)
  **>= 0.60**. Ambiguous (flagged, not counted) = good name at 50-80 m, or close but weak name.
- `renamed` rows: match on **proximity only** (old Name is stale), then among all venues within
  50 m pick the one whose name is closest to `current_name` (NOT merely the nearest — dense
  clusters otherwise pin the wrong neighbour). Rejected nearest is shown when it differs.
- Kept as a visible **fourth bucket**; never folded into operational/closed.

## Headline findings
- **Staleness (the point):** of 6 `closed` places, Places still lists **0**, OSM lists **6/6**.
- **OSM baseline is near-tautological** — the 50 GT rows were sampled *from* OSM, so it "matches"
  everything at 0 m. The signal is *what* it keeps (the closed venues), not the match count.
- **operational:** Places confident-match 13/22 (all `partial` coverage), 4 ambiguous, 5 missed.
- **unverifiable:** 0/12 turned up in Places even with the fuller grid (no change from spot check).
- **renamed:** 7/7 spots covered; current_name matches for Terrace 145, Manqal Old, Salam Baku
  Restaurant (Buta), White Fountain Park; Çay Bağı→"Tea Garden 145" is a translation; **West End
  and Rast are genuine name gaps** (renamed venue not in Places under its current name).

## Hand-verification notes (from user, not the pipeline)
- **Buta → "Salam Baku"** confirmed: present in Places as `Salam Baku Restaurant` at 18 m,
  OPERATIONAL. (This drove the renamed matcher fix from nearest-neighbour to best-current_name.)

## Open items / next steps (not started)
- **Load the DB:** user runs `load_places.sql` themselves, then reviews before any indexing/tuning.
- **Two park misses** (Khan's Garden, Azim Azimzadeh Park) are inside coarse-tile coverage (618/677 m
  << 1800 m radius) — NOT a grid gap. `includedTypes=park` Nearby Search doesn't surface enclosed
  old-city gardens. Would need a `tourist_attraction` type or a text query, not a finer grid.
- **3 restaurant/cafe operational misses** (Best Place, bürc qala, Art Café Mayak-13) sit in the
  fine-grid zone — genuine partial-collection gaps that further refinement passes could recover.
- **West End / Rast** renamed rows: hand-verify whether truly absent or renamed beyond fuzzy reach.
- Ambiguous matches (8 total, listed in the report) are left for manual resolution rather than
  building alias/translation matching.
