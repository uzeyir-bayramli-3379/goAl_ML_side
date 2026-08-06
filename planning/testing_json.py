import json

with open('timeline_no_meal.json', 'r', encoding='utf-8') as f:
    timeline = json.load(f)

# Inspect the first stop item
first_stop = next(item for item in timeline if item['type'] == 'stop')
print("Keys in timeline item:", first_stop.keys())
print("Item data:", first_stop)