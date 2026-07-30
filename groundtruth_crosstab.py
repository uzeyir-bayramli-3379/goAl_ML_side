"""
Part B: ground-truth cross-tab, CSV only, no database.

Matches each hand-checked ground_truth_50.csv row against:
  - baku_amenities_places.csv (Google Places API New, current source)
  - baku_amenities_clean.csv  (OSM, superseded baseline)

by name similarity + proximity (~50m). Writes places_vs_osm_groundtruth.md.

Rules (see task spec):
  - EXCLUDE `unreliable` coverage rows from the Places side entirely.
  - EXCLUDE `chain` ground-truth rows from all scoring.
  - `renamed` is a distinct fourth bucket: proximity-only match (ignore stale
    Name), then check the matched name against `current_name`.
  - Flag ambiguous matches instead of forcing them.
Reads only; modifies nothing.
"""

import csv
import math
import unicodedata
from difflib import SequenceMatcher

HERE = __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0]
GT_CSV = f"{HERE}/ground_truth_50.csv"
PLACES_CSV = f"{HERE}/baku_amenities_places.csv"
OSM_CSV = f"{HERE}/baku_amenities_clean.csv"
OUT_MD = f"{HERE}/places_vs_osm_groundtruth.md"

PROX_M = 50.0        # primary proximity gate
AMBIG_M = 80.0       # widened radius that only flags, never confirms
NAME_HIT = 0.60      # name ratio for a confident name+proximity match
NAME_MAYBE = 0.45    # name ratio floor for "ambiguous, flag it"


