"""merge_baku_units.py — combine all anchor unit JSONs into one packer input.

Merges three plan_units.py outputs (Icherisheher, Flame Towers, Opera and
Ballet) plus a manually authored entry for Heydar Aliyev Center (which has
only 2 landmarks and falls below the MIN_ANCHOR_N=5 floor in plan_units.py,
so it never gets a generated units file — authored by hand per the HANDOFF
design doc's "solo stop to attach to another anchor's day" pattern).

Output: baku_all_units.json — feed this to pack_day.py as --json_name.

Usage:
    python merge_baku_units.py
    python merge_baku_units.py --icherisheher my_old_city.json --out custom_out.json

Validation (runs automatically):
    - No duplicate wikidata_ids across the merged set
    - No primary/secondary unit has a null duration (would break packer arithmetic)
    - Every unit has required keys: unit_name, wikidata_ids, tier, duration_minutes
    - Tier values are all legal
    - Prints a tier summary per anchor so you can sanity-check coverage
"""

import argparse
import json
from pathlib import Path

# --------------------------------------------------------------------------
# Default file names — override via CLI args if your files are named differently
# --------------------------------------------------------------------------
DEFAULTS = {
    "icherisheher": "insider_plan.json",
    "flame_towers": "flame_towers_plan.json",
    "opera":        "opera_house_plan.json",
    "out":          "baku_all_units.json",
}

# --------------------------------------------------------------------------
# Heydar Aliyev Center — manually authored.
# Only 2 landmarks in its radius; plan_units.py skips it (below MIN_ANCHOR_N=5).
# QIDs verified against the heydar_center_test.csv you ran.
#
# Tier reasoning:
#   Heydar Aliyev Center: 34 sitelinks — primary by any measure. 90 min
#   (museum + architecture, not a 3-hour complex).
#   Baku Convention Center: 7 sitelinks — secondary, 45 min.
#   wiki_extract for Convention Center is Russian — fine for tiering,
#   must NOT feed Stage 8 narration (filter on wiki_extract_lang='ru').
# --------------------------------------------------------------------------
HEYDAR_UNITS = [
    {
        "unit_name": "Heydar Aliyev Center",
        "wikidata_ids": ["Q43928"],
        "tier": "primary",
        "duration_minutes": 90,
    },
    {
        "unit_name": "Baku Convention Center",
        "wikidata_ids": ["Q20813015"],
        "tier": "secondary",
        "duration_minutes": 45,
    },
]

VALID_TIERS = {"primary", "secondary", "ambient", "anchor_self"}
SCHEDULED_TIERS = {"primary", "secondary"}


def load_json(path: Path, label: str) -> list:
    if not path.exists():
        raise FileNotFoundError(
            f"[{label}] File not found: {path}\n"
            f"  Run plan_units.py for this anchor first, or pass a different "
            f"path via --{label.lower().replace(' ', '_')}."
        )
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"[{label}] Expected a JSON array, got {type(data).__name__}.")
    print(f"  {label}: {len(data)} units loaded from {path.name}")
    return data


def validate(units: list) -> list[str]:
    """Returns a list of problem strings. Empty = clean."""
    problems = []
    seen_wids: dict[str, str] = {}   # wid -> unit_name that first claimed it
    required_keys = {"unit_name", "wikidata_ids", "tier", "duration_minutes"}

    for u in units:
        name = u.get("unit_name", "<unnamed>")

        # required keys
        missing = required_keys - set(u.keys())
        if missing:
            problems.append(f"{name}: missing keys {sorted(missing)}")

        # tier legality
        tier = u.get("tier")
        if tier not in VALID_TIERS:
            problems.append(f"{name}: unknown tier '{tier}'")

        # scheduled units must have a duration
        if tier in SCHEDULED_TIERS and not u.get("duration_minutes"):
            problems.append(
                f"{name}: tier='{tier}' but duration_minutes is null/zero — "
                f"will break packer arithmetic"
            )

        # duplicate wikidata_ids
        for wid in u.get("wikidata_ids", []):
            if wid in seen_wids:
                problems.append(
                    f"Duplicate wikidata_id {wid}: claimed by both "
                    f"'{seen_wids[wid]}' and '{name}'"
                )
            else:
                seen_wids[wid] = name

    return problems


def tier_summary(label: str, units: list):
    counts: dict[str, int] = {}
    for u in units:
        t = u.get("tier", "?")
        counts[t] = counts.get(t, 0) + 1
    summary = ", ".join(f"{t}={n}" for t, n in sorted(counts.items()))
    print(f"    tiers: {summary}")


def merge(args):
    print("\n=== Merging Baku unit files ===\n")

    sources = [
        (args.icherisheher, "Icherisheher"),
        (args.flame_towers, "Flame Towers"),
        (args.opera,        "Opera and Ballet"),
    ]

    merged = []
    for path_str, label in sources:
        path = Path(path_str)
        units = load_json(path, label)
        tier_summary(label, units)
        merged += units

    # Heydar Aliyev — manually authored, always appended
    print(f"  Heydar Aliyev Center: {len(HEYDAR_UNITS)} units (manually authored)")
    tier_summary("Heydar Aliyev Center", HEYDAR_UNITS)
    merged += HEYDAR_UNITS

    print(f"\nTotal before validation: {len(merged)} units")

    # ---------- validation ----------
    problems = validate(merged)
    if problems:
        print("\nVALIDATION FAILED — fix these before feeding to the packer:")
        for p in problems:
            print(f"  - {p}")
        print("\nWriting output anyway for inspection; treat as INCOMPLETE.")
    else:
        print("Validation: clean — no duplicate IDs, no null scheduled durations.")

    # ---------- write ----------
    out = Path(args.out)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    # ---------- final summary ----------
    tiers: dict[str, int] = {}
    for u in merged:
        t = u.get("tier", "?")
        tiers[t] = tiers.get(t, 0) + 1

    print(f"\nWrote {len(merged)} units -> {out}")
    print(f"  overall tiers: {dict(sorted(tiers.items()))}")
    scheduled = tiers.get("primary", 0) + tiers.get("secondary", 0)
    print(f"  scheduled (primary+secondary): {scheduled} units")
    print(f"\nNext step:")
    print(f"  python pack_day.py --json_name {out} --start_point \"<lat,lng>\"")


def main():
    ap = argparse.ArgumentParser(
        description="Merge per-anchor unit JSONs into one baku_all_units.json for the packer."
    )
    ap.add_argument("--icherisheher", default=DEFAULTS["icherisheher"],
                    help=f"Icherisheher units JSON (default: {DEFAULTS['icherisheher']})")
    ap.add_argument("--flame_towers", default=DEFAULTS["flame_towers"],
                    help=f"Flame Towers units JSON (default: {DEFAULTS['flame_towers']})")
    ap.add_argument("--opera",        default=DEFAULTS["opera"],
                    help=f"Opera and Ballet units JSON (default: {DEFAULTS['opera']})")
    ap.add_argument("--out",          default=DEFAULTS["out"],
                    help=f"Output path (default: {DEFAULTS['out']})")
    args = ap.parse_args()
    merge(args)


if __name__ == "__main__":
    main()