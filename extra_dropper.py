"""Build a clean amenities CSV: all tourist_attraction rows plus a hand-picked
set of shopping malls (given by spreadsheet row number, where the header is
row 1 -> pandas index = row - 2)."""

import pandas as pd

SRC = "baku_amenities_extra.csv"
OUT = "baku_amenities_extra_clean.csv"

# Spreadsheet row numbers (header = row 1) of the malls to keep.
KEEP_MALL_ROWS = [10, 53, 56, 63, 64, 65, 93, 123]

df = pd.read_csv(SRC)

attractions = df[df["category"] == "tourist_attraction"]
malls = df.loc[[r - 2 for r in KEEP_MALL_ROWS]]

clean = pd.concat([attractions, malls]).sort_index()
clean.to_csv(OUT, index=False)

print(f"{len(attractions)} tourist_attraction + {len(malls)} malls "
      f"= {len(clean)} rows -> {OUT}")