def norm(s):
    """Fold case, strip diacritics/emoji/punctuation, collapse spaces."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.casefold()
    out = [c if (c.isalnum() or c.isspace()) else " " for c in s]
    return " ".join("".join(out).split())


def ratio(a, b):
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def haversine_m(lat1, lng1, lat2, lng2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_places():
    rows = []
    with open(PLACES_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["coverage"].strip() == "unreliable":   # excluded entirely
                continue
            try:
                r["_lat"] = float(r["lat"]); r["_lng"] = float(r["lng"])
            except (ValueError, KeyError):
                continue
            r["_name"] = r["name"]
            rows.append(r)
    return rows


def load_osm():
    rows = []
    with open(OSM_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                r["_lat"] = float(r["Latitude"]); r["_lng"] = float(r["Longitude"])
            except (ValueError, KeyError):
                continue
            r["_name"] = r["Name"]
            rows.append(r)
    return rows


def load_gt():
    rows = []
    with open(GT_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "row": r["row"].strip(),
                "name": r["Name"].strip(),
                "lat": float(r["Latitude"]),
                "lng": float(r["Longitude"]),
                "verdict": r["verdict"].strip().lower(),
                "current_name": (r.get("current_name") or "").strip(),
            })
    return rows


def candidates(gt, source, radius):
    """All source rows within `radius` metres of gt, nearest first, with name ratio."""
    out = []
    for s in source:
        d = haversine_m(gt["lat"], gt["lng"], s["_lat"], s["_lng"])
        if d <= radius:
            out.append((d, ratio(gt["name"], s["_name"]), s))
    out.sort(key=lambda t: t[0])
    return out


def match_name_prox(gt, source):
    """
    Name+proximity match. Returns (status, detail).
    status in {'match', 'ambiguous', 'miss'}.
    """
    cands = candidates(gt, source, AMBIG_M)
    # confident: within PROX_M and a good name hit
    for d, rat, s in cands:
        if d <= PROX_M and rat >= NAME_HIT:
            return "match", (d, rat, s)
    # ambiguous: good name but slightly too far, or close but middling name
    for d, rat, s in cands:
        if (PROX_M < d <= AMBIG_M and rat >= NAME_HIT) or \
           (d <= PROX_M and NAME_MAYBE <= rat < NAME_HIT):
            return "ambiguous", (d, rat, s)
    return "miss", None


def main():
    gt = load_gt()
    places = load_places()
    osm = load_osm()

    buckets = {}
    for g in gt:
        buckets.setdefault(g["verdict"], []).append(g)

    lines = []
    w = lines.append

    w("# Places vs OSM — ground-truth cross-tab (50 hand-checked places, Icherisheher)\n")
    w("Source: `ground_truth_50.csv`. Places side = `baku_amenities_places.csv` "
      "(pass 0 + pass 1, `unreliable` coverage excluded). "
      "OSM baseline = `baku_amenities_clean.csv`.\n")
    w(f"Matching: name similarity + proximity. Confident match = within {PROX_M:.0f} m "
      f"AND name ratio >= {NAME_HIT:.2f}. Ambiguous (flagged, not counted as match) = "
      f"good name within {AMBIG_M:.0f} m, or close but weak name.\n")
    w("> **Read the OSM baseline as near-tautological.** The 50 ground-truth rows were "
      "sampled *from* OSM (their name/coords are the OSM records I hand-checked), so OSM "
      "trivially \"matches\" nearly everything at 0 m. The point is not that OSM finds more — "
      "it is *what* it finds: OSM keeps listing the closed venues (staleness), while Places "
      "surfaces almost none of the closed ones. Compare the two on the **closed** and "
      "**unverifiable** rows, not on raw match counts.\n")

    counts = {k: len(v) for k, v in buckets.items()}
    w("## Bucket sizes\n")
    for k in ("operational", "closed", "unverifiable", "renamed", "chain"):
        note = " (excluded from all scoring)" if k == "chain" else ""
        w(f"- **{k}**: {counts.get(k, 0)}{note}")
    w("")

    def score_bucket(rows, source):
        res = {"match": [], "ambiguous": [], "miss": []}
        for g in rows:
            st, detail = match_name_prox(g, source)
            res[st].append((g, detail))
        return res

    # ---- operational ----
    w("## operational — is it found?\n")
    for label, source in (("amenities_places (Places)", places), ("amenities (OSM baseline)", osm)):
        r = score_bucket(buckets.get("operational", []), source)
        w(f"### {label}")
        w(f"- matched: **{len(r['match'])} / {len(buckets.get('operational', []))}**  "
          f"| ambiguous (flagged): {len(r['ambiguous'])} | missed: {len(r['miss'])}")
        if source is places:
            cov = {"complete": 0, "partial": 0, "other": 0}
            for g, (d, rat, s) in r["match"]:
                cov[s.get("coverage", "other") if s.get("coverage") in cov else "other"] += 1
            w(f"  - matched-side coverage: complete={cov['complete']}, partial={cov['partial']}"
              + (f", other={cov['other']}" if cov["other"] else ""))
        for g, det in r["ambiguous"]:
            d, rat, s = det
            w(f"  - FLAG ambiguous: GT `{g['name']}` ~ `{s['_name']}` "
              f"(dist={d:.0f} m, name={rat:.2f})")
        for g, det in r["miss"]:
            w(f"  - miss: GT `{g['name']}`")
        w("")

    # ---- closed (staleness: still listed?) ----
    w("## closed — does the source still list it? (staleness — lower is better)\n")
    for label, source in (("amenities_places (Places)", places), ("amenities (OSM baseline)", osm)):
        r = score_bucket(buckets.get("closed", []), source)
        w(f"### {label}")
        w(f"- still present: **{len(r['match'])} / {len(buckets.get('closed', []))}**  "
          f"| ambiguous: {len(r['ambiguous'])} | correctly absent: {len(r['miss'])}")
        for g, det in r["match"]:
            d, rat, s = det
            extra = f", coverage={s.get('coverage')}" if source is places else ""
            w(f"  - STALE: closed `{g['name']}` still listed as `{s['_name']}` "
              f"(dist={d:.0f} m, name={rat:.2f}{extra})")
        for g, det in r["ambiguous"]:
            d, rat, s = det
            w(f"  - FLAG ambiguous: closed `{g['name']}` ~ `{s['_name']}` "
              f"(dist={d:.0f} m, name={rat:.2f})")
        w("")

    # ---- unverifiable (do they turn up now?) ----
    w("## unverifiable — turned up in the fuller grid?\n")
    w("Hand-check found no Google record for these. Any Places match is a change from the "
      "earlier 50-place spot check.\n")
    r = score_bucket(buckets.get("unverifiable", []), places)
    w(f"### amenities_places (Places)")
    w(f"- now present: **{len(r['match'])} / {len(buckets.get('unverifiable', []))}**  "
      f"| ambiguous: {len(r['ambiguous'])} | still absent: {len(r['miss'])}")
    for g, det in r["match"]:
        d, rat, s = det
        w(f"  - CHANGE (now found): `{g['name']}` -> `{s['_name']}` "
          f"(dist={d:.0f} m, name={rat:.2f}, coverage={s.get('coverage')})")
    for g, det in r["ambiguous"]:
        d, rat, s = det
        w(f"  - FLAG ambiguous: `{g['name']}` ~ `{s['_name']}` (dist={d:.0f} m, name={rat:.2f})")
    w("")

    # ---- renamed (proximity-only, then name-vs-current_name) ----
    w("## renamed — proximity-only match, then name vs `current_name`\n")
    w("Old `Name` is known-stale, so match on location alone (~50 m). A hit means the spot "
      "is covered; comparing the found name to `current_name` tells us if it's the renamed venue.\n")
    w("Among all candidates within 50 m we pick the one whose name is closest to "
      "`current_name` (NOT merely the nearest) — dense clusters otherwise pin the wrong "
      "neighbour. Nearest-neighbour is still shown when it differs.\n")
    ren = buckets.get("renamed", [])
    for label, source in (("amenities_places (Places)", places), ("amenities (OSM baseline)", osm)):
        found, missed = [], []
        for g in ren:
            cands = candidates(g, source, PROX_M)   # (dist, name~oldName, row), nearest first
            if not cands:
                missed.append(g)
            else:
                best = max(cands, key=lambda t: ratio(g["current_name"], t[2]["_name"]))
                found.append((g, best, cands[0]))
        w(f"### {label}")
        w(f"- covered by proximity (a venue within 50 m): **{len(found)} / {len(ren)}**")
        w(f"- missed even on proximity (real gap — venue moved or not fetched): **{len(missed)}**")
        for g, (d, _ro, s), (dn, _rn, sn) in found:
            rat_cur = ratio(g["current_name"], s["_name"])
            verdict = "matches current_name" if rat_cur >= NAME_HIT else \
                      ("weak vs current_name" if rat_cur >= NAME_MAYBE else "name differs from current_name")
            extra = f", coverage={s.get('coverage')}" if source is places else ""
            w(f"  - `{g['name']}` -> current `{g['current_name']}` | best-name match `{s['_name']}` "
              f"(dist={d:.0f} m, name~current={rat_cur:.2f}: {verdict}{extra})")
            if sn["_name"] != s["_name"]:
                w(f"      (nearest within 50 m was a different venue: `{sn['_name']}` at {dn:.0f} m)")
        for g in missed:
            w(f"  - MISS (proximity gap): `{g['name']}` (current `{g['current_name']}`)")
        w("")

    text = "\n".join(lines) + "\n"
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    main()
