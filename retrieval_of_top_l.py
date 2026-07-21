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


def retrieve_landmarks(question, user_lat, user_lng, radius_m=2000, k=5):
    # 1. embed the question — RETRIEVAL_QUERY, not DOCUMENT
    resp = client.models.embed_content(
        model="gemini-embedding-001",
        contents=question,
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY",
            output_dimensionality=768,
        ),
    )
    qvec = resp.embeddings[0].values

    # 2. geo filter → vector rerank
    W_VEC = 0.7
    W_GEO = 0.3

    cur.execute("""
        SELECT wikidata_id, name, description, primary_class,
               ST_Distance(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) AS meters_away,
               embedding <=> %s::vector AS vec_dist
        FROM landmarks
        WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
        ORDER BY (
            %s * (embedding <=> %s::vector) +
            %s * (ST_Distance(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) / %s)
        )
        LIMIT %s
    """, (
        user_lng, user_lat,      # SELECT distance
        qvec,                    # SELECT vec_dist
        user_lng, user_lat, radius_m,   # WHERE geo filter
        W_VEC, qvec,             # ORDER weighted vector term
        W_GEO, user_lng, user_lat, radius_m,   # ORDER weighted geo term
        k,                       # LIMIT
    ))
    return cur.fetchall()

results = retrieve_landmarks(
    "old historic tower in the walled city",
    user_lat=40.343677073909134, user_lng=49.848291906565315,   # actual Old City coords this time
    radius_m=2000,
)
for r in results:
    print(f"{r[1]:35} | {r[4]:.0f}m | vec={r[5]:.4f}")


def build_context(rows):
    """Turn retrieved DB rows into a grounded context block."""
    blocks = []
    for r in rows:
        wid, name, desc, pclass, meters, vec = r
        blocks.append(
            f"- {name} ({pclass}), ~{meters:.0f}m away\n"
            f"  {desc or 'No description available.'}"
        )
    return "\n".join(blocks)


SYSTEM_PROMPT = """You are a Baku city guide. Answer the user's question using ONLY the landmark information provided in the context below.

Rules:
- Use only facts present in the context. Do not add landmarks, history, or details from your own knowledge.
- If the context doesn't contain enough to answer, say so plainly rather than guessing.
- Be concise and conversational, like a local pointing things out.
- You may mention distances to help orient the user.
"""


def answer_question(question, user_lat, user_lng, radius_m=2000, k=5):
    rows = retrieve_landmarks(question, user_lat, user_lng, radius_m, k)

    if not rows:
        return "I don't see any landmarks near you within that range."

    context = build_context(rows)

    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=f"Context (nearby landmarks):\n{context}\n\nQuestion: {question}",
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.3,
        ),
    )
    return resp.text

print(answer_question(
    "what modern landmarks are near me?",
    user_lat=40.343677073909134, user_lng=49.848291906565315,
))