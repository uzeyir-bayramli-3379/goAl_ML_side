# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

The Machine Learning side of **GoAl / GoAI ("gAIde")**: an AI city-exploration guide for
foreigners, Baku launch, **English-only**. This repo owns the AI/ML track — grounding data
acquisition and a grounded retrieval loop — not the Flutter client or Spring Boot backend.

The core principle everywhere is **grounding / anti-hallucination**: the LLM narrates or serves
from verified place data, never invents. Two content types with opposite data shapes:

- **Landmarks** — sparse, fact-rich, *narrated* (Gemini writes a grounded paragraph). Source:
  Wikidata via SPARQL.
- **Amenities** (cafes/restaurants/leisure) — dense, fact-poor, *served not narrated* (structured
  retrieval — category/distance, no generated blurb). Source: **Google Places API (New)**.
  OpenStreetMap/Overpass was the original source and is now **superseded** (see below).

**Why the amenity source changed:** hand-validation of 50 OSM amenities near Icherisheher found
~20% confirmed permanently closed and ~25% unverifiable. OSM's Baku coverage is too stale to
ground answers on. Places (New) gives `businessStatus`, which makes staleness *measurable* instead
of invisible. The OSM pipeline and its CSV are kept as the **comparison baseline** for measuring
that miss rate — do not delete `baku_amenities_clean.csv`, it is the only copy.

There is no build system, package manifest, or test suite — these are standalone scripts run directly.

## Pipeline & files

Two acquisition pipelines produce CSVs; a serving layer loads them into Postgres and runs retrieval.

**Landmark acquisition (Wikidata):**
- `data_getting.py` — Stage 1 discovery SPARQL. `discover_landmarks() -> {QID: dict}`; `__main__`
  prints a DataFrame. Emits name/coords/category skeleton.
- `enrich_landmarks.py` — Stage 2. QID-keyed enrichment SPARQL (batched ~150, `VALUES ?item {…}`),
  per-QID JSON checkpoint (resumable), merges → `clean_landmarks()` → writes `baku_landmarks_clean.csv`
  + `baku_dropped.csv`. Optional `--wiki` stage 3 pulls Wikipedia intro extracts.
- `clean_landmarks.py` — filtering/dedup helper (sanity-box geo drop, core-zone flag, type tag).
  Imported by the enrichment stage.

**Amenity acquisition (Google Places API New) — CURRENT:**
- `get_amenities_places.py` — tiled `places:searchNearby` / `:searchText` discovery over the same
  `CORE_BOX`. Dual-resolution grid (fine 800m spacing / 600m radius for dense types; coarse
  2400m/1800m for sparse ones — radius >= spacing/sqrt2 so square cells are fully covered).
  Per-task JSON checkpoint (`baku_places_checkpoint.json`) guarded by a **plan fingerprint**.
  Writes `baku_amenities_places.csv`. Flags: `--dry-run`, `--sample`, `--yes`. Full run = 812
  requests and real quota — never run it unprompted.

**Amenity acquisition (OSM) — SUPERSEDED, kept as baseline:**
- `get_amenities.py` — Overpass discovery for cafes/restaurants/leisure POIs. Hardened HTTP layer.
  Writes `baku_amenities_clean.csv`. Still runnable, but not the source of truth. Its CSV is the
  comparison baseline for the Places-vs-OSM staleness analysis.

**Serving layer (Postgres + PostGIS + pgvector, on Supabase):**
- `postgresser.py` — landmark loader. `baku_landmarks_clean.csv` → `landmarks` table (`ON CONFLICT
  (wikidata_id) DO NOTHING`), builds `geom`, then embeds each row (`gemini-embedding-001` @ 768d,
  `RETRIEVAL_DOCUMENT`).
- `amenities_loading_places.py` — **current** amenity loader. `baku_amenities_places.csv` →
  `amenities_places` table, builds `geom`, creates GIST index. Idempotent: `place_id` is UNIQUE
  and the insert is `ON CONFLICT (place_id) DO NOTHING`. Has no destructive flags — it never
  touches the OSM table.
- `amenities_loading.py` — **superseded** OSM loader. `baku_amenities_clean.csv` → `amenities`
  table. **No ON CONFLICT** — the PK is a surrogate BIGSERIAL, so re-running duplicates rows
  (it already ran twice and produced exactly 2× rows): run once, or `TRUNCATE amenities
  RESTART IDENTITY` before reloading.
