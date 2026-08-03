# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

The Machine Learning side of **GoAl / GoAI ("gAIde")**: an AI city-exploration guide for
foreigners, Baku launch, **English-only**. This repo owns the AI/ML track — grounding data
acquisition, a grounded retrieval loop, and the onboarding-time itinerary model — not the Flutter
client or Spring Boot backend.

The core principle everywhere is **grounding / anti-hallucination**: the LLM narrates, ranks, or
serves from verified place data, never invents. Two content types with opposite data shapes:

- **Landmarks** — sparse, fact-rich, *narrated* (Gemini writes a grounded paragraph). Source:
  Wikidata via SPARQL, plus Wikipedia lede extracts.
- **Amenities** (cafes/restaurants/leisure/malls) — dense, fact-poor, *served not narrated*
  (structured retrieval — category/distance, no generated blurb). Source: **Google Places API
  (New)**. OpenStreetMap/Overpass was the original source and is now **superseded** (see below).

On top of the data layer sits an **itinerary model** (see "Planning model" below): landmarks are
grouped into *anchors* (walkable clusters) and *visit units* (one per thing a visitor stops at),
tiered by hard quotas at onboarding time. The query-time *packer* that turns this inventory into a
timed day plan does not exist yet.

**Why the amenity source changed:** hand-validation of 50 OSM amenities near Icherisheher found
~20% confirmed permanently closed and ~25% unverifiable. OSM's Baku coverage is too stale to
ground answers on. Places (New) gives `businessStatus`, which makes staleness *measurable* instead
of invisible. The OSM pipeline and its CSV are kept as the **comparison baseline** for measuring
that miss rate — do not delete `baku_amenities_clean.csv`, it is the only copy.

There is no build system, package manifest, or test suite — these are standalone scripts run directly.

## Pipeline & files

Acquisition pipelines produce CSVs; a serving layer loads them into Postgres and runs retrieval;
a planning layer groups landmarks into visit units.

**Landmark acquisition (Wikidata):**
- `data_getting.py` — Stage 1 discovery SPARQL. `discover_landmarks() -> {QID: dict}`; `__main__`
  prints a DataFrame. Emits name/coords/category skeleton.
- `enrich_landmarks.py` — Stage 2. QID-keyed enrichment SPARQL (batched ~150, `VALUES ?item {…}`),
  per-QID JSON checkpoint (`enrichment_checkpoint.jsonl`, resumable), merges → `clean_landmarks()`
  → writes `baku_landmarks_clean.csv` + `baku_dropped.csv`. Optional `--wiki` stage 3 pulls
  Wikipedia intro extracts (`wiki_extracts*.jsonl`).
- `clean_landmarks.py` — filtering/dedup helper (sanity-box geo drop, core-zone flag, type tag).
  Imported by the enrichment stage.
- `landmark_rerun/` — working **COPIES** of `data_getting.py`, `enrich_landmarks.py`,
  `clean_landmarks.py` (all three together so cross-imports resolve). Staged for the schema-change
  rerun (populate/emit `existence_status`). **Edit these copies, not the originals.**

**Landmark narration (Gemini — offline batch, not wired into the serving path):**
- `prompts.py` — prompt text + string-assembly helpers for overview generation. Pure strings, no
  I/O, no client. Selects which Wikidata columns become grounding FACTS. Imported by
  `generate_overviews.py`; NOT by the backend.
- `generate_overviews.py` — one `gemini-2.5-flash` call per landmark → `{card_summary, narrative,
  retrieval}` → `landmark_overviews.csv`. Resumable via append-only JSONL checkpoint keyed on
  `Wikidata_ID:register` (the **register** system = named voice variants regenerated alongside each
  other; constants in `prompts.py`). Reads a CSV, writes a CSV — never touches the DB.

**Amenity acquisition (Google Places API New) — CURRENT:**
- `get_amenities_places.py` — tiled `places:searchNearby` / `:searchText` discovery over the
  `CORE_BOX`. Dual-resolution grid + reactive adaptive refinement of any tile that hits the 20-cap
  (see saturation gotcha). Append-only JSONL checkpoint (`baku_places_checkpoint.jsonl`) guarded by
  a **plan fingerprint** on line 1. Writes `baku_amenities_places.csv`. Flags: `--dry-run`,
  `--sample`, `--sample-tile N`, `--yes`. Full run = 812 requests (pass 0) plus refinement passes,
  real quota — never run it unprompted.
