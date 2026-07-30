# Loading baku_amenities_places.csv into `amenities_places`

`load_places.sql` and `baku_amenities_places.csv` must sit in the **same directory**,
and you run `psql` **from that directory** (the `\copy` path is relative).

## Command

```bash
cd <dir containing load_places.sql and baku_amenities_places.csv>
psql "$DATABASE_URL" -f load_places.sql
```

`$DATABASE_URL` must be the Supabase **session pooler** string
(`...pooler.supabase.com`, user `postgres.<project-ref>`) — the direct connection is
IPv6-only and fails on IPv4 networks. On Windows PowerShell use `$env:DATABASE_URL`:

```powershell
psql $env:DATABASE_URL -f load_places.sql
```

## What it does / safety

- Creates `amenities_places` (and the PostGIS extension + GIST index) only if absent.
- Imports the CSV into a temp staging table via client-side `\copy`, then upserts with
  `ON CONFLICT (place_id) DO NOTHING` and builds `geom` (longitude-first).
- **Idempotent** — re-running imports nothing new (existing `place_id`s are skipped) and
  leaves already-loaded rows untouched.
- Never references or modifies the OSM `amenities` table.

## Verify after loading

```sql
SELECT count(*)                                    AS rows,
       count(*) FILTER (WHERE geom IS NOT NULL)     AS with_geom,
       count(*) FILTER (WHERE coverage = 'partial') AS partial
FROM amenities_places;
```

Expect ~6,563 rows (minus any duplicate `place_id`s in the CSV).
