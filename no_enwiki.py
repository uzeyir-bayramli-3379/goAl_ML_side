import pandas as pd

df = pd.read_csv("baku_landmarks_clean.csv")
missing = df[df["enwiki_title"].isna()]

print(f"{len(missing)} rows with no enwiki_title:\n")
for _, row in missing.iterrows():
    print(row["Wikidata_ID"], "-", row["Name"])
