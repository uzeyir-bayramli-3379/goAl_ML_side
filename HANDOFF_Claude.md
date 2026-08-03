# Handoff — anchor model, visit-unit grouping, Baku capacity (2026-08-03)

## Headline result

**Baku's itinerary capacity is ~32 hours ≈ 5 days**, computed rather than guessed:

| Anchor | N landmarks | Primary | Secondary | Coverage |
|---|---|---|---|---|
| Icherisheher | 99 | 12h | 5h | **17h** |
| Opera and Ballet | 33 | 4.5h | 4.2h | **8.7h** |
| Flame Towers | 11 | 4.5h | 1.8h | **6.3h** |
| Heydar Aliyev Center | 2 | — | — | below anchor floor |

At ~6h of visiting per 8h day (minus one meal and inter-stop walking), that is
5.3 days. **The day picker can honestly offer 1–5 for Baku.** Any city where
capacity falls short should cap the picker rather than generate thin days.

---

## The planning model (decided this session)

The original question was "walkable or city-spanning itineraries?" That is a
false binary and produces a bad plan either way. Baku is one dense walkable core
(Icherisheher) plus scattered outliers 2–25km out, so walkable-only repeats
itself after day one while city-spanning needs a routing API and invents travel
times.

**Resolution: anchors and clusters.**

- An **anchor** is a walkable cluster centre with a `radius_m` (default 700).
- A **day** is 1–2 anchors. Stops within an anchor are a walking radius apart.
- Between anchors there is an **explicit transport hop**, surfaced as its own
  itinerary item ("metro to Nariman Narimanov, ~20 min"), not swallowed in
  whitespace like the original vibe-coded mockup did.
- City coverage comes from **more anchors**, never a wider radius. A 3km radius
  is a 6km span nobody walks and it dissolves the district structure.
- Landmarks >50km out (Neft Daşları at 86km) are out of scope entirely.
  6–25km out (Ateshgah, Lökbatan, Bibi-Heybat) are **day-trip tier** — a
  different itinerary shape, not an anchor. Not built.

**Anchor selection is a UNION, not clustering alone.** DBSCAN on the 305
landmarks gives one 82-row blob for Icherisheher that refuses to split at any
eps, while Heydar Aliyev Center never clusters (it is one building in a park).
So: dense cluster centroids ∪ high-salience `anchor_eligible` singletons, then
greedy non-overlap (take top by sitelinks, claim its radius, skip everything
inside, repeat). **The four current anchors were hand-picked from the ranked
query — the greedy algorithm is NOT yet written.**

---

## Schema (`sql/schema.sql`, `sql/seed_baku.sql`, `sql/queries.sql`)

New: `cities`, `anchors`. New landmark columns: `city_id`, `anchor_id`,
`existence_status`, `anchor_eligible`, `stop_eligible`, `wiki_extract`,
`wiki_extract_lang`. New amenity columns: `city_id`, `stop_eligible`,
`curation_source`, `duplicate_of`, `is_landmark`.

### Two eligibility columns, deliberately different

- **`anchor_eligible`** — can this be an anchor *centroid*? Broad blocklist.
  Baku railway station has 48 sitelinks, second only to the Old City; without
  this it would be the second anchor in the city.
- **`stop_eligible`** — can this be *ranked as a stop*? Physical access only.
  The parachute tower and the funicular cannot anchor a day but are real stops.

**Both read `all_types`, never `primary_class`.** The taxonomy collapse puts
Baku Olympic Stadium and Crystal Hall both under `architectural structure`, so
`primary_class` cannot gate anything.

**Do not add "uninteresting" categories to `stop_eligible`.** An early
`school building` rule buried the Empress Alexandra Russian Muslim Boarding
School for Girls — the first secular school for Muslim girls in the region, and
exactly the kind of thing a city guide exists to surface. Significance does not
track building function. Sitelinks tiering is what handles "not very
interesting"; the blocklist handles "physically cannot go there".

### `existence_status` — 'ok' | 'flagged' | 'gone'

Wikidata records what a building **was**. Detected from
`all_types ILIKE '%destroyed%|%former%'`, resolved by hand. Six rows resolved
for Baku. **Never an auto-drop:** Bibi-Heybat is tagged destroyed (dynamited
1936) and was rebuilt in the late 1990s and is open today.

Rows are flagged, never deleted — the record that we checked is the point.
`landmarks_live` filters `'gone'`; existence is a *correctness* filter so it
belongs in a view, while eligibility is stage-specific and stays an explicit
WHERE at the call site.

