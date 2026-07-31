import pandas as pd
df = pd.read_csv("baku_landmarks_clean.csv")
print(df[["azwiki_title","ruwiki_title"]].notna().sum())
print(df.loc[df.Wikidata_ID=="Q29965119", ["enwiki_title","azwiki_title","ruwiki_title"]])