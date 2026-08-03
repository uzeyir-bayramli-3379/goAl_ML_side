# Handoff — grouping agent + landmark rerun prep (2026-08-03)

## Goal

Two threads, both about grounding landmark data for the GoAI Baku guide:

1. **Visit-unit grouping agent** — a Gemini agent that collapses the ~100 landmark
   entities within a walkable radius of an Old City anchor into "visit units" (one
   unit per thing a visitor actually stops at), tiered primary/secondary/ambient by
   a hard quota, with durations. Lives in `planning/`.
2. **Landmark data rerun** — the Supabase `landmarks` table schema changed (an
   `existence_status` column was added, e.g. `'gone'` for demolished sites), so the
   get→clean→enrich pipeline must be rerun and the serving layer must stop returning
   gone landmarks.

## Current state of the code

**Grouping agent (`planning/`) — working, NOT yet run against the API.**
- `planning/planning_prompt_v1.py` — system prompt. Tiering is a hard-quota RANKING
  task (top 6 primary, next 12 secondary, rest ambient; ≤2 per shared category across
  primary+secondary). The `existence_doubt` field + EXISTENCE block were **removed**
  this session (it produced false positives; `existence_status` from Wikidata answers
  this better). Per-unit output schema is now: `unit_name`, `wikidata_ids`, `tier`,
  `duration_minutes`.
- `planning/plan_units.py` — single-shot agent. Reads
  `planning/Supabase Snippet Untitled query.csv` (cols: wikidata_id, name,
  primary_class, sitelinks, meters_away), sends the whole set to `gemini-2.5-flash`,
  validates every input id lands in exactly one unit and none are invented, writes
  `visit_units.json`. Config: `temperature=0`, `MAX_OUTPUT_TOKENS=16000`,
  `THINKING_BUDGET=4096`, streaming on (tokens print live), JSON mime type. Flags
  (mutually exclusive): `--limit N`, `--thinnest N`/`--fattest N` (by sitelinks),
  `--ids Q... Q...`, plus `--dry-run`. Anchor hardcoded `(49.8355, 40.3663)` (lon,lat).

**Landmark rerun prep.**
- `landmark_rerun/` — working COPIES of `data_getting.py`, `enrich_landmarks.py`,
  `clean_landmarks.py` (all three together so cross-imports resolve). **Alter these
  copies, not the originals.** Not yet modified for the schema change.
- `retrieval_of_top_l.py` — added `AND existence_status IS DISTINCT FROM 'gone'` to the
  single retrieval WHERE clause (~line 42). Used `IS DISTINCT FROM`, not `!= 'gone'`,
  so NULL-status rows are kept rather than silently dropped.

## Files actively being edited
- `planning/plan_units.py`, `planning/planning_prompt_v1.py` — done for now, ready.
- `retrieval_of_top_l.py` — existence filter added.
- `landmark_rerun/{data_getting,enrich_landmarks,clean_landmarks}.py` — copies staged,
  NOT yet edited for the schema change.

## What was tried and failed
- **First grouping run truncated at ~1,463 chars** mid-array; `json.loads` raised
  `Expecting value`. Cause: `gemini-2.5-flash` thinks by default and thinking tokens
  count against `max_output_tokens`; unbounded thinking ate ~15.6k of 16k, so
  `finish_reason=MAX_TOKENS` cut the JSON short. Raising the cap did NOT help.
  **Fixed** with `ThinkingConfig(thinking_budget=4096)` + an explicit truncation error.
- **`existence_status != 'gone'` (the literal ask) is a NULL trap** — `NULL != 'gone'`
  is not true in SQL, so it drops every unset-status landmark. Switched to
  `IS DISTINCT FROM`. Confirm the schema's NULL semantics before assuming either.

## Constraints (do not forget)
- **NEVER run against the Gemini API** — user's key is capped ~20 calls/day and THEY
  do the real prompting. `--dry-run` / `py_compile` / import checks only. Same for the
  Places run and any DB write.
- Windows console is cp1252 → prefix dry-runs with `PYTHONIOENCODING=utf-8` or
  Azerbaijani names (ə/ç) crash bare prints. Real path writes UTF-8 files, unaffected.
- Use the conda env python: `~/anaconda3/envs/goAI_env/python.exe`.

## Next step I'd take
1. **The grouping input CSV still contains gone landmarks.** It comes from a manual
   Supabase snippet (not a repo file), so Alexander Nevsky (Q1817541) and Church of the
   Holy Virgin (Q4505131) — both demolished, both wrongly reached *secondary* last run —
   are still in `planning/Supabase Snippet Untitled query.csv`. Add
   `existence_status IS DISTINCT FROM 'gone'` to that snippet and regenerate the CSV.
   **Those two vanishing from the input is the proof the whole change worked.**
2. Edit the `landmark_rerun/` copies for whatever the schema change requires
   (populate/emit `existence_status`); user reruns the pipeline and reloads.
3. Re-run the grouping (user-driven) on the cleaned input; confirm the two churches are
   gone and the quotas hold.

---

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