Wikidata found all true positives here; an earlier LLM `existence_doubt` field
scored 1 hit / 1 false positive and was removed.

---

## Grouping agent (`planning/`) — WORKING, all three anchors run

`plan_units.py` + `planning_prompt_v1.py`. One Gemini call per anchor, offline,
at onboarding. Collapses landmark entities into **visit units** (one unit per
thing a visitor stops at), tiered primary/secondary/ambient, with durations.

### Pipeline order

1. `split_eligible` — `stop_eligible = false` rows never reach the model and are
   appended as ambient afterwards. Keeps the output a complete partition of the
   input so the id validator stays meaningful.
2. **Quotas computed from the eligible count**, passed in the user message:
   `primary = min(6, max(2, n//8))`, `secondary = min(12, max(3, n//4))`.
   `n < 5` → not an anchor, skip (a solo stop to attach to another anchor's day).
3. Gemini call. `temperature=0`, `MAX_OUTPUT_TOKENS=16000`,
   `THINKING_BUDGET=4096`, streaming, JSON mime type.
4. `enforce` — see below.
5. `_validate` — every input id in exactly one unit, none invented.

### Quota rationale

Caps come from the day budget: 6 primaries at 90–180min ≈ 2–3 days, 12
secondaries ≈ 1 more. So no single 700m circle can ever claim a whole trip.
Floors exist so a thin anchor still yields something. **The divisors (`//8`,
`//4`) are uncalibrated interpolation between the caps and floors** — revisit
after more anchors.

### `enforce` — rules the model is told and does not follow

The prompt states all three. The model ignored all three. Enforced in code:

- **Saturation cap.** Classes to cap are *derived per anchor*: any
  `primary_class` at ≥15% share, excluding Wikidata's catch-alls
  (`building`, `architectural structure`, `monument`, `structure`) which are
  frequent because the taxonomy gave up, not because the things are alike.
  Max 4 per class across primary+secondary; demoted slots are not backfilled.
  On Icherisheher this fires on `mosque` (20%) and `museum` (18%) — two
  demotions. A hardcoded `{mosque, hammam, caravanserai}` whitelist was tried
  first and is wrong: Baku saturates on mosques, Rome on churches, Kyoto on
  temples. The derived rule also caught museums, which neither of us predicted.
- **One 180-minute unit per anchor**, demote the rest to 90.
- **Ambient/anchor_self ⇒ null duration.** Runs last, so demotions are covered.

### `wiki_extract` — the highest-value change of the session

The Wikipedia lede is in the DB (305/305 populated), truncated to 400 chars in
the user message. It carries **current use**, which `primary_class` does not:
a former school may now house a research institute (Empress Alexandra → the
Institute of Manuscripts). Immediately fixed two things on the Opera run:

- Railway Museum 180min → 90min (extract: opened 2019 in the Sabunchu station).
- Government House → ambient/null (extract: houses state ministries, so not
  visitable — the prompt's "not open to visitors ⇒ ambient" clause firing).

Prompt framing matters: **sitelinks rank, extract informs duration and breaks
ties.** Do not let the extract override the sitelinks order — reproducibility
across cities depends on the ranking coming from the input, not the model's
familiarity. ~1/3 of extracts are Russian or Azerbaijani (`wiki_extract_lang`);
fine for tiering, but the app is English-only so they must not feed narration.

---

## Amenities

`amenities_places` has 6,563 rows from the main Places census, 82% food. A
top-up fetch (`get_amenities_extra.py`, 20 coarse-grid requests) added
`shopping_mall` and `tourist_attraction`.

**It is a separate script on purpose:** `get_amenities_places.py`'s
`plan_fingerprint()` hashes `NEARBY_QUERIES`, so appending one category
invalidates the checkpoint and forces the whole 812-task plan to be re-fetched
and re-billed. The guard is right to be conservative (it cannot tell "added a
type" from "moved the grid") but for a pure addition every existing task id
keeps its exact meaning. The proper fix is per-task fingerprints; not done.

**Results, honestly:** `tourist_attraction` was worth it but far less than it
first looked — of ~31 apparent additions, 15 were already landmarks and 10 were
already in the 348 `park` rows. **True unique yield ~6 rows** (Mini Venice,
Azneft Square, Swans Fountain, Saat Qülləsi, the Ağ Şəhər cluster, viewpoints).
`shopping_mall` is ~130 rows of noise — Google applies it to any retail
premises, including car dealerships and one traffic police station — 8 real
malls hand-picked. `market` is convenience stores; dropped entirely.

`stop_eligible` defaults **false** here (opposite of landmarks) because Places
returns raw retail. 34 hand-flagged true, ~174 hand-flagged false: **that split
is the labelled eval set** for the classifier that should eventually replace the
hand pass. Do not delete junk rows — `ON CONFLICT DO NOTHING` would re-import
them and the labels would be lost.

**Open:** the other ~337 `park` rows plus cinemas, theme parks, arcades are all
still `stop_eligible = false`. Needs a category-level decision (Oruj): which
amenity categories can fill a *stop* slot vs a *meal* slot. Gyms and nightclubs
should not.

---

## Known bugs / limitations

- **Scheduled units with null duration.** Muhammad Mosque came back
  `secondary` / `null` on the Icherisheher run. Will break the packer's
  arithmetic. Add to `enforce`: primary/secondary + null ⇒ default 45.
- **`anchor_self` over-used.** 11 units on Icherisheher including Fountains
  Square and the Sabir Garden. The model treats it as "part of the district"
  rather than "the anchor entity". Harmless for coverage (nulled) but Fountains
  Square is a real destination being discarded. Tighten the prompt line.
- **Quota divisors and the 0.15 saturation threshold are uncalibrated guesses.**
- **`CORE_BOX` (40.34–40.44, 49.79–49.90) excludes real destinations** —
  Sederek, the big wholesale complex, is outside it. Amenity coverage is not
  complete for the city and should not be assumed so.
- **Walking distances do not exist yet.** `meters_away` is radial from the
  anchor, not pairwise. Two stops both 600m out can be 1.2km apart.
- **Straight-line × 1.3 will underestimate inside Icherisheher** — it is a
  medieval maze, not a grid. Probably 1.5–1.6. Needs ground-truthing on foot.
- **Google Places ToS**: caching most Places content long-term is restricted;
  place IDs are explicitly storable. Flagged in `get_amenities_places.py`'s
  docstring, still unresolved before any public launch.
- **Anchor names and coordinates are hand-picked**, not generated.

---

## Next step: the packer

Everything above produces a fixed inventory at onboarding. The packer runs at
query time and does not exist. It needs, in order:

1. **Pairwise walk distances** between units in an anchor (computable from
   coordinates already stored — a small NxN, no API needed).
2. **Day budget arithmetic** — 8h minus one meal minus walking ≈ 6h visiting.
   Emit durations and offsets from the user's start time, not fabricated
   absolute clock times for every day.
3. **Ordering** — within a cluster this is a 4–6 stop TSP, brute-forceable in
   microseconds. There is no reason to ask a language model to sort five points
   on a map.
4. **Meal slots** — a stop consuming 60–90min that must sit geographically
   between its neighbours. Note: **opening hours are not fetched** (Enterprise
   SKU), so the serving layer must never claim a place is open.
5. **Transport hops** between anchors as first-class itinerary items.
6. **Preference filtering** — the existing preference→category classifier
   filters the unit pool *before* packing. It is a query-time step, NOT a
   change to the grouping prompt: grouping is onboarding-time and fixed.

Only step 6's classifier and the final **narration** pass use an LLM. Narration
receives a fully sequenced, fully timed plan and cannot move anything.

**The split that makes this work: generation and computation are different jobs
and only one of them belongs to Gemini.** The original mockup's four-hour walk
around the Old City walls came from an LLM with no duration field and no
distance matrix — not from LLM recklessness. Give it the data and it produces
sane timings; withhold the data and no prompt wording saves you.

---

## Constraints (do not forget)

- **NEVER run against the Gemini API unprompted** — the key is capped ~20
  calls/day and the user does the real prompting. `--dry-run` / `py_compile` /
  import checks only. Same for Places runs and DB writes.
- Windows console is cp1252 → prefix dry-runs with `PYTHONIOENCODING=utf-8` or
  Azerbaijani names (ə/ç) crash bare prints.
- Conda env python: `~/anaconda3/envs/goAI_env/python.exe`.
- `CREATE VIEW ... SELECT *` freezes the column list — re-run the
  `landmarks_live` definition after any `ALTER TABLE landmarks ADD COLUMN`.
- `ST_MakePoint` is **longitude first**. A swapped pair returns zero rows if you
  are lucky and the wrong neighbourhood if you are not.

---

## Superseded

Previous handoffs (grouping-agent prep 2026-08-03, Places loader + ground-truth
cross-tab 2026-07-29) are in `HANDOFF_old.md`. The 2026-07-29 Places/ground-truth
findings are still current and were not re-derived here.
