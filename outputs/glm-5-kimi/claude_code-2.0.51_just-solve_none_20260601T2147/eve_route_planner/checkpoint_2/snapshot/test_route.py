#!/usr/bin/env python3
import sys
sys.dont_write_bytecode = True

# Import everything from hauling.py
from hauling import (load_systems, load_jumps, load_stargates, load_stations,
                     find_entity, find_route, WarpParams)

systems = load_systems('./sde/')
jumps = load_jumps('./sde/')
stargates = load_stargates('./sde')
stations = load_stations('./sde')

warp_params = WarpParams(align_time=4.0, top_speed=562, warp_speed=8.0, dock_time=30, gate_time=10)

_, start_sta = find_entity('Jita IV - Moon 4 - Caldari Navy Assembly Plant', systems, stations)
_, end_sta = find_entity('Tama VII - Moon 9 - Republic Security Services Testing Facilities', systems, stations)

start_id = start_sta.system_id
end_id = end_sta.system_id

route, total_time = find_route(start_id, start_sta, end_id, end_sta, systems, jumps, stargates, warp_params)
print(f'Route: {route}')
for sid in route:
    print(f'  {systems[sid].name}')
print(f'Time: {total_time:.2f}s')
