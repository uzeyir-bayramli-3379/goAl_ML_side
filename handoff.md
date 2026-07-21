# Handoff — GoAI ML side (grounding data acquisition)

_Written 2026-07-19. Curated state for a fresh session (context-handoff pattern,
not a compaction). Everything below is recoverable from the code; this is the
"why" and the "where we are."_

## Goal

Build the **grounding data layer** for GoAI ("gAIde"), the AI city-exploration app
(Baku, English-only). Two mirror-image sources feed the LLM so it narrates/serves
from verified facts instead of hallucinating:
- **Landmarks → Wikidata (SPARQL)** — sparse, fact-rich, *narrated* by Gemini.
- **Amenities → OpenStreetMap (Overpass)** — dense, fact-poor, *served* (structured
  retrieval, no generated blurb).

Hard project rules, enforced everywhere: **missing value => None, never "" or a
guess**; **drop only unambiguous junk, flag ambiguous cases**; **surface HTTP errors
with their real body, never swallow them**.

## Current state — all three pipelines run clean end-to-end

- `data_getting.py` — discovery SPARQL. Refactored to expose
  `discover_landmarks() -> {QID: dict}`; `__main__` still prints the DataFrame.
  Fixed two wrong category QIDs (Q12513 "helical stairs"→Q12280 bridge;
  Q329777 "appeal"→Q2977 cathedral), added mosque Q32815, single-source
  `CLASS_LABELS` drives priority + SPARQL VALUES + display labels. Outputs
  human-readable `Primary_Class`.
- `enrich_landmarks.py` (new) — batched (~150) QID-keyed enrichment SPARQL,
  per-QID JSON checkpoint (resumable), merge → `clean_landmarks()` → writes
  `baku_landmarks_clean.csv` + `baku_dropped.csv`. Optional `--wiki` stage 3
  fetches Wikipedia intro extracts for `core_zone OR sitelinks>=10`.
  **Result: 399 discovered → 305 clean.** Spot-checks pass (Maiden Tower
  inception 1200, Heydar Aliyev Center architect = Zaha Hadid, 125/305 heritage).
- `get_amenities.py` — Overpass amenity discovery. Request layer hardened (see
  below). **Result: 1,102 clean in CORE_BOX** (restaurant 483, cafe 370).
- `clean_landmarks.py` — pre-existing, unchanged (reused verbatim).
- `GoAI_gAIde_Project 1.md` — engineering notebook; added a section documenting
  this session's data-acquisition work + lessons.

Nothing committed. Outputs (CSVs) and checkpoints (`enrichment_checkpoint.jsonl`,
`wiki_extracts.jsonl`) are on disk.

**Env:** run with `~/anaconda3/envs/goAI_env/python.exe` (has requests/pandas).
Base `python` does NOT — it's only good for `py_compile`. `conda` is not on PATH.
Windows console is cp1252 → set `PYTHONIOENCODING=utf-8` when printing Azerbaijani
text (the ə/ç chars crash bare prints).

## Files actively edited this session

- `data_getting.py` — QID fixes + function refactor.
- `enrich_landmarks.py` — created from scratch.
- `get_amenities.py` — request/error layer + headers only (query/parse/clean untouched).
- `GoAI_gAIde_Project 1.md` — appended documentation section.

## Tried and failed (don't re-derive these)

1. **`wikibase:label` auto-service inside `GROUP_CONCAT` under `GROUP BY`** — silently
   returns EMPTY for every QID-valued label (architect/style/heritage all null).
   First enrichment run looked like it worked but every multi-valued label column
   was 100% null. **Fix that works:** explicit `?x rdfs:label ?xLabel.
   FILTER(LANG(?xLabel)="en")` joins, drop the SERVICE. Verified live.
   ⚠️ If you change the enrich query, you MUST delete `enrichment_checkpoint.jsonl`
   or it re-serves the stale/broken cache.
2. **Overpass 406 Not Acceptable** — caused by missing/default User-Agent, NOT the
   query. Fixed with a descriptive UA + `Accept: application/json`.
3. **Overpass 504s** are normal under load — the retry/backoff handles them (live run
   hit 504 twice, recovered on attempt 3). Do not treat as fatal.
4. **`requests`/`conda` in base shell** — not available; use the goAI_env python path.

## Next step I'd take

**Verify the two grounding datasets actually join into one usable serving payload.**
The landmark and amenity CSVs share the same coordinate/box conventions (CORE_BOX,
SANITY_BOX) but nothing has confirmed they compose. Concretely:
- Decide the unified schema the backend ingests (both currently have Name/Lat/Lon/
  category + type-specific fact columns) and whether they're one table or two.
- Then the real open thread from the notebook: **write the batch pre-generation
  script** — loop `baku_landmarks_clean.csv` seed rows → build the grounded Gemini
  prompt from the fact columns → validate JSON → (eventually) write Postgres / warm
  Redis. The enrichment columns (inception, architect, heritage, wiki_extract) are
  exactly the grounding slots that script fills.

Secondary: run `python enrich_landmarks.py --wiki` (not yet done) and
`python get_amenities.py --wide` for full-city coverage once the schema is settled.