- `get_amenities_extra.py` — **one-off top-up** for categories added *after* the main census
  (`shopping_mall`, `tourist_attraction`), 20 coarse-grid requests → `baku_amenities_extra.csv`.
  Deliberately a **separate script**: `get_amenities_places.py`'s `plan_fingerprint()` hashes
  `NEARBY_QUERIES`, so appending one category would invalidate the checkpoint and force the whole
  812-task plan to be re-fetched and re-billed. Separate checkpoint
  `baku_places_extra_checkpoint.jsonl`.

**Amenity acquisition (OSM) — SUPERSEDED, kept as baseline:**
- `get_amenities.py` — Overpass discovery for cafes/restaurants/leisure POIs. Hardened HTTP layer.
  Writes `baku_amenities_clean.csv`. Still runnable, but not the source of truth. Its CSV is the
  comparison baseline for the Places-vs-OSM staleness analysis.

**Serving layer (Postgres + PostGIS + pgvector, on Supabase):**
- `postgresser.py` — landmark loader. `baku_landmarks_clean.csv` → `landmarks` table (`ON CONFLICT
  (wikidata_id) DO NOTHING`), builds `geom`, then embeds each row (`gemini-embedding-001` @ 768d,
  `RETRIEVAL_DOCUMENT`).
- `amenities_loading_places.py` — **current** amenity loader. `baku_amenities_places.csv` →
  `amenities_places` table, builds `geom`, GIST index. Idempotent: `place_id` UNIQUE + `ON CONFLICT
  (place_id) DO NOTHING`. Never touches the OSM table.
- `amenities_loading.py` — **superseded** OSM loader. `baku_amenities_clean.csv` → `amenities`.
  **No ON CONFLICT** — surrogate BIGSERIAL PK, so re-running duplicates rows (already ran twice =
  2× rows): run once, or `TRUNCATE amenities RESTART IDENTITY` before reloading.
- Neither amenity loader embeds anything (structured/geo layer only).
- `retrieval_of_top_l.py` — two-stage landmark retrieval + grounded generation: `ST_DWithin` geo
  filter → pgvector `<=>` cosine rerank (weighted `0.7·vec + 0.3·geo`) → Gemini `gemini-2.5-flash`
  answer clamped to context. WHERE clause carries `existence_status IS DISTINCT FROM 'gone'`.
- `load_places.sql` — psql loader equivalent for `amenities_places` (idempotent, longitude-first
  geom). `load_extra_and_curate.sql` — adds amenity curation columns, loads
  `baku_amenities_extra.csv`, marks hand-reviewed rows `stop_eligible = true` (duplicate checks at
  the bottom, commented, run by hand). `LOAD_INSTRUCTIONS.md` — exact `psql` steps + session-pooler
  note. **These are the only SQL files in-repo; the `cities`/`anchors`/new-column schema was applied
  directly in Supabase, not checked in as a `schema.sql`.**

**Planning / grouping agent (`planning/`) — onboarding-time, one Gemini call per anchor:**
- `planning/planning_prompt_v1.py` — system prompt. Tiering is a hard-quota RANKING task; per-run
  quotas + entity table go in the USER message (the system prompt holds literal `{}` braces and is
  never `.format()`-ed). Output schema per unit: `unit_name`, `wikidata_ids`, `tier`,
  `duration_minutes`.
- `planning/plan_units.py` — single-shot agent. Reads an entity CSV, splits off `stop_eligible=false`
  rows, computes quotas from the eligible count, calls `gemini-2.5-flash`, runs `enforce` (code-side
  rules the model ignores), validates the id partition, writes `visit_units.json`. Flags:
  `--in/--csv_name FILE`, `--out`, `--dry-run`, and mutually-exclusive subset modes `--limit`,
  `--thinnest`, `--fattest`, `--ids`. Test CSVs: `from_the_inside.csv` (Icherisheher),
  `opera_plan_test.csv`, `flame_plan_test.csv`, `heydar_plan_test.csv`.

