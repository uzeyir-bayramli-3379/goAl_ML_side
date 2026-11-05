import os
from pathlib import Path
import psycopg2
from dotenv import load_dotenv

# Load environment variables from parent directory
load_dotenv(Path("../.env"))

# Connect to the database using DATABASE_URL
conn = psycopg2.connect(os.environ["DATABASE_URL"])

try:
    with conn.cursor() as cur:
        # Execute the PostGIS query
        cur.execute(
            """
            SELECT name, ST_Y(geom::geometry), ST_X(geom::geometry), radius_m 
            FROM anchors;
        """
        )
        for row in cur.fetchall():
            print(row)
finally:
    conn.close()
