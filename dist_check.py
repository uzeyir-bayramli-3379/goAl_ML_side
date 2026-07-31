import pandas as pd
df = pd.read_csv('baku_landmarks_clean.csv')
s = df["wiki_extract"].dropna().str.len()
print(f"n={len(s)}  p25={s.quantile(.25):.0f}  median={s.median():.0f}  p75={s.quantile(.75):.0f}")