- Neither amenity loader embeds anything (structured/geo layer only).
- `retrieval_of_top_l.py` — two-stage landmark retrieval + grounded generation: `ST_DWithin` geo
  filter → pgvector `<=>` cosine rerank (weighted `0.7·vec + 0.3·geo`) → Gemini `gemini-2.5-flash`
  answer clamped to context.

**LLM prototypes (standalone experiments, not wired into the pipeline):**
- `test.py` — NVIDIA-hosted Gemma overview generation (used to test grounding prompts cheaply).
- `test2.py` — Google Gemini overview generation.

## Running

Scripts are run directly with Python. **Use the conda env python** — base `python` lacks
`requests`/`pandas`/`psycopg2`:

```
~/anaconda3/envs/goAI_env/python.exe <script>.py     # conda is NOT on PATH
```

```
python data_getting.py       # discovery SPARQL (prints DataFrame)
python enrich_landmarks.py    # discovery -> enrich -> clean -> CSVs  (--wiki adds extracts)
python get_amenities_places.py --dry-run  # print the 812-request plan, no calls
python get_amenities_places.py --sample   # ONE real request, print parsed rows
python get_amenities_places.py            # FULL run — 812 requests, real quota
python get_amenities.py       # SUPERSEDED: Overpass amenity discovery -> CSV
python postgresser.py         # load + geom + embed landmarks (idempotent)
python amenities_loading_places.py  # load + geom + GIST -> amenities_places (idempotent)
python amenities_loading.py   # SUPERSEDED OSM loader -> amenities  (RUN ONCE — no dedup)
python retrieval_of_top_l.py  # two-stage retrieval + grounded answer
```

On Windows the console is cp1252 → set `PYTHONIOENCODING=utf-8` when printing Azerbaijani text
(ə/ç chars crash bare prints).

Dependencies installed ad hoc (no requirements file): `requests`, `pandas`, `psycopg2`, `openai`,
`google-genai`, `python-dotenv`.

## Secrets

API keys live in `.env` (gitignored), loaded with `python-dotenv`. Expected keys:
- `GEMINI_API_KEY` — Google Gemini (embeddings + generation) in the serving layer and `test2.py`.
- `GEMMA_API_KEY` — NVIDIA integrate API (`integrate.api.nvidia.com`) in `test.py`.
- `GOOGLE_PLACES_API_KEY` — Google Places API (New) in `get_amenities_places.py`.
- `DATABASE_URL` — Supabase Postgres connection string (use the **session pooler**, see gotchas).

## Database

**Supabase: Postgres + PostGIS + pgvector.** One table per source. Connect via the **session
pooler**, not the direct connection.

- `landmarks` — keyed on `wikidata_id`; `geom geography(Point,4326)`; `embedding vector(768)`;
  indexes GIST(geom) + HNSW(embedding, `vector_cosine_ops`).
- `amenities_places` — **current** amenity table. Surrogate `id BIGSERIAL` PK; `place_id TEXT NOT
  NULL UNIQUE` (Google place ids are the one Places field storable indefinitely under ToS);
  `opening_hours JSONB` **always NULL** (see SKU rule below); `business_status TEXT`;
  `geom geography(Point,4326)`; GIST(geom); no embedding column (deferred on purpose).
- `amenities` — **superseded** OSM table, kept for source comparison. Surrogate `id BIGSERIAL` PK;
  `osm_id` is **TEXT** (OSM ids are prefixed `n…`/`w…`/`r…`); `geom`; GIST(geom).

The two amenity tables are deliberately separate names. Nothing in the repo drops `amenities` —
losing it would destroy the only baseline for measuring OSM's miss rate.

## Key design decisions

**Acquisition (`data_getting.py` / `enrich_landmarks.py`):**
- **Notability gate**: `MIN_SITELINKS` is the real quality filter, not the class list (intentionally coarse).
- **Containment, not identity**: `wdt:P131+` (transitive, `+` not `*`) — items *contained in* the city.
- **Deterministic categorization**: `CLASS_PRIORITY`/`CLASS_LABELS` (single source of truth) picks one
  primary category; broad roots (building, architectural structure) ranked last so specific types win.
  QIDs verified against live Wikidata — don't trust QIDs from memory.
- **Discovery and enrichment are separate queries** — pinning the enrichment set with `VALUES` removes
  the transitive closure, keeping ~8 OPTIONALs cheap. Rule: pin the set, then enrich.
- **Null, never guess / flag, don't drop** — missing value => `None`, never `""` or a fabrication;
  drop only unambiguous junk (no name/coords), flag ambiguous cases. Visible nulls are what let silent
  label bugs surface instead of shipping empty strings into a grounding slot.

