# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

The Machine Learning side of the **GoAl** project: a landmark/travel guide for foreigners.
The pipeline has two independent stages, currently prototyped as standalone scripts:

1. **Data acquisition** (`data_getting.py`) — pulls structured landmark data (name, coordinates,
   category) for a city from Wikidata via SPARQL.
2. **Description generation** (`test.py`, `test2.py`) — feeds landmark facts to an LLM to produce
   short, grounded overviews for travelers.

There is no build system, package manifest, or test suite yet — these are exploratory scripts.

## Running

Scripts are run directly with Python (conda is the configured env manager per `.vscode/settings.json`):

```
python data_getting.py   # query Wikidata for landmarks (prints a pandas DataFrame)
python test.py           # generate a landmark overview via NVIDIA-hosted Gemma
python test2.py          # generate an overview via Google Gemini
```

Dependencies are installed ad hoc (no requirements file): `requests`, `pandas`, `openai`,
`google-genai`, `python-dotenv`.

## Secrets

API keys live in `.env` (gitignored) and are loaded with `python-dotenv`. Expected keys:
- `GEMMA_API_KEY` — used in `test.py` against the NVIDIA integrate API (`integrate.api.nvidia.com`).
- `GEMINI_API_KEY` — used in `test2.py` against Google Gemini.

## Key design decisions (data_getting.py)

- **Notability gate**: `MIN_SITELINKS` (Wikidata sitelink count) is the real quality filter, not the
  class list. The class VALUES list is intentionally coarse.
- **Containment, not identity**: the query uses `wdt:P131+` (transitive, `+` not `*`) so results are
  items *contained in* the city, excluding the city entity itself.
- **Deterministic categorization**: when an item matches several root classes, `CLASS_PRIORITY`
  picks one primary category. The two broad roots (building, architectural structure) are ranked
  last on purpose so specific types (e.g. place of worship) win. QID meanings marked "verify" in
  comments should be confirmed before trusting them.
- **Coordinates**: Wikidata returns WKT `Point(LONG LAT)` — longitude first. The raw WKT string is
  preserved (`WKT` field) for direct PostGIS ingestion; parsed `Latitude`/`Longitude` are separate.

## LLM prompting convention

The system prompts across `test.py`/`test2.py` share a hard rule: **do not fabricate**. When facts
are missing the model must say so explicitly (e.g. "no information found on X") rather than inventing
details, and keep output short. Preserve this grounding constraint when editing prompts.
