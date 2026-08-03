import pandas as pd

df = pd.read_csv("baku_amenities_places.csv")
total = len(df)
unreliable = (df["coverage"] == "unreliable").sum()

print(f"unreliable: {unreliable}/{total} ({unreliable / total * 100:.1f}%)")
