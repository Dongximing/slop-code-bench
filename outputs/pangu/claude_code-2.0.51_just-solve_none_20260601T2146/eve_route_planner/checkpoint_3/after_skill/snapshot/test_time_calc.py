#!/usr/bin/env python3
import sys
sys.path.insert(0, '.')

from hauling import *

config = load_yaml('test_contracts.yaml')
systems, jumps, stations, _, _ = load_sde_data('sde')

start_type, start_sys_data, _ = identify_location('Jita', systems, stations)
start_sys_id = start_sys_data['system_id'] if start_type == 'station' else start_sys_data['id']

calc = HaulingCalculator(config, 'Deep Space Transport', systems, jumps, stations, config['times'])

contracts = load_contracts('test_contracts.jsonl')
contract = contracts[2]  # Contract 3
params = calculate_contract_parameters(contract, systems, jumps, calc, start_sys_id)

print(f"Route: {len(params['route'])} systems, {len(params['route'])-1} jumps")
print(f"Return route: {len(params['return_route'])} systems")

# Calculate time
leg_time = params['total_leg_time']
dock_time = 2 * calc.times['dock']
move_time = calc.times['move_cargo']
total_seconds = leg_time + dock_time + move_time

print(f"Leg time: {leg_time:.2f} seconds")
print(f"Dock time: {dock_time:.2f} seconds")
print(f"Move time: {move_time:.2f} seconds")
print(f"Total: {total_seconds:.2f} seconds")

# More detailed leg time calculation
print("\nDetailed leg time:")
total = 0.0
for i in range(len(params['route']) - 1):
    s, t = params['route'][i], params['route'][i+1]
    pos1 = (systems[s]['x'], systems[s]['y'], systems[s]['z'])
    pos2 = (systems[t]['x'], systems[t]['y'], systems[t]['z'])
    dist = calculate_distance(pos1, pos2)
    warp_t = calculate_warp_time(dist, calc.warp_speed, calc.top_speed)
    gate_t = calc.times['gate']
    align_t = calc.align
    total += align_t + warp_t + gate_t
    print(f"  Jump {i+1}: dist={dist/1000:.0f}km, warp={warp_t:.1f}s, gate={gate_t:.1f}s, align={align_t:.1f}s, subtotal={align_t+warp_t+gate_t:.1f}s")

print(f"Total leg time detailed: {total:.2f}s")
print(f"Total time: {total + dock_time + move_time:.2f}s")
print(f"Total hours: {(total + dock_time + move_time)/3600:.4f}")

# Now check total_jumps
print(f"\nTotal jumps: {params['total_jumps']}")
print(f"Reward: {contract['reward']} ISK")
print(f"IPH: {contract['reward'] / ((total + dock_time + move_time)/3600):.2f} ISK/hr")
