import pandas as pd
df = pd.read_csv('baku_amenities_places.csv')
print(len(df), '->', df['place_id'].nunique())
df.drop_duplicates(subset='place_id', keep='first').to_csv('baku_amenities_places_dedup.csv', index=False)