**Ground-truth / analysis:**
- `groundtruth_crosstab.py` — matches `ground_truth_50.csv` (50 hand-checked Icherisheher places)
  against Places vs the OSM baseline by name-sim + proximity (~50m); writes
  `places_vs_osm_groundtruth.md`. Reads only.
- `ground_truth_50.csv` (+ `_template`) — do not modify. Verdicts: operational, closed,
  unverifiable, chain, renamed.

**Small check/util scripts (throwaway diagnostics, read-only unless noted):**
`unreliable_pct.py` (coverage %), `dist_check.py` / `landmark_checker.py` (wiki_extract length
stats), `no_enwiki.py` (rows missing enwiki title), `enrcich_check.py` / `fallback_checking.py` /
`fallback_2.py` (wiki-title / multilang extract coverage), `amenities_duper.py` (dedup
`baku_amenities_places.csv` → `_dedup.csv` by place_id), `extra_dropper.py` (builds
`baku_amenities_extra_clean.csv`: all `tourist_attraction` rows + hand-picked malls),
`groundtruth_crosstab.py` output review. `test.py` / `test2.py` are old prompt-grounding
prototypes (NVIDIA Gemma / Google Gemini), not wired in.

## Running

Scripts are run directly with Python. **Use the conda env python** — base `python` lacks
`requests`/`pandas`/`psycopg2` (conda is NOT on PATH):

```
~/anaconda3/envs/goAI_env/python.exe <script>.py
```

```
python data_getting.py                       # discovery SPARQL (prints DataFrame)
python enrich_landmarks.py                    # discovery -> enrich -> clean -> CSVs  (--wiki adds extracts)
python generate_overviews.py                  # per-landmark grounded overviews (Gemini, resumable)
python get_amenities_places.py --dry-run      # print the 812-request plan, no calls
python get_amenities_places.py --sample       # ONE real request, print parsed rows
python get_amenities_places.py                # FULL run — 812 requests, real quota
python get_amenities_extra.py                 # top-up fetch: malls + tourist_attraction (20 reqs)
python get_amenities.py                       # SUPERSEDED: Overpass amenity discovery -> CSV
python postgresser.py                         # load + geom + embed landmarks (idempotent)
python amenities_loading_places.py            # load + geom + GIST -> amenities_places (idempotent)
python amenities_loading.py                   # SUPERSEDED OSM loader -> amenities  (RUN ONCE — no dedup)
python retrieval_of_top_l.py                  # two-stage retrieval + grounded answer
python planning/plan_units.py --dry-run       # visit-unit grouping, no API call
python groundtruth_crosstab.py                # Places-vs-OSM cross-tab report (reads only)
```

On Windows the console is cp1252 → set `PYTHONIOENCODING=utf-8` when printing Azerbaijani text
(ə/ç chars crash bare prints). Real path writes UTF-8 files, unaffected.

Dependencies installed ad hoc (no requirements file): `requests`, `pandas`, `psycopg2`, `openai`,
`google-genai`, `python-dotenv`.

## Secrets

API keys live in `.env` (gitignored), loaded with `python-dotenv`. Expected keys:
- `GEMINI_API_KEY` — Google Gemini (embeddings + generation) in the serving/planning/overview
  layers and `test2.py`.
- `GEMMA_API_KEY` — NVIDIA integrate API (`integrate.api.nvidia.com`) in `test.py`.
- `GOOGLE_PLACES_API_KEY` — Google Places API (New) in `get_amenities_places.py` /
  `get_amenities_extra.py`.
- `DATABASE_URL` — Supabase Postgres connection string (use the **session pooler**, see gotchas).

## Database

**Supabase: Postgres + PostGIS + pgvector.** One table per source, plus `cities`/`anchors` for the
planning model. Connect via the **session pooler**, not the direct connection.

- `landmarks` — keyed on `wikidata_id`; `geom geography(Point,4326)`; `embedding vector(768)`;
  GIST(geom) + HNSW(embedding, `vector_cosine_ops`). Planning/eligibility columns: `city_id`,
  `anchor_id`, `existence_status` ('ok'|'flagged'|'gone'), `anchor_eligible`, `stop_eligible`,
  `wiki_extract`, `wiki_extract_lang`.
