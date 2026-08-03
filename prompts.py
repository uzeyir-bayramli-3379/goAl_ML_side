"""Prompt text and prompt-building helpers for landmark overview generation.

Pure strings + string assembly. No I/O, no API client, no network. Imported by
generate_overviews.py (offline batch) — NOT by the backend.
"""

import re

import pandas as pd

# The Wikidata columns worth feeding the model as grounding FACTS. Coordinates,
# WKT, Sitelinks, image filenames, wiki URLs and the internal flags (core_zone,
# coord_group, type_tag) are deliberately excluded — they are not narratable
# facts. wiki_extract is handled separately by build_facts_block (goes last).
FACT_COLUMNS = [
    "Primary_Class",
    "inception",
    "official_opening",
    "height_m",
    "architectural_style",
    "architect",
    "founded_by",
    "creator",
    "material_used",
    "named_after",
    "heritage_designation",
    "description",
]

# ---------------------------------------------------------------------------
# PLACEHOLDER — paste the real system prompt below, replacing the marker text.
#
# HARD RULE: this stays a plain module-level triple-quoted string at column 0.
#   - NO f-prefix.
#   - NEVER pass it through .format() / % / .format_map().
# It contains literal { } braces (JSON schema example), so any formatting call
# raises KeyError on the first brace. Interpolate the per-landmark facts into the
# *user* message (build_facts_block), never into this system prompt.
# ---------------------------------------------------------------------------
OVERVIEW_SYSTEM_PROMPT = """You are writing for a Baku city guide app. Two audiences in one voice:
a well-travelled 45-year-old who has actually been here, talking to a
friend who just arrived. Curious, unhurried, not selling anything.

Source material may be in Azerbaijani or Russian. Write output in English.
Do not translate proper nouns that have an established English form.

GROUNDING CONTRACT — non-negotiable:
- Use ONLY the facts given. You know things about Baku. Do not
  use them. Anything not in the facts does not exist for this task.
- If a field is absent, do not gesture at it. No "little is known about
  its origins" — that is you noticing a gap and narrating the gap.
- If the facts contain fewer than three concrete, non-tautological details,
  set "narrative" to null. Do not stretch. A null is a correct answer.
- These do not count toward the three-detail threshold:
  - Primary_Class when it merely restates the name
  - heritage_designation (boilerplate, present on most entries)
  - description when it only restates the name, the type, or the city
  - architectural_style when it is a generic parent category
    (e.g. "Islamic architecture") rather than a named school
  Count only details a visitor could not have guessed from the name alone.
- If the source material describes the place in the past tense, or
  indicates it was destroyed, demolished, replaced, or converted to
  another use, do NOT write in present tense. State plainly that it
  no longer stands or no longer serves that purpose.

VOICE:
- Choose one detail and make it land. Do not list everything you were given.
- Dates only when anchored to something human or comparative.
- Present tense, UNLESS the grounding contract's past-tense rule applies.
  Second person occasionally, sparingly.
- Short sentences carry more weight than long ones.
- NEVER use: testament to, rich history, steeped in, nestled, iconic,
  must-see, hidden gem, whispers, stands proud, bustling, charming.
- Atmosphere is a fact claim. You have never been to this place. Do not
  describe how it feels, sounds, or how busy it is unless the source
  says so. "Sits quietly", "offers a glimpse into local life", "worth
  a detour" — none of these are in the facts.
- Voice lives in HOW you state a fact, not in additions to it. "Stone,
  all of it" is voice. "Sits quietly in the district" is invention.
- Never open a narrative with "You know", "So", "Imagine", or a
  rhetorical question. Start with the fact.
- Never write "what's interesting is", "what's wild is", or
  "surprisingly". Delete the label, keep the fact.

VOICE RULES — these are absolute, not stylistic preferences:

-Opening: the first sentence of `narrative` must begin with the
subject or a fact about it. Never open with "You know", "So",
"Imagine", "Picture this", or a rhetorical question.
  BAD:  "You know, this isn't just any mud volcano."
  GOOD: "Lökbatan is one of the world's five most active mud volcanoes."

-Interest labels: never announce that a fact is interesting. If you
write "what's interesting is", "what's really interesting is",
"what's wild is", "surprisingly", or "remarkably", delete the label
and keep the fact that followed it.
  BAD:  "What's really interesting is its role in the oil industry."
  GOOD: "In 1933, a well drilled here gushed 20,000 tons of oil."

-A fact is interesting because of what it says. Labelling it as interesting makes it less so.

- Never evaluate a fact before stating it. Do not tell the reader a
thing is dramatic, surprising, notable, or interesting — state the
thing and let it land.
  BAD:  "Its eruptions can be quite dramatic; in 1867, flames
         reached 400-500 meters."
  GOOD: "In 1867, flames reached 400-500 meters — bright enough to
         read a newspaper 60km away."

- Order by interest, not chronology. The single most striking fact in
the source goes in the first or second sentence. Background and
context follow it.

-Never label a date as the "last", "latest", or "most recent" unless
the source explicitly says so. State the date plainly instead.
  BAD:  "with its last recorded event in September 2024"
  GOOD: "It has erupted 25 times since 1829, most recently in
         April 2025." [only if source says most recently]
  SAFE: "Eruptions were recorded in September 2024 and April 2025."
  ALSO: "The museum was renovated in 2019." not "The museum's most
         recent renovation was in 2019."
Return ONLY valid JSON, no markdown fences:
{
  "card_summary": "1-2 sentences. What this is and why a person would walk to it. Grounded. Voice still applies.",
  "narrative": "as many sentences as the facts genuinely support, up to 5.
  Three facts make three short sentences, not five padded ones. If you
  find yourself writing a sentence that adds no information ("which is
  interesting", "as the name suggests"), delete it and return fewer."
  "retrieval":    "2-3 dense sentences, plain and factual, no voice. Written to be matched against a search query."
}
"""


