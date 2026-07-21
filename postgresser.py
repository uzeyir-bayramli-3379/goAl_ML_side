import os
import csv
import time
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values
from google import genai
from google.genai import types

load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()

EMB_MODEL = "gemini-embedding-001"


# ---------- 1. LOAD LANDMARKS ----------
with open("baku_landmarks_clean.csv", newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    rows = []
    for r in reader:
        rows.append((
            r["Wikidata_ID"], r["Name"],
            r["Latitude"] or None, r["Longitude"] or None,
            r["WKT"] or None, r["Sitelinks"] or None,
            r["Primary_Class"] or None, r["All_Types"] or None,
            r["inception"] or None, r["official_opening"] or None,
            r["height_m"] or None, r["architectural_style"] or None,
            r["architect"] or None, r["founded_by"] or None,
            r["creator"] or None, r["material_used"] or None,
            r["named_after"] or None, r["heritage_designation"] or None,
            r["image"] or None, r["description"] or None,
            r["enwiki_title"] or None, r["enwiki_url"] or None,
            r["core_zone"] or None, r["coord_group"] or None,
            r["type_tag"] or None,
        ))

execute_values(cur, """
    INSERT INTO landmarks (
        wikidata_id, name, latitude, longitude, wkt, sitelinks,
        primary_class, all_types, inception, official_opening, height_m,
        architectural_style, architect, founded_by, creator, material_used,
        named_after, heritage_designation, image, description,
        enwiki_title, enwiki_url, core_zone, coord_group, type_tag
    ) VALUES %s
    ON CONFLICT (wikidata_id) DO NOTHING
""", rows)
conn.commit()
print(f"Loaded {len(rows)} landmarks")

# ---------- 2. BUILD GEOM ----------
cur.execute("""
    UPDATE landmarks
    SET geom = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography
    WHERE geom IS NULL AND longitude IS NOT NULL AND latitude IS NOT NULL
""")
conn.commit()

# ---------- 3. EMBED ----------

def embed_text(text, task_type):
    resp = client.models.embed_content(
        model=EMB_MODEL,
        contents=text,
        config=types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=768,
        ),
    )
    return resp.embeddings[0].values
cur.execute("""
    SELECT wikidata_id, name, description, primary_class, architectural_style
    FROM landmarks WHERE embedding IS NULL
""")
to_embed = cur.fetchall()

for wid, name, desc, pclass, style in to_embed:
    parts = [p for p in [name, desc, pclass, style] if p]   # deterministic, skip NULLs
    text = " | ".join(parts)
    try:
        vec = embed_text(text, "RETRIEVAL_DOCUMENT")
        cur.execute(
            "UPDATE landmarks SET embedding = %s WHERE wikidata_id = %s",
            (vec, wid)
        )
        conn.commit()
    except Exception as e:
        print(f"Failed {wid} ({name}): {e}")
        time.sleep(2)  # back off on rate limit, then move on

print("Embedding pass done")
cur.close()
conn.close()