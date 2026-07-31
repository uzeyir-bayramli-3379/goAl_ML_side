import json
from collections import Counter

langs = Counter()
seen = set()
dupes = 0
with open("wiki_extracts_multilang.jsonl", encoding="utf-8") as fh:
    for line in fh:
        if not line.strip():
            continue
        rec = json.loads(line)
        key = rec.get("title") or rec.get("key")
        lang = key.split(":", 1)[0] if ":" in key else "?"
        langs[lang] += 1
        if key in seen:
            dupes += 1
        seen.add(key)

print(langs, f"unique={len(seen)} dupes={dupes}")
print(open("wiki_extracts_multilang.jsonl", encoding="utf-8").readline())