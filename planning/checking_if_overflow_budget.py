import json

# Open and load the itinerary JSON file
with open("final_multi_day_itinerary.json") as f:
    days = json.load(f)

# Loop through each day to print the summary
for day_key, day_data in days.items():
    stops = [i for i in day_data["timeline"] if i["type"] == "stop"]
    anchors = set(s.get("anchor", "?") for s in stops)

    # Fixed the escaped quote syntax error here
    print(
        f"{day_key}: {len(stops)} stops | {day_data['total_duration_minutes']}m | anchors={anchors}"
    )
