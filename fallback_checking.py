import pandas as pd
df = pd.read_csv("baku_landmarks_clean.csv")
print([c for c in df.columns if "wiki" in c.lower()])
print(df[["azwiki_title", "ruwiki_title"]].notna().sum())