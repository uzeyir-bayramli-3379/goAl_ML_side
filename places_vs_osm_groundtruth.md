# Places vs OSM — ground-truth cross-tab (50 hand-checked places, Icherisheher)

Source: `ground_truth_50.csv`. Places side = `baku_amenities_places.csv` (pass 0 + pass 1, `unreliable` coverage excluded). OSM baseline = `baku_amenities_clean.csv`.

Matching: name similarity + proximity. Confident match = within 50 m AND name ratio >= 0.60. Ambiguous (flagged, not counted as match) = good name within 80 m, or close but weak name.

> **Read the OSM baseline as near-tautological.** The 50 ground-truth rows were sampled *from* OSM (their name/coords are the OSM records I hand-checked), so OSM trivially "matches" nearly everything at 0 m. The point is not that OSM finds more — it is *what* it finds: OSM keeps listing the closed venues (staleness), while Places surfaces almost none of the closed ones. Compare the two on the **closed** and **unverifiable** rows, not on raw match counts.

## Bucket sizes

- **operational**: 22
- **closed**: 6
- **unverifiable**: 12
- **renamed**: 7
- **chain**: 3 (excluded from all scoring)

## operational — is it found?

### amenities_places (Places)
- matched: **13 / 22**  | ambiguous (flagged): 4 | missed: 5
  - matched-side coverage: complete=0, partial=13
  - FLAG ambiguous: GT `Chay Baghi 145` ~ `Tea Garden 145` (dist=2 m, name=0.50)
  - FLAG ambiguous: GT `Tatell Baku` ~ `Nare Cake` (dist=33 m, name=0.50)
  - FLAG ambiguous: GT `Qaynana` ~ `Qaynana Restaurant` (dist=3 m, name=0.56)
  - FLAG ambiguous: GT `Moonlight` ~ `Moonlight Hotel & Restaurant Baku` (dist=3 m, name=0.45)
  - miss: GT `Best Place`
  - miss: GT `bürc qala`
  - miss: GT `Khan's Garden (Baku)`
  - miss: GT `Azim Azimzadeh Park / Archeological Park`
  - miss: GT `Art Café Mayak-13`

### amenities (OSM baseline)
- matched: **22 / 22**  | ambiguous (flagged): 0 | missed: 0

## closed — does the source still list it? (staleness — lower is better)

### amenities_places (Places)
- still present: **0 / 6**  | ambiguous: 3 | correctly absent: 3
  - FLAG ambiguous: closed `🇮🇹 Volare İtalian Cuisine` ~ `Le Pain Quotidien` (dist=32 m, name=0.51)
  - FLAG ambiguous: closed `Kafe Gallery` ~ `Galateya` (dist=37 m, name=0.50)
  - FLAG ambiguous: closed `Els Book Cafe` ~ `İçərişəhər Bookhouse & Cafe` (dist=4 m, name=0.58)

### amenities (OSM baseline)
- still present: **6 / 6**  | ambiguous: 0 | correctly absent: 0
  - STALE: closed `Böyük Manqal` still listed as `Böyük Manqal` (dist=0 m, name=1.00)
  - STALE: closed `🇮🇹 Volare İtalian Cuisine` still listed as `🇮🇹 Volare İtalian Cuisine` (dist=0 m, name=1.00)
  - STALE: closed `Kafe Gallery` still listed as `Kafe Gallery` (dist=0 m, name=1.00)
  - STALE: closed `Cantinetta Antinori` still listed as `Cantinetta Antinori` (dist=0 m, name=1.00)
  - STALE: closed `Els Book Cafe` still listed as `Els Book Cafe` (dist=0 m, name=1.00)
  - STALE: closed `Dolce Vita Kafe` still listed as `Dolce Vita Kafe` (dist=0 m, name=1.00)

## unverifiable — turned up in the fuller grid?

Hand-check found no Google record for these. Any Places match is a change from the earlier 50-place spot check.

### amenities_places (Places)
- now present: **0 / 12**  | ambiguous: 1 | still absent: 11
  - FLAG ambiguous: `cheers cafe baku` ~ `Almond cake&bakery` (dist=40 m, name=0.47)

## renamed — proximity-only match, then name vs `current_name`

Old `Name` is known-stale, so match on location alone (~50 m). A hit means the spot is covered; comparing the found name to `current_name` tells us if it's the renamed venue.

Among all candidates within 50 m we pick the one whose name is closest to `current_name` (NOT merely the nearest) — dense clusters otherwise pin the wrong neighbour. Nearest-neighbour is still shown when it differs.

### amenities_places (Places)
- covered by proximity (a venue within 50 m): **7 / 7**
- missed even on proximity (real gap — venue moved or not fetched): **0**
  - `Terrace Garden` -> current `Terrace 145` | best-name match `Terrace 145` (dist=9 m, name~current=1.00: matches current_name, coverage=partial)
  - `West End` -> current `West inn restaurant` | best-name match `16th. Floor Coffee` (dist=44 m, name~current=0.11: name differs from current_name, coverage=partial)
      (nearest within 50 m was a different venue: `Cay bala cafe` at 31 m)
  - `Çay Bağı` -> current `Chay Baghi 145` | best-name match `Tea Garden 145` (dist=14 m, name~current=0.50: weak vs current_name, coverage=partial)
  - `Rast` -> current `Rast restourant lounge` | best-name match `Mugam Klub Restaurant` (dist=46 m, name~current=0.51: weak vs current_name, coverage=partial)
      (nearest within 50 m was a different venue: `Art's Demon CLUB` at 18 m)
  - `Mangal` -> current `Mangal Old` | best-name match `Manqal Old` (dist=5 m, name~current=0.90: matches current_name, coverage=partial)
  - `Buta` -> current `Salam Baku` | best-name match `Salam Baku Restaurant` (dist=18 m, name~current=0.65: matches current_name, coverage=partial)
      (nearest within 50 m was a different venue: `Manqal Old` at 14 m)
  - `Ağ Liliyalar Fontan Bağı` -> current `White Fountain Park` | best-name match `White Fountain Park` (dist=15 m, name~current=1.00: matches current_name, coverage=partial)

### amenities (OSM baseline)
- covered by proximity (a venue within 50 m): **7 / 7**
- missed even on proximity (real gap — venue moved or not fetched): **0**
  - `Terrace Garden` -> current `Terrace 145` | best-name match `Terrace Garden` (dist=0 m, name~current=0.64: matches current_name)
  - `West End` -> current `West inn restaurant` | best-name match `West End` (dist=0 m, name~current=0.44: name differs from current_name)
  - `Çay Bağı` -> current `Chay Baghi 145` | best-name match `Chay Baghi 145` (dist=12 m, name~current=1.00: matches current_name)
      (nearest within 50 m was a different venue: `Çay Bağı` at 0 m)
  - `Rast` -> current `Rast restourant lounge` | best-name match `Mugam Club Restaurant` (dist=36 m, name~current=0.51: weak vs current_name)
      (nearest within 50 m was a different venue: `Rast` at 0 m)
  - `Mangal` -> current `Mangal Old` | best-name match `Mangal` (dist=0 m, name~current=0.75: matches current_name)
  - `Buta` -> current `Salam Baku` | best-name match `Qaynana` (dist=14 m, name~current=0.35: name differs from current_name)
      (nearest within 50 m was a different venue: `Buta` at 0 m)
  - `Ağ Liliyalar Fontan Bağı` -> current `White Fountain Park` | best-name match `Ağ Liliyalar Fontan Bağı` (dist=0 m, name~current=0.47: weak vs current_name)

