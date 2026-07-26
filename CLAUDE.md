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
  retrieval — category/cuisine/hours/distance, no generated blurb). Source: OpenStreetMap via Overpass.

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

**Amenity acquisition (OSM):**
- `get_amenities.py` — Overpass discovery for cafes/restaurants/leisure POIs. Hardened HTTP layer.
  Writes `baku_amenities_clean.csv`.

**Serving layer (Postgres + PostGIS + pgvector, on Supabase):**
- `postgresser.py` — landmark loader. `baku_landmarks_clean.csv` → `landmarks` table (`ON CONFLICT
  (wikidata_id) DO NOTHING`), builds `geom`, then embeds each row (`gemini-embedding-001` @ 768d,
  `RETRIEVAL_DOCUMENT`).
- `amenities_loading.py` — amenity loader. `baku_amenities_clean.csv` → `amenities` table, builds
  `geom`, creates GIST index. **No embeddings** (structured/geo layer only). **No ON CONFLICT** —
  the PK is a surrogate BIGSERIAL, so re-running duplicates rows: run once, or `TRUNCATE amenities
  RESTART IDENTITY` before reloading.
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
python get_amenities.py       # Overpass amenity discovery -> CSV
python postgresser.py         # load + geom + embed landmarks (idempotent)
python amenities_loading.py   # load + geom + GIST amenities  (RUN ONCE — no dedup)
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
- `DATABASE_URL` — Supabase Postgres connection string (use the **session pooler**, see gotchas).

## Database

**Supabase: Postgres + PostGIS + pgvector.** Landmarks and amenities are **two tables**, mirroring
the two-source split. Connect via the **session pooler**, not the direct connection.

- `landmarks` — keyed on `wikidata_id`; `geom geography(Point,4326)`; `embedding vector(768)`;
  indexes GIST(geom) + HNSW(embedding, `vector_cosine_ops`).
- `amenities` — surrogate `id BIGSERIAL` PK; `osm_id` is **TEXT** (OSM ids are prefixed `n…`/`w…`/`r…`);
  `geom geography(Point,4326)`; GIST(geom); **no embedding column yet** (deferred on purpose).

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

**Serving (`postgresser.py` / `amenities_loading.py` / `retrieval_of_top_l.py`):**
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
- **`amenities_loading.py` has no ON CONFLICT** — running twice produced exactly 2× rows. Run once or TRUNCATE first.
- **Overpass returns HTTP 200 with a `remark` field** for runtime timeout/OOM — treat `remark` as failure, not success.
- **Overpass 406 = missing User-Agent**; **504s are normal under load** and handled by retry/backoff.