- `landmarks_live` — view over `landmarks` filtering `existence_status = 'gone'`. Existence is a
  *correctness* filter so it lives in the view; eligibility is stage-specific and stays an explicit
  WHERE at the call site. **`CREATE VIEW … SELECT *` freezes the column list — re-run the view
  definition after any `ALTER TABLE landmarks ADD COLUMN`.**
- `amenities_places` — **current** amenity table. Surrogate `id BIGSERIAL` PK; `place_id TEXT NOT
  NULL UNIQUE`; `opening_hours JSONB` **always NULL** (SKU rule below); `business_status TEXT`;
  `geom geography(Point,4326)`; GIST(geom); no embedding column. Curation columns: `city_id`,
  `stop_eligible` (defaults **false** — Places returns raw retail), `curation_source`,
  `duplicate_of`, `is_landmark`.
- `amenities` — **superseded** OSM table, kept for source comparison. Surrogate `id BIGSERIAL` PK;
  `osm_id` is **TEXT** (ids prefixed `n…`/`w…`/`r…`); `geom`; GIST(geom).
- `cities`, `anchors` — planning model. An anchor is a walkable cluster centre with a `radius_m`
  (default 700).

The two amenity tables are deliberately separate names. Nothing in the repo drops `amenities` —
losing it would destroy the only baseline for measuring OSM's miss rate.

## Key design decisions

**Landmark acquisition (`data_getting.py` / `enrich_landmarks.py`):**
- **Notability gate**: `MIN_SITELINKS` is the real quality filter, not the class list (intentionally coarse).
- **Containment, not identity**: `wdt:P131+` (transitive, `+` not `*`) — items *contained in* the city.
- **Deterministic categorization**: `CLASS_PRIORITY`/`CLASS_LABELS` (single source of truth) picks one
  primary category; broad roots ranked last so specific types win. QIDs verified against live
  Wikidata — don't trust QIDs from memory.
- **Discovery and enrichment are separate queries** — pinning the enrichment set with `VALUES`
  removes the transitive closure, keeping ~8 OPTIONALs cheap. Rule: pin the set, then enrich.
- **Null, never guess / flag, don't drop** — missing value ⇒ `None`, never `""` or a fabrication;
  drop only unambiguous junk (no name/coords), flag ambiguous cases.

