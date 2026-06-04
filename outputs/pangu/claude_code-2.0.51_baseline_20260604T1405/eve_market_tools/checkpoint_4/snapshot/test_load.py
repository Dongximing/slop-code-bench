#!/usr/bin/env python3
import sys
sys.path.insert(0, '/workspace')

from market_tools import SDE

# Initialize SDE
sde = SDE('sde')
sde.load()
print("SDE loaded successfully")
print(f"Types count: {len(sde.types)}")

# Count compressed ore types
compressed_count = sum(1 for t in sde.types.values() if 'Compressed' in t.name)
print(f"Compressed types: {compressed_count}")

print("\n=== Compressed Ore Types (Asteroid category) ===")
count = 0
for tid, t in sorted(sde.types.items()):
    if 'Compressed' in t.name:
        cat_name = sde.get_category_name(t.category_id)
        if cat_name == 'Asteroid':
            print(f"  {tid}: {t.name} (portion={t.portion_size}, volume={t.volume})")
            count += 1
            if count >= 30:
                break

print("\n=== Gas Isotopes (Compressed) ===")
for tid, t in sorted(sde.types.items()):
    if 'Compressed' in t.name:
        cat_name = sde.get_category_name(t.category_id)
        if cat_name == 'Gas':
            print(f"  {tid}: {t.name} (portion={t.portion_size}, volume={t.volume})")

print("\n=== Ice (Compressed) ===")
for tid, t in sorted(sde.types.items()):
    if 'Compressed' in t.name:
        cat_name = sde.get_category_name(t.category_id)
        if cat_name == 'Ice':
            print(f"  {tid}: {t.name} (portion={t.portion_size}, volume={t.volume})")

print("\n=== Tritanium ===")
tritanium_id = sde.type_ids.get('Tritanium')
if tritanium_id:
    print(f"  Volume: {sde.get_volume(tritanium_id)}")

print("\n=== Pyerite ===")
pyerite_id = sde.type_ids.get('Pyerite')
if pyerite_id:
    print(f"  Volume: {sde.get_volume(pyerite_id)}")

print("\n=== Mexallon ===")
mexallon_id = sde.type_ids.get('Mexallon')
if mexallon_id:
    print(f"  Volume: {sde.get_volume(mexallon_id)}")

print("\n=== Categories ===")
print(f"  Asteroid (422) check: {sde.get_category_name(422)}")
print(f"  Gas (422?) check: {sde.get_category_name(422) == 'Gas'}")
