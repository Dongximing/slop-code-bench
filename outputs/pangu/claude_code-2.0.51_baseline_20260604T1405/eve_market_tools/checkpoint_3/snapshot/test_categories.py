#!/usr/bin/env python3
import sys
sys.path.insert(0, '/workspace')

from market_tools import SDE

sde = SDE('sde')
sde.load()

# Show all categories
print("All categories:")
for cat_id, cat_name in sorted(sde.categories.items()):
    if cat_id > 0:
        print(f"  {cat_id}: {cat_name}")

print("\n=== Category IDs for compressed ores ===")
for tid, t in sorted(sde.types.items()):
    if 'Compressed' in t.name:
        cat_name = sde.get_category_name(t.category_id)
        print(f"  {tid}: {t.name} -> {cat_name} (category {t.category_id})")
        break

# Count ores by category
cat_counts = {}
for tid, t in sde.types.items():
    if 'Compressed' in t.name:
        cat = sde.get_category_name(t.category_id)
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

print(f"\nCompressed types by category: {cat_counts}")