**Eligibility & existence (planning-model columns on `landmarks`):**
- **Two eligibility columns, deliberately different.** `anchor_eligible` = can this be an anchor
  *centroid*? (broad blocklist — Baku railway station has 48 sitelinks and would otherwise be the
  second anchor). `stop_eligible` = can this be *ranked as a stop*? (physical access only — the
  parachute tower can't anchor a day but is a real stop). **Both read `all_types`, never
  `primary_class`** (the taxonomy collapses distinct things into one class).
- **Do not add "uninteresting" categories to `stop_eligible`.** A `school building` rule once buried
  the first secular school for Muslim girls in the region. Significance doesn't track building
  function — sitelinks tiering handles "not very interesting"; the blocklist handles "physically
  cannot go there".
- **`existence_status` is never an auto-drop.** Wikidata records what a building *was* (detected via
  `all_types ILIKE '%destroyed%|%former%'`, resolved by hand). Bibi-Heybat is tagged destroyed
  (dynamited 1936) but rebuilt and open today. Rows are flagged, never deleted — the record that we
  checked is the point. An earlier LLM `existence_doubt` field scored 1 hit / 1 false positive and
  was removed.

**Amenity acquisition (`get_amenities_places.py`):**
- **The field mask determines the billing SKU** (a request bills at the highest SKU of any field).
  The current mask (id, displayName, location, types, businessStatus, formattedAddress) is entirely
  **Nearby Search Pro** — 5,000 free calls/month. A single Enterprise field drops the whole run to
  1,000/month. `places.regularOpeningHours` was the trap and was removed; `opening_hours` is
  therefore always NULL. **Check any new field against Google's Enterprise trigger list first.**
- **`businessStatus` stays** — Pro-tier, and the only thing that measures staleness. Non-OPERATIONAL
  places are excluded from the CSV but *counted* (the closed count justified leaving OSM).
- **Tile grid, not one big radius** — Nearby Search hard-caps at 20 results with no pagination, so a
  large-radius query silently truncates. Coverage geometry: radius ≥ spacing/√2, deduped by place id.
- **Adaptive refinement** — a saturating tile is split into a 2×2 sub-grid at half spacing/radius and
  re-queried, recursing until nothing truncates or `MAX_REFINE_DEPTH` (800→400→200→100m).
- **Append-only JSONL checkpoint** (line 1 = plan fingerprint) — constant-time appends, no
  `os.replace` lock window for a sync client to corrupt. A torn final line costs one record.
- **Checkpoint carries a plan fingerprint** (hash of FIELD_MASK + GRIDS + NEARBY/TEXT_QUERIES +
  refine policy). Change the plan and the loader *refuses to resume*. It never auto-deletes the
  checkpoint. (This same fingerprint is why the mall/attraction top-up is a **separate script** —
  see `get_amenities_extra.py`.)

**Serving (`postgresser.py` / `amenities_loading*.py` / `retrieval_of_top_l.py`):**
- **Coordinates are longitude-first**: `ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography`.
  Wikidata WKT is also `Point(LONG LAT)`. A swapped pair returns zero rows or the wrong neighbourhood.
- **`geography` not `geometry`** — distances in real meters, so `ST_DWithin(…, 500)` = 500m.
- **Embeddings**: `gemini-embedding-001` @ **768 dims** (Matryoshka `output_dimensionality=768` —
  omit it and you get 3072 and every insert mismatches `vector(768)`). `RETRIEVAL_DOCUMENT` for rows,
  `RETRIEVAL_QUERY` for the question.
- **Two-stage retrieval**: cheap geo filter first, then pgvector cosine rerank within the candidate
  set. The `0.7·vec + 0.3·geo` weighting earns its keep only when candidates are spread far.
- **Amenities = retrieval, not generation** — inventing "cozy atmosphere" from a name+category is the
  same hallucination shape as inventing landmark history, so amenities get structured serving, no blurb.

**Planning model (`planning/`, and the anchor/cluster design):**
- **Anchors and clusters, not walkable-vs-city-spanning.** An anchor is a walkable cluster centre
  (`radius_m` default 700); a day is 1–2 anchors; between anchors there is an **explicit transport
  hop** as its own itinerary item. City coverage comes from *more anchors*, never a wider radius.
  Landmarks >50km out are out of scope; 6–25km out are day-trip tier (not built). Anchor selection
  is a UNION (dense cluster centroids ∪ high-salience `anchor_eligible` singletons, then greedy
  non-overlap) — **the greedy algorithm is not yet written; the four anchors are hand-picked.**
- **Grouping pipeline order** in `plan_units.py`: `split_eligible` (blocked rows never reach the
  model, appended ambient after — keeps a complete id partition) → quotas from eligible count →
  Gemini call → `enforce` → `_validate`.
- **Quotas computed, not hardcoded**, and passed in the user message:
  `primary = min(6, max(2, n//8))`, `secondary = min(12, max(3, n//4))`; `n < MIN_ANCHOR_N (5)` ⇒
  not an anchor, skip. Caps come from the day budget (6 primaries ≈ 2–3 days); the `//8`/`//4`
  divisors are **uncalibrated interpolation** — revisit after more anchors.
- **`enforce` = code-side rules the model is told and ignores.** (1) **Saturation cap**: classes to
  cap are *derived per anchor* — any `primary_class` at ≥`SATURATION_THRESHOLD` (0.15) share,
  excluding Wikidata catch-alls (`building`, `architectural structure`, `monument`, `structure`);
  max `MAX_PER_CATEGORY` (4) across primary+secondary, demoted slots not backfilled. A hardcoded
  whitelist was tried and is wrong (Baku saturates on mosques, Rome on churches). On Icherisheher
  this fires on `mosque` (20%) and `museum` (18%). (2) **One 180-minute unit per anchor**, rest → 90.
  (3) **Ambient/anchor_self ⇒ null duration**, runs last so demotions are covered.
- **`wiki_extract` carries *current use*, which `primary_class` does not** — a former school may now
  house a research institute. Prompt framing: **sitelinks rank, extract informs duration and breaks
  ties.** Do not let the extract override the sitelinks order (reproducibility across cities depends
  on ranking from the input). ~1/3 of extracts are Russian/Azerbaijani (`wiki_extract_lang`) — fine
  for tiering, but the app is English-only so they must not feed narration.

## LLM prompting convention

System prompts share a hard rule: **do not fabricate**. Clamp answers to the provided context; when
facts are missing, say so plainly rather than inventing; refuse off-topic or unsupported claims (the
anti-injection + scope wall is one instruction). Keep output short and conversational. Preserve this
grounding constraint when editing prompts. Per-run facts (entity tables, anchors, quotas) go in the
USER message — never in a system prompt that contains literal `{}` braces (any `.format()`/`%` call
raises `KeyError` on the first brace).

## Gotchas (cost real time — don't re-derive)

- **Supabase direct connection is IPv6-only** → `Name or service not known` on IPv4-only networks
  (typical in Baku). Use the **session pooler** (`…pooler.supabase.com`, user `postgres.<project-ref>`).
- **`gemini-2.5-flash` thinks by default and thinking tokens count against `max_output_tokens`** —
  unbounded thinking ate ~15.6k of a 16k budget and truncated the JSON mid-array (raising the cap did
  NOT help). Fixed with `ThinkingConfig(thinking_budget=4096)` + an explicit MAX_TOKENS truncation
  error in the streaming loop.
- **`existence_status != 'gone'` is a NULL trap** — `NULL != 'gone'` is not true in SQL, so it drops
  every unset-status row. Use `IS DISTINCT FROM 'gone'`. Confirm NULL semantics before assuming either.
- **`CREATE VIEW … SELECT *` freezes the column list** — re-run the `landmarks_live` definition after
  any `ALTER TABLE landmarks ADD COLUMN`.
- **`text-embedding-004` is DEAD** (deprecated, 404s). Use `gemini-embedding-001` with explicit
  `output_dimensionality=768`.
- **Use `google-genai`, not `google-generativeai`** — different API shape (`client.models.embed_content`,
  result at `resp.embeddings[0].values`).
- **psycopg2 uses `%s` positional params only**, never `:named`. Tuple order must match top-to-bottom.
- **Places tile saturation is a silent failure**: 20 results back means the response was *truncated*,
  not that the tile has 20 venues. `parse_places` counts saturated tasks on the raw response and
  declares the run INCOMPLETE. The fix is a finer grid for that type-group, not ignoring the warning.
- **`amenities_loading.py` has no ON CONFLICT** — running twice produced exactly 2× rows. Run once or
  TRUNCATE first. (`amenities_loading_places.py` fixed this; the OSM loader is left as-is.)
- **Overpass returns HTTP 200 with a `remark` field** for runtime timeout/OOM — treat `remark` as
  failure, not success. **406 = missing User-Agent**; **504s are normal under load** (retry/backoff).
- **Uncalibrated planning constants**: the quota divisors (`//8`, `//4`) and the 0.15 saturation
  threshold are guesses. `meters_away` is **radial from the anchor, not pairwise** — two stops both
  600m out can be 1.2km apart; walking distances don't exist yet.
- **`CORE_BOX` (40.34–40.44, 49.79–49.90) excludes real destinations** (e.g. Sederek) — amenity
  coverage is not complete for the city and should not be assumed so.
- **Anchor names and coordinates are hand-picked, not generated.**

## Hard constraints (do not forget)

- **NEVER run against the Gemini API unprompted** — the user's key is capped ~20 calls/day and THEY
  do the real prompting. `--dry-run` / `py_compile` / import checks only. Same for Places runs and
  any DB write.
- Windows console is cp1252 → prefix dry-runs with `PYTHONIOENCODING=utf-8`.
- Use the conda env python: `~/anaconda3/envs/goAI_env/python.exe`.