# Wikidata year-precision dates are stored with a 01-01 PLACEHOLDER and the
# precision flag was dropped when the CSV was built — unrecoverable. So ANY
# value shaped like an ISO-8601 timestamp is rendered as the bare year: no
# landmark here needs day precision, and "opened on the first of January" is a
# fabrication risk that outweighs the month/day lost on the handful with real ones.
_ISO_TS = re.compile(r"^(\d{4})-\d{2}-\d{2}T")

# founded_by = a country is a broken Wikidata claim (a country did not found a
# neighbourhood mosque). Unlike a missing field it produces a confident FALSEHOOD
# that passes the grounding contract cleanly, so drop it.
_COUNTRY_NAMES = {
    "azerbaijan", "russia", "soviet union", "ussr", "iran", "turkey",
    "united kingdom", "united states",
}

# Words stripped before judging whether a description merely restates the entry.
_DESC_STOPWORDS = {
    "a", "an", "the", "in", "of", "and", "is", "was", "to", "at", "on", "for",
    "capital", "city", "town", "baku", "azerbaijan",
}

# architectural_style values that are generic parent categories, not a named
# school — tautological on their subject ("Islamic architecture" on a mosque).
# Hardcoded denylist, not inference: the list is finite and known, and a fixed
# set is more predictable than a genericness heuristic. Specific schools
# (e.g. "Shirvan-Absheron architectural school") are KEPT.
_GENERIC_STYLES = {
    "islamic architecture",
    "modern architecture",
    "soviet architecture",
    "islamic",
}

REGISTERS = {
    "wise_traveller": OVERVIEW_SYSTEM_PROMPT,
}
DEFAULT_REGISTER = "wise_traveller"


def _render_date(val):
    """ISO-8601 timestamp -> bare year string; anything else unchanged."""
    m = _ISO_TS.match(val)
    return m.group(1) if m else val


def _is_country(val):
    return val.strip().lower() in _COUNTRY_NAMES


def _is_restatement(desc, name, primary_class):
    """True when `desc` only restates the name / type / city. Heuristic: strip the
    landmark name, the Primary_Class value, Baku/Azerbaijan and stopwords, and if
    under 3 words remain there is no fact left worth feeding the model."""
    drop = set(_DESC_STOPWORDS)
    drop |= set(re.findall(r"[a-z]+", str(name).lower()))
    if isinstance(primary_class, str):
        drop |= set(re.findall(r"[a-z]+", primary_class.lower()))
    remaining = [t for t in re.findall(r"[a-z]+", desc.lower()) if t not in drop]
    return len(remaining) < 3


def build_facts_block(row, fact_columns):
    """Per-landmark user message.

    Line 1 is the landmark name. Then one "column: value" line per non-null
    fact column, in fact_columns order. Then, if a wiki_extract is present, a
    blank line followed by the extract.

    NaN guard (this is the whole point of the function): pandas represents a
    missing cell as float('nan'), and bool(nan) is True — so a naive `if val:`
    happily writes "inception: nan" into the prompt and the model narrates a
    landmark founded in nan. Text columns are gated on isinstance(str); anything
    else is gated on pd.notna() so numeric facts (e.g. height_m) still pass.

    Garbage filtering: ISO dates collapse to year; country-valued founded_by and
    name/type/city-restating descriptions are dropped (broken claims that read as
    confident facts are worse than a visible gap).
    """
    name = str(row["Name"])
    lines = [name]
    inception_val = None
    for col in fact_columns:
        val = row.get(col)
        if isinstance(val, str):
            v = val.strip()
            if not v:
                continue
            if col == "founded_by" and _is_country(v):
                continue
            if col == "description" and _is_restatement(v, name, row.get("Primary_Class")):
                continue
            if col == "architectural_style" and v.lower() in _GENERIC_STYLES:
                continue
            rendered = _render_date(v)
            if col == "inception":
                inception_val = rendered
            # official_opening that renders to the same value as inception is a
            # duplicate (both bare years after the precision fix) — two lines
            # saying "2014" inflate the fact count and push rows over the
            # three-detail threshold that should return null.
            if col == "official_opening" and rendered == inception_val:
                continue
            lines.append(f"{col}: {rendered}")
        elif pd.notna(val):
            lines.append(f"{col}: {val}")

    extract = row.get("wiki_extract")
    if isinstance(extract, str) and extract.strip():
        lines.append("")
        lines.append(extract.strip())

    return "\n".join(lines)
