import requests
import pandas as pd
import re
import sys

CITY_ID = "Q9248"  # Baku
SPARQL_URL = "https://query.wikidata.org/sparql"
MIN_SITELINKS = 2  # notability gate — the class list is coarse, THIS does the real work.
                   # Eyeball 2 vs 3 for Baku; 2 may let minor az+en items through.

# Root classes we match, in priority order for choosing a single primary category
# when an item matches several. Earliest = highest priority. The two broad roots
# (building / architectural structure) are last on purpose so a mosque reads as
# "mosque" not "building". QIDs verified against Wikidata 2026-07-19. The SPARQL
# VALUES clause and the display labels are both derived from this dict, so the two
# can't drift out of sync.
CLASS_LABELS = {
    "Q33506":   "museum",
    "Q4989906": "monument",
    "Q12280":   "bridge",
    "Q2977":    "cathedral",
    "Q32815":   "mosque",
    "Q174782":  "square",
    "Q570116":  "tourist attraction",
    "Q41176":   "building",                  # broad
    "Q811979":  "architectural structure",   # broad
}
CLASS_PRIORITY = list(CLASS_LABELS)
VALUES_CLAUSE = " ".join(f"wd:{q}" for q in CLASS_LABELS)

query = f"""
SELECT DISTINCT ?item ?itemLabel ?typeLabel ?class ?coord ?sitelinks WHERE {{
    ?item wdt:P131+ wd:{CITY_ID} ;          # + not * : must be CONTAINED in Baku, not be Baku
          wdt:P625 ?coord ;
          wdt:P31 ?type ;
          wikibase:sitelinks ?sitelinks .
    ?type wdt:P279* ?class .
    VALUES ?class {{ {VALUES_CLAUSE} }}
    FILTER(?sitelinks >= {MIN_SITELINKS})
    SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,az,ru". }}
}}
ORDER BY DESC(?sitelinks)
"""

headers = {
    "User-Agent": "GoAI/1.0 (https://github.com/uzeyir-bayramli-3379; your@email.com)",
    "Accept": "application/sparql-results+json",
}

def discover_landmarks(verbose=True):
    """Run the discovery SPARQL and return the resolved `landmarks` dict keyed by
    QID (Name, coords, WKT, Sitelinks, Primary_Class, All_Types). This is the
    skeleton the enrichment stage keys off of."""
    try:
        r = requests.get(SPARQL_URL, params={"query": query, "format": "json"},
                         headers=headers, timeout=60)

        if r.status_code == 429:
            print(f"Rate limited. Retry-After: {r.headers.get('Retry-After')}s")
            sys.exit(1)
        if not r.ok:
            # WDQS timeouts come back as 500 with a Java exception in the body,
            # not a 429 — surface it instead of letting it look like a generic fail.
            print(f"HTTP {r.status_code}. Body (first 500 chars):\n{r.text[:500]}")
            sys.exit(1)

        try:
            rows = r.json()["results"]["bindings"]
        except (ValueError, KeyError) as e:
            print(f"Unexpected response shape: {e}\nBody:\n{r.text[:500]}")
            sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
        sys.exit(1)

    # Accumulate per QID: one item can appear on many rows (multiple types/classes).
    landmarks = {}
    for row in rows:
        qid = row["item"]["value"].split("/")[-1]
        name = row["itemLabel"]["value"]
        if name.startswith("Q") and name[1:].isdigit():
            continue  # unlabelled item

        if qid not in landmarks:
            wkt = row["coord"]["value"]  # "Point(LONG LAT)" — longitude first
            m = re.match(r"Point\(([-\d.]+) ([-\d.]+)\)", wkt)
            lon, lat = (float(m.group(1)), float(m.group(2))) if m else (None, None)
            landmarks[qid] = {
                "Wikidata_ID": qid,
                "Name": name,
                "Latitude": lat,
                "Longitude": lon,
                "WKT": wkt,          # keep raw: PostGIS ingests directly
                "_types": set(),     # specific P31 labels
                "_classes": set(),   # matched root class QIDs (for priority pick)
                "Sitelinks": int(row["sitelinks"]["value"]),
            }

        landmarks[qid]["_types"].add(row.get("typeLabel", {}).get("value", "Unknown"))
        landmarks[qid]["_classes"].add(row["class"]["value"].split("/")[-1])

    # Resolve deterministic category fields.
    for lm in landmarks.values():
        matched = [c for c in CLASS_PRIORITY if c in lm["_classes"]]
        lm["Primary_Class"] = CLASS_LABELS[matched[0]] if matched else "unknown"
        lm["All_Types"] = ", ".join(sorted(lm["_types"]))  # sorted => stable across runs
        del lm["_types"], lm["_classes"]

    if verbose:
        print(f"Discovered {len(landmarks)} unique Baku landmarks.")
    return landmarks


if __name__ == "__main__":
    landmarks = discover_landmarks()
    df = pd.DataFrame(landmarks.values())
    print(df.to_string(index=False))
    # df.to_csv("baku_landmarks.csv", index=False)