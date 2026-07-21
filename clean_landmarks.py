import re
import unicodedata
import pandas as pd

# --- Tunables -------------------------------------------------------------
MIN_SITELINKS = 4          # 2–3 band is where dupes + junk cluster; scan 4–6 before locking

# Hard sanity box: greater Baku + Absheron + offshore. Outside = mislocated → DROP.
# Catches European route E002 (39.2/46.9) and the Ganja/Yerevan strays.
SANITY_BOX = {"lat": (40.00, 40.65), "lon": (49.30, 50.90)}

# Soft core box: walkable tourist core. Inside = core_zone flag (NOT a drop).
CORE_BOX   = {"lat": (40.34, 40.44), "lon": (49.79, 49.90)}

# Types that become a `type_tag` for SQL-side filtering. NOT dropped —
# Baku Boulevard is tagged "street" but has 20 sitelinks and is a real landmark.
TRANSIT_HINTS = {"metro station", "underground station"}
STREET_HINTS  = {"street", "road", "avenue"}


def _norm_name(name: str) -> str:
    """Lowercase, strip diacritics + punctuation for duplicate detection.
    Deliberately conservative — collapses 'Palace of the Shirvanshahs' x2
    without merging genuinely distinct names."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    n = re.sub(r"[^a-z0-9 ]", " ", n.lower())
    return re.sub(r"\s+", " ", n).strip()


def _tag_type(all_types: str) -> str:
    toks = {t.strip().lower() for t in all_types.split(",")}
    if toks and toks <= TRANSIT_HINTS:       # ALL tokens are transit → pure metro entry
        return "transit"
    if toks and toks <= STREET_HINTS:        # ALL tokens are street/road → pure street
        return "street"
    if toks & TRANSIT_HINTS:
        return "transit_mixed"
    return "poi"


def _in_box(lat, lon, box) -> bool:
    return box["lat"][0] <= lat <= box["lat"][1] and box["lon"][0] <= lon <= box["lon"][1]


def clean_landmarks(landmarks: dict, min_sitelinks=MIN_SITELINKS, verbose=True):
    """Takes the resolved `landmarks` dict (keyed by QID) from the SPARQL script,
    returns (clean_df, report). Drops unambiguous junk; flags the rest."""
    kept, dropped = [], []

    def drop(rec, reason):
        dropped.append((rec["Wikidata_ID"], rec["Name"], reason))

    # --- Pass 1: hard drops -----------------------------------------------
    for rec in landmarks.values():
        name = rec["Name"]

        if name.startswith("Category:"):           # Commons category page, not a place
            drop(rec, "commons_category"); continue

        lat, lon = rec["Latitude"], rec["Longitude"]
        if lat is None or lon is None:             # coord regex failed to parse
            drop(rec, "null_coords"); continue

        if not _in_box(lat, lon, SANITY_BOX):      # mislocated (E002, Ganja, etc.)
            drop(rec, f"out_of_region ({lat:.3f},{lon:.3f})"); continue

        if rec["Sitelinks"] < min_sitelinks:       # below notability floor
            drop(rec, f"sitelinks<{min_sitelinks}"); continue

        kept.append(rec)

    # --- Pass 2: name-duplicate dedup (keep highest sitelinks) -------------
    by_name = {}
    for rec in kept:
        key = _norm_name(rec["Name"])
        if key not in by_name or rec["Sitelinks"] > by_name[key]["Sitelinks"]:
            if key in by_name:
                drop(by_name[key], f"name_dupe of {rec['Wikidata_ID']}")
            by_name[key] = rec
        else:
            drop(rec, f"name_dupe of {by_name[key]['Wikidata_ID']}")
    kept = list(by_name.values())

    # --- Pass 3: flags (no drops) -----------------------------------------
    # coord collisions: group items sharing a pin so you can inspect/nudge them.
    coord_counts = {}
    for rec in kept:
        ck = (round(rec["Latitude"], 6), round(rec["Longitude"], 6))
        coord_counts.setdefault(ck, []).append(rec["Wikidata_ID"])
    collisions = {ck: i + 1 for i, (ck, ids) in enumerate(coord_counts.items()) if len(ids) > 1}

    for rec in kept:
        ck = (round(rec["Latitude"], 6), round(rec["Longitude"], 6))
        rec["core_zone"]   = _in_box(rec["Latitude"], rec["Longitude"], CORE_BOX)
        rec["coord_group"] = collisions.get(ck)          # None = unique pin
        rec["type_tag"]    = _tag_type(rec["All_Types"])

    clean_df = pd.DataFrame(kept).sort_values("Sitelinks", ascending=False)
    report = pd.DataFrame(dropped, columns=["Wikidata_ID", "Name", "Reason"])

    if verbose:
        print(f"Kept {len(kept)} / {len(landmarks)}  ({len(dropped)} dropped)\n")
        print("Drops by reason:")
        print(report["Reason"].str.replace(r"\(.*\)|of Q\d+", "", regex=True)
                              .str.strip().value_counts().to_string(), "\n")
        print(f"core_zone: {clean_df['core_zone'].sum()} | "
              f"pin collisions: {len(collisions)} groups | "
              f"tags: {clean_df['type_tag'].value_counts().to_dict()}")

    return clean_df, report
