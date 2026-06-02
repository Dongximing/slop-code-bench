#!/usr/bin/env python3
import sys
import heapq
from hauling import (load_systems, load_jumps, load_stargates, load_stations,
                     find_entity, find_gate_to_system, find_route,
                     _get_position, _get_entry_position, get_first_gate_lexicographically,
                     calculate_distance, calculate_warp_time,
                     WarpParams, Stargate, ZARZAKH_SYSTEM_ID)

systems = load_systems('./sde/')
jumps = load_jumps('./sde/')
stargates = load_stargates('./sde')
stations = load_stations('./sde')

warp_params = WarpParams(align_time=4.0, top_speed=562, warp_speed=8.0, dock_time=30, gate_time=10)

_, start_sta = find_entity('Jita IV - Moon 4 - Caldari Navy Assembly Plant', systems, stations)
_, end_sta = find_entity('Tama VII - Moon 9 - Republic Security Services Testing Facilities', systems, stations)

start_system_id = 30000142
end_system_id = 30002813

initial_time = warp_params.dock_time if start_sta else 0
pq = [(initial_time, start_system_id, None, None)]
visited = {}

count = 0
while pq:
    time, current_system, zarzakh_lock, prev_system = heapq.heappop(pq)
    count += 1

    if current_system == end_system_id:
        print(f'FOUND END at step {count}, time={time}')
        print(f'  state: ({current_system}, {zarzakh_lock}), prev={prev_system}')

        # Reconstruct path
        path = []
        state = (current_system, zarzakh_lock)
        step = 0
        while state[1] is not None or state[0] != start_system_id:
            path.append(state[0])
            _, prev = visited.get(state, (0, None))
            print(f'  backtrack step {step}: state={state}, prev={prev}')
            step += 1
            if prev is None:
                break
            if prev == ZARZAKH_SYSTEM_ID:
                prev_lock = current_system
            elif state[0] == ZARZAKH_SYSTEM_ID:
                prev_lock = None
            else:
                prev_lock = state[1]
            state = (prev, prev_lock)
        path.append(start_system_id)
        path.reverse()
        print(f'Reconstructed path: {path}')
        for sid in path:
            print(f'  {systems[sid].name} ({systems[sid].security:.1f})')
        break

    state_key = (current_system, zarzakh_lock)
    if state_key in visited:
        continue
    visited[state_key] = (time, prev_system)

    if count <= 5:
        print(f'Step {count}: system={systems[current_system].name} ({current_system}), lock={zarzakh_lock}, prev={prev_system}')

    for neighbor_system in jumps.get(current_system, []):
        neighbor_time = time

        gate = find_gate_to_system(current_system, neighbor_system, stargates, systems)
        if gate is None:
            current_sys = systems[current_system]
            gate = Stargate(0, current_system, 'Gate', current_sys.x, current_sys.y, current_sys.z)

        if prev_system is None:
            start_x, start_y, start_z = _get_position(current_system, start_sta, stargates, systems)
        else:
            start_x, start_y, start_z = _get_entry_position(current_system, prev_system, stargates, systems)

        warp_dist = calculate_distance(start_x, start_y, start_z, gate.x, gate.y, gate.z)
        neighbor_time += calculate_warp_time(warp_dist, warp_params) + warp_params.gate_time

        new_zarzakh_lock = zarzakh_lock
        if neighbor_system == ZARZAKH_SYSTEM_ID:
            new_zarzakh_lock = current_system
        elif current_system == ZARZAKH_SYSTEM_ID:
            if zarzakh_lock is not None and neighbor_system != zarzakh_lock:
                continue
            new_zarzakh_lock = None

        neighbor_state = (neighbor_system, new_zarzakh_lock)
        if neighbor_state not in visited:
            heapq.heappush(pq, (neighbor_time, neighbor_system, new_zarzakh_lock, current_system))

    if count > 200:
        print('Stopping after 200 iterations')
        break