**Amenity acquisition (`get_amenities_places.py`):**
- **The field mask determines the billing SKU**, and a request bills at the *highest* SKU of any
  field requested. The current mask (id, displayName, location, types, businessStatus,
  formattedAddress) is entirely **Nearby Search Pro** — 5,000 free calls/month. Adding a single
  Enterprise field drops the whole run to 1,000/month, an 80% cut. `places.regularOpeningHours`
  was the trap and was removed for exactly this reason; `opening_hours` is therefore always NULL.
  **Check any new field against Google's Enterprise trigger list before adding it.**
- **`businessStatus` stays** — it is Pro-tier and it is the only thing that measures staleness.
  Non-OPERATIONAL places are excluded from the CSV but *counted*, because the closed count is the
  metric that justified leaving OSM.
- **Tile grid, not one big radius** — Nearby Search hard-caps at 20 results with no pagination in
  the new API, so a large-radius query silently truncates. Coverage geometry: radius >=
  spacing/sqrt2 (600 >= 800/1.414, 1800 >= 2400/1.414), deduped by place id across overlaps.
- **Checkpoint carries a plan fingerprint** (hash of FIELD_MASK + GRIDS + NEARBY_QUERIES +
  TEXT_QUERIES). Change the plan and the loader *refuses to resume* rather than mixing responses
  with the wrong field set, or letting a stale `task_id` fall through to the `restaurant` fallback
  category. It never auto-deletes the checkpoint — that stays a human decision.

**Serving (`postgresser.py` / `amenities_loading*.py` / `retrieval_of_top_l.py`):**
- **Coordinates are longitude-first**: `ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography`.
  Flip and points land in the wrong hemisphere. Wikidata WKT is also `Point(LONG LAT)`.
- **`geography` not `geometry`** — distances come out in real meters, so `ST_DWithin(…, 500)` = 500m.
- **Embeddings**: `gemini-embedding-001` @ **768 dims** (Matryoshka `output_dimensionality=768` —
  omit it and you get 3072 and every insert mismatches `vector(768)`). `RETRIEVAL_DOCUMENT` when
  embedding rows, `RETRIEVAL_QUERY` when embedding the question.
- **Two-stage retrieval**: cheap geo filter first (`ST_DWithin`), then pgvector cosine rerank within
  the candidate set. The `0.7·vec + 0.3·geo` weighting is marginal in a compact area (proximity and
  relevance rarely disagree in a tight radius) — earns its keep only when candidates are spread far.
- **Amenities = retrieval, not generation** — inventing "cozy atmosphere" from a name+category is the
  same hallucination shape as inventing landmark history, so amenities get structured serving, no blurb.
  Vibe search (embeddings on LLM-synthesized ambiance text) is a deferred next step, not v1.

## LLM prompting convention

System prompts share a hard rule: **do not fabricate**. Clamp answers to the provided context;
when facts are missing, say so plainly ("no information found on X") rather than inventing; refuse
off-topic or unsupported claims (the anti-injection + scope wall is one instruction). Keep output
short and conversational. Preserve this grounding constraint when editing prompts.

## Gotchas (cost real time — don't re-derive)

- **Supabase direct connection is IPv6-only** → `Name or service not known` on IPv4-only networks
  (typical in Baku). Use the **session pooler** (`…pooler.supabase.com`, user `postgres.<project-ref>`).
- **`text-embedding-004` is DEAD** (deprecated, 404s). Use `gemini-embedding-001` with explicit `output_dimensionality=768`.
- **Use `google-genai`, not `google-generativeai`** — different API shape (`client.models.embed_content`,
  result at `resp.embeddings[0].values`).
- **psycopg2 uses `%s` positional params only**, never `:named`. Tuple order must match top-to-bottom.
- **Places tile saturation is a silent failure**: 20 results back from a tile means the response
  was *truncated*, not that the tile has 20 venues. Same shape as the Overpass `remark` bug below —
  a successful-looking response that is actually incomplete. `parse_places` counts saturated tasks
  on the raw response (before dedup/filtering) and declares the run INCOMPLETE. If it fires, the
  fix is a finer grid for that type-group, not ignoring the warning.
- **`amenities_loading.py` has no ON CONFLICT** — running twice produced exactly 2× rows. Run once or TRUNCATE first.
  (`amenities_loading_places.py` fixed this; the OSM loader is left as-is since it is superseded.)
- **Overpass returns HTTP 200 with a `remark` field** for runtime timeout/OOM — treat `remark` as failure, not success.
- **Overpass 406 = missing User-Agent**; **504s are normal under load** and handled by retry/backoff.
