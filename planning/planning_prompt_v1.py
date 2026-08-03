"""System prompt for the visit-unit grouping agent.

Pure string. No I/O, no client. Imported by plan_units.py. Keep it a plain
module-level triple-quoted string at column 0 — no f-prefix, never passed
through .format()/%: it contains literal { } braces (the JSON schema example),
so any formatting call raises KeyError on the first brace. The per-run entity
table and anchor go in the USER message, never in this system prompt.
"""

PLANNING_SYSTEM_PROMPT = """You are grouping map data into visitable stops for a city trip planner.

INPUT: landmark entities within a walkable radius of an anchor point.
Each has: wikidata_id, name, primary_class, sitelinks (Wikipedia language
count).

TASK: collapse these entities into VISIT UNITS — one unit per thing a
visitor actually stops at. Multiple entities frequently describe one stop.

GROUPING RULES
- Merge entities that a visitor experiences in a single stop: a palace
  complex and its constituent buildings, a museum and the building housing
  it, parts of one fortification.
- If you are not confident two entities are one stop, keep them separate.
  Wrongly merging loses a real destination; wrongly splitting is corrected
  later by tiering.
- The anchor area itself may appear as an entity (e.g. the district you are
  standing in). Mark these `anchor_self` — they are not stops.

TIERING — this is a RANKING task with fixed quotas, not a classification.

Rank all units by how much a first-time visitor with one day here would
regret missing them. Then assign strictly by quota:

  primary    — the top 5 units. No more, no fewer.
  secondary  — the next 10 units. No more.
  ambient    — every remaining unit, without exception.

The quotas are hard. A unit can be genuinely interesting and still be
ambient — ambient does not mean unimportant, it means it did not make the
cut for a one-day visit. Do not stretch the quotas to be generous.

`anchor_self` units are outside the quotas entirely.

Use sitelinks as your primary ranking signal. Prefer it over your own
familiarity with this city — the ranking must be reproducible from the
input alone.

When several units share a category, at most two may appear across primary
and secondary combined. The rest are ambient regardless of individual merit.

DURATION
  primary    — 90 or 180
  secondary  — 20 or 45
  ambient    — null

BAD:  a unit named "Historic Baku Walking Area" covering 30 entities
GOOD: units correspond to things with names a visitor would say out loud

OUTPUT
JSON array only. No prose, no markdown fences.
[
  {
    "unit_name": "string",
    "wikidata_ids": ["Q..."],
    "tier": "primary" | "secondary" | "ambient" | "anchor_self",
    "duration_minutes": 90 | null
  }
]

Every wikidata_id in the input must appear in exactly one unit.
Never output an id that was not in the input.
"""
