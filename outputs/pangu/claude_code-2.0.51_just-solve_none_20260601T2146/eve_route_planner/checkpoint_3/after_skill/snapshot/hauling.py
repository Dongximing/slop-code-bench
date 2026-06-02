import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
import yaml


AU_IN_M = 149597870700.0


def parse_float(value):
    if value is None or value == '':
        return None
    return float(value)


def load_sde_data(sde_dir: str):
    systems = {}
    jumps = defaultdict(set)
    stations = {}
    station_by_id = {}

    path = os.path.join(sde_dir, 'mapSolarSystems.csv.bz2')
    if os.path.exists(path):
        import bz2
        with bz2.open(path, 'rt', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                sid = int(row['solarSystemID'])
                systems[sid] = {'name': row['solarSystemName'],
                                'security': float(row['security']),
                                'x': parse_float(row['x']),
                                'y': parse_float(row['y']),
                                'z': parse_float(row['z'])}

    path = os.path.join(sde_dir, 'mapSolarSystemJumps.csv.bz2')
    if os.path.exists(path):
        import bz2
        with bz2.open(path, 'rt', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                f_id = int(row['fromSolarSystemID'])
                t_id = int(row['toSolarSystemID'])
                jumps[f_id].add(t_id)
                jumps[t_id].add(f_id)

    path = os.path.join(sde_dir, 'staStations.csv.bz2')
    if os.path.exists(path):
        import bz2
        with bz2.open(path, 'rt', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                st_id = int(row['stationID'])
                st_name = row['stationName']
                st_sys = int(row['solarSystemID'])
                stations[st_name] = {'id': st_id, 'system_id': st_sys,
                                      'x': parse_float(row['x']),
                                      'y': parse_float(row['y']),
                                      'z': parse_float(row['z'])}
                station_by_id[st_id] = stations[st_name]

    path = os.path.join(sde_dir, 'mapDenormalize.csv.bz2')
    if os.path.exists(path):
        import bz2
        with bz2.open(path, 'rt', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                try:
                    iid = int(row['itemID'])
                except (ValueError, KeyError):
                    continue
                tid_str = row.get('typeID', '').strip()
                tid = int(tid_str) if tid_str and tid_str != 'None' else None
                sid_str = row.get('solarSystemID', '').strip()
                sid = int(sid_str) if sid_str and sid_str != 'None' else None
                denormalize[iid] = {'name': row.get('itemName', ''),
                                    'type_id': tid,
                                    'system_id': sid,
                                    'x': parse_float(row.get('x', '0')),
                                    'y': parse_float(row.get('y', '0')),
                                    'z': parse_float(row.get('z', '0'))}

    return systems, jumps, stations, station_by_id, denormalize


def find_location(name: str, systems, stations):
    """Find a location by name. Returns (type, id, data_dict) or (None, None, None)."""
    name_lower = name.lower().strip()
    # Check system first
    for sys_id, sys_data in systems.items():
        if name_lower == sys_data['name'].lower():
            result = dict(sys_data)
            result['id'] = sys_id
            return 'system', sys_id, result
        if name_lower in sys_data['name'].lower():
            result = dict(sys_data)
            result['id'] = sys_id
            return 'system', sys_id, result
    # Check station
    if name_lower in stations:
        st_data = stations[name_lower]
        result = dict(st_data)
        result['name'] = name_lower
        return 'station', st_data['system_id'], result
    for sname, sdata in stations.items():
        if name_lower in sname.lower():
            result = dict(sdata)
            result['name'] = sname
            return 'station', result['system_id'], result
    return None, None, None


def calculate_distance(pos1, pos2):
    dx = pos1[0] - pos2[0]
    dy = pos1[1] - pos2[1]
    dz = pos1[2] - pos2[2]
    return math.sqrt(dx*dx + dy*dy + dz*dz)


def calculate_warp_time(distance_m, warp_speed_au_s, top_speed):
    k_a = warp_speed_au_s
    k_d = min(warp_speed_au_s / 3, 2)
    v_drop = min(top_speed / 2, 100)
    v_warp_max_ms = warp_speed_au_s * AU_IN_M

    d_min = v_warp_max_ms**2 / (2 * k_a * k_d * AU_IN_M) + AU_IN_M
    if distance_m < d_min:
        v_warp_ms = (distance_m * k_a * k_d) / (k_a + k_d)
        t_cruise = 0
    else:
        v_warp_ms = v_warp_max_ms
        t_cruise = (distance_m - d_min) / v_warp_ms

    v_warp_au_s = v_warp_ms / AU_IN_M
    return (1 / k_a) * math.log(v_warp_au_s / k_a) + (1 / k_d) * math.log(v_warp_au_s / (v_drop / AU_IN_M)) + t_cruise


def find_path(jumps, systems, start_id, end_id, allow_low_sec=True):
    if start_id == end_id:
        return [start_id]
    queue = [start_id]
    visited = {start_id}
    parent = {start_id: None}
    while queue:
        current = queue.pop(0)
        if current == end_id:
            path = []
            while current is not None:
                path.append(current)
                current = parent[current]
            return path[::-1]
        for nb in jumps.get(current, set()):
            if nb not in visited:
                if not allow_low_sec and systems[nb]['security'] < 0.5:
                    continue
                visited.add(nb)
                parent[nb] = current
                queue.append(nb)
    return None


def format_time(seconds):
    return f"{math.ceil(seconds / 60) // 60:02d}:{math.ceil(seconds / 60) % 60:02d}"


def format_amount(amount):
    return f"{amount:,.2f}"


def load_yaml(path):
    with open(path, 'r') as f:
        return yaml.safe_load(f)


def load_contracts(path):
    contracts = []
    with open(path, 'r') as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if line:
                contract = json.loads(line)
                contract['id'] = i
                contracts.append(contract)
    return contracts


class HaulingCalculator:
    __slots__ = ('config', 'ship_name', 'systems', 'jumps', 'stations', 'times',
                 'ship', 'ship_type', 'align', 'top_speed', 'warp_speed', 'cargo_size', 'is_freighter')

    def __init__(self, config, ship_name, systems, jumps, stations, times):
        self.config = config
        self.ship_name = ship_name
        self.systems = systems
        self.jumps = jumps
        self.stations = stations
        self.times = times
        self.ship = config['ships'][ship_name]
        self.ship_type = self.ship['type']
        self.align = self.ship['align']
        self.top_speed = self.ship['top_speed']
        self.warp_speed = self.ship['warp_speed']
        self.cargo_size = self.ship['cargo_size']
        self.is_freighter = self.ship_type == 'Freighter'

    def path_time(self, path):
        total = 0.0
        for i in range(len(path) - 1):
            s, t = path[i], path[i+1]
            pos1 = (self.systems[s]['x'], self.systems[s]['y'], self.systems[s]['z'])
            pos2 = (self.systems[t]['x'], self.systems[t]['y'], self.systems[t]['z'])
            total += self.align + calculate_warp_time(calculate_distance(pos1, pos2),
                                                       self.warp_speed, self.top_speed)
            total += self.times['gate']
        return total

    def route(self, start_id, end_id):
        return find_path(self.jumps, self.systems, start_id, end_id,
                         allow_low_sec=not self.is_freighter)

    def total_time(self, operations):
        total = 0.0
        for op in operations:
            t = op['type']
            if t in ('UNDOCK', 'DOCK'):
                total += self.times['dock']
            elif t == 'GO':
                parts = [p.split(' (')[0] for p in op['route'].split(' -> ')]
                path = []
                for part in parts:
                    sid, _ = find_location(part, self.systems, self.stations)
                    if sid:
                        path.append(sid)
                if len(path) > 1:
                    total += self.path_time(path)
            elif t in ('LOAD', 'UNLOAD'):
                total += self.times['move_cargo']
        return total


def route_for_waypoint(calc, current_sys_id, wp):
    wp_type, wp_id, wp_data = find_location(wp['name'], calc.systems, calc.stations)
    if wp_type is None:
        raise ValueError(f"Could not find location: {wp['name']}")
    route = find_path(calc.jumps, calc.systems, current_sys_id, wp_id,
                      allow_low_sec=not calc.is_freighter)
    if not route:
        raise ValueError(f"No route from {calc.systems[current_sys_id]['name']} to {wp['name']}")
    path_str = ' -> '.join(f"{calc.systems[sid]['name']} ({calc.systems[sid]['security']:.1f})"
                           for sid in route)
    return route, path_str, wp_type, wp_data


def format_path(path, systems):
    return ' -> '.join(f"{systems[sid]['name']} ({systems[sid]['security']:.1f})" for sid in path)


def bankers_round(value):
    return float(f"{value:.10f}")


def get_ship_ehp(ship_name, config):
    if 'ehp' in config['ships'][ship_name]:
        return config['ships'][ship_name]['ehp']
    ship_type = config['ships'][ship_name]['type']
    return 60000 if ship_type == 'Deep Space Transport' else (300000 if ship_type == 'Freighter' else 0)


def calculate_contract_parameters(contract, systems, jumps, calc, start_sys_id):
    start_type, start_id, _ = find_location(contract['start'], systems, calc.stations)
    end_type, end_id, _ = find_location(contract['end'], systems, calc.stations)
    if start_type is None or end_type is None:
        return None

    route = find_path(jumps, systems, start_id, end_id,
                      allow_low_sec=not calc.is_freighter)
    if route is None:
        return None

    contract_jumps = len(route) - 1

    leg_time = 0.0
    for i in range(len(route) - 1):
        s, t = route[i], route[i+1]
        pos1 = (systems[s]['x'], systems[s]['y'], systems[s]['z'])
        pos2 = (systems[t]['x'], systems[t]['y'], systems[t]['z'])
        leg_time += calc.align + calculate_warp_time(calculate_distance(pos1, pos2),
                                                      calc.warp_speed, calc.top_speed)
        leg_time += calc.times['gate']

    return_route = find_path(jumps, systems, end_id, start_sys_id,
                             allow_low_sec=not calc.is_freighter)
    if return_route is None:
        return None

    return_jumps = len(return_route) - 1

    return {
        'start_sys': start_id,
        'end_sys': end_id,
        'route': route,
        'return_route': return_route,
        'total_jumps': contract_jumps + return_jumps,
        'total_leg_time': leg_time,
        'reward': contract['reward'],
        'collateral': contract.get('collateral', 0),
        'm3': contract.get('m3', 0),
        'actual_value': contract.get('actual_value', 0),
        'issuer': contract.get('issuer', ''),
        'id': contract['id']
    }


def contracts_main(args, config, systems, jumps, stations):
    start_type, start_sys_id, _ = find_location(args.start, systems, stations)
    if start_type is None:
        print("No Good Contracts")
        return

    contracts = load_contracts(args.contracts_file)

    min_isk_per_jump = config.get('min_isk_per_jump')
    max_isk_per_ehp = config.get('max_isk_per_ehp')

    valid_contracts = []

    for ship_name in config['ships']:
        calc = HaulingCalculator(config, ship_name, systems, jumps, stations, config['times'])
        ehp = get_ship_ehp(ship_name, config)
        ship_type = config['ships'][ship_name]['type']

        for contract in contracts:
            params = calculate_contract_parameters(contract, systems, jumps, calc, start_sys_id)
            if params is None:
                continue

            if params['m3'] > calc.cargo_size:
                continue

            isk_per_jump = params['reward'] / params['total_jumps'] if params['total_jumps'] > 0 else 0

            if min_isk_per_jump is not None and isk_per_jump < min_isk_per_jump:
                continue

            if max_isk_per_ehp is not None and ship_type != 'Blockade Runner':
                isk_per_ehp = params['reward'] / ehp if ehp > 0 else float('inf')
                if isk_per_ehp > max_isk_per_ehp:
                    continue

            total_time = params['total_leg_time'] + 2 * calc.times['dock'] + calc.times['move_cargo']

            valid_contracts.append({
                'ship_name': ship_name,
                'ship_type': ship_type,
                'ehp': ehp,
                'contract': contract,
                'params': params,
                'isk_per_jump': isk_per_jump
            })

    if not valid_contracts:
        print("No Good Contracts")
        return

    valid_contracts.sort(key=lambda c: (-c['isk_per_jump'], -c['ehp'], c['ship_name'], c['contract'].get('issuer', '')))

    best_ship = min(config['ships'].keys(),
                    key=lambda s: (config['ships'][s]['align'] + config['ships'][s]['top_speed'],
                                   -get_ship_ehp(s, config), s))

    ship_contracts = [c for c in valid_contracts if c['ship_name'] == best_ship]

    if not ship_contracts:
        print("No Good Contracts")
        return

    ship_contracts.sort(key=lambda c: (-c['isk_per_jump'], -c['ehp'], c['ship_name'], c['contract'].get('issuer', '')))

    selected_contracts = []
    total_time = 0.0
    total_reward = 0.0
    total_m3 = 0.0
    total_jumps = 0

    calc = HaulingCalculator(config, best_ship, systems, jumps, stations, config['times'])

    max_time_seconds = args.max_time * 60 if args.max_time else None

    for sc in ship_contracts:
        contract = sc['contract']
        params = sc['params']

        route_time = params['total_leg_time'] + 2 * calc.times['dock'] + calc.times['move_cargo']

        if max_time_seconds is not None and total_time + route_time > max_time_seconds:
            continue

        if args.target_iph is not None:
            combined_reward = total_reward + params['reward']
            combined_time = total_time + route_time
            if combined_time > 0 and combined_reward / combined_time < args.target_iph:
                continue

        selected_contracts.append((contract, params, route_time))
        total_reward += params['reward']
        total_m3 += params['m3']
        total_jumps += params['total_jumps']
        total_time += route_time

    if not selected_contracts:
        print("No Good Contracts")
        return

    total_time_minutes = total_time / 60.0
    total_time_hours = total_time_minutes / 60.0

    iph = total_reward / total_time_hours if total_time_hours > 0 else 0
    isk_per_m3 = total_reward / total_m3 if total_m3 > 0 else 0

    print(f"SHIP: {best_ship}")

    current_cargo = 0
    ship_calc = HaulingCalculator(config, best_ship, systems, jumps, stations, config['times'])
    carrying_contracts = []
    unloaded_contracts = set()

    for contract, params, route_time in selected_contracts:
        contract_id = contract['id']
        issuer = contract.get('issuer', '')
        reward = params['reward']
        m3 = params['m3']

        reward_m = bankers_round(reward / 1000000.0)
        print(f"LOAD {issuer} (id={contract_id}): {format_amount(reward_m)}M ISK | {format_amount(m3)} m3")
        carrying_contracts.append((contract_id, params['total_jumps'], m3))

        route_str = format_path(params['route'], systems)
        print(f"GO: {route_str}")

        jumps_while_carrying = params['total_jumps'] - len(params['return_route']) + 1
        print(f"UNLOAD {issuer} (id={contract_id}): {jumps_while_carrying} Jumps | {format_amount(m3)} m3")

        if params['return_route']:
            return_route_str = format_path(params['return_route'], systems)
            print(f"GO: {return_route_str}")

    print(f"MOVED")
    print(f"NUM CONTRACTS: {len(selected_contracts)}")
    profit_m = bankers_round(total_reward / 1000000.0)
    print(f"PROFIT: {format_amount(profit_m)}M")
    print(f"ISK/M3: {format_amount(bankers_round(isk_per_m3))}")
    isk_per_jump_m = bankers_round(total_reward / total_jumps) / 1000000.0 if total_jumps > 0 else 0
    print(f"ISK/Jump: {format_amount(isk_per_jump_m)}M")
    iph_m = bankers_round(iph / 1000000.0)
    print(f"ISK/Hour: {format_amount(iph_m)}M")


def plan_trip(calc, start_station, end_station, cargo_waypoints, starting_cargo, is_first):
    ops = []
    start_type, start_sys_id, start_data = find_location(start_station['name'], calc.systems, calc.stations)
    current_sys = start_data['system_id'] if start_type == 'station' else start_data['id']

    if is_first:
        ops.append({'type': 'START', 'location': start_station['name']})
    if start_type == 'station':
        ops.append({'type': 'UNDOCK'})

    current_cargo = starting_cargo

    for i, wp in enumerate(cargo_waypoints):
        wp_cargo = wp.get('cargo')
        if wp_cargo is None:
            continue

        route, path_str, wp_type, wp_data = route_for_waypoint(calc, current_sys, wp)
        wp_id = wp_data['system_id'] if wp_type == 'station' else wp_data['id']

        ops.append({'type': 'GO', 'route': path_str})
        current_sys = wp_id

        if wp_type == 'station':
            ops.append({'type': 'DOCK', 'location': wp['name']})

        abs_cargo = abs(wp_cargo)
        if wp_cargo > 0:
            actual = min(abs_cargo, calc.cargo_size - current_cargo)
            if actual > 0:
                ops.append({'type': 'LOAD', 'amount': actual, 'contract_id': wp.get('contract_id', 0)})
                current_cargo += actual
        else:
            if current_cargo >= abs_cargo:
                ops.append({'type': 'UNLOAD', 'amount': abs_cargo, 'contract_id': wp.get('contract_id', 0)})
                current_cargo -= abs_cargo

        remaining = [w for j, w in enumerate(cargo_waypoints) if j > i and w.get('cargo') is not None]
        if wp_type == 'station' and remaining:
            ops.append({'type': 'UNDOCK'})

    if current_cargo > 0:
        end_type, end_id, end_data = find_location(end_station['name'], calc.systems, calc.stations)
        if end_type is not None:
            route, path_str, _, _ = route_for_waypoint(calc, current_sys, end_station)
            ops.append({'type': 'GO', 'route': path_str})
            if end_type == 'station':
                ops.append({'type': 'DOCK', 'location': end_station['name']})
                ops.append({'type': 'UNLOAD', 'amount': current_cargo})

    return ops


def main():
    parser = argparse.ArgumentParser(description='Hauling route planner for New Eden')
    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    plan_parser = subparsers.add_parser('plan', help='Plan a hauling route')
    plan_parser.add_argument('start', help='Starting location')
    plan_parser.add_argument('end', help='Ending location')
    plan_parser.add_argument('--manifest', required=True, help='Manifest YAML path')
    plan_parser.add_argument('--config', required=True, help='Config YAML path')
    plan_parser.add_argument('--ship', required=True, help='Ship name')
    plan_parser.add_argument('--sde', required=True, help='SDE directory')

    contracts_parser = subparsers.add_parser('contracts', help='Plan contracts hauling')
    contracts_parser.add_argument('start', help='Starting system')
    contracts_parser.add_argument('contracts_file', help='Path to contracts JSONL file')
    contracts_parser.add_argument('--config', required=True, help='Config YAML path')
    contracts_parser.add_argument('--sde', required=True, help='SDE directory')
    contracts_parser.add_argument('--target-iph', type=float, help='Target ISK per hour (M isk/Hour)')
    contracts_parser.add_argument('--max-time', type=int, help='Maximum time in minutes')

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return

    config = load_yaml(args.config)
    if 'ships' not in config or 'times' not in config:
        raise ValueError("Config must contain 'ships' and 'times'")

    systems, jumps, stations, _, _ = load_sde_data(args.sde)

    if args.command == 'plan':
        manifest = load_yaml(args.manifest)
        start_cargo = manifest.get('start_cargo') or 0
        waypoints = manifest.get('waypoints', [])

        start_type, start_sys_id, start_data = find_location(args.start, systems, stations)
        end_type, end_id, end_data = find_location(args.end, systems, stations)

        if None in (start_type, end_type):
            raise ValueError(f"Could not find location: {args.start} or {args.end}")

        if args.ship not in config['ships']:
            raise ValueError(f"Ship '{args.ship}' not found")

        calc = HaulingCalculator(config, args.ship, systems, jumps, stations, config['times'])

        def resolve_station(stype, ssys_data, sst_data):
            if stype == 'station':
                return sst_data
            sid = ssys_data['id']
            for sname, sdata in stations.items():
                if sdata['system_id'] == sid:
                    return {'name': sname, 'system_id': sid}
            raise ValueError(f"No station in system: {ssys_data['name']}")

        start_station = resolve_station(start_type, start_data, start_data)
        end_station = resolve_station(end_type, end_data, end_data)

        cargo_waypoints = [wp for wp in waypoints if wp.get('cargo') is not None]
        total_moved = sum(abs(wp.get('cargo', 0) or 0) for wp in cargo_waypoints) + start_cargo

        if not cargo_waypoints and start_cargo == 0:
            print(f"START: {start_station['name']}")
            if start_type == 'station':
                print("UNDOCK")
            sid = start_station.get('system_id', start_sys_id)
            eid = end_station.get('system_id', end_id)
            route = calc.route(sid, eid)
            if route:
                print(f"GO: {format_path(route, systems)}")
            if end_type == 'station':
                print(f"DOCK: {end_station['name']}")
            tt = 0.0
            if start_type == 'station':
                tt += calc.times['dock']
            if route and len(route) > 1:
                tt += calc.path_time(route)
            if end_type == 'station':
                tt += calc.times['dock']
            print(f"DONE: {format_time(tt)}")
            return

        all_ops = []
        trip_idx = 1
        current_cargo = start_cargo
        processed = set()

        while len(processed) < len(cargo_waypoints) or current_cargo > 0:
            if trip_idx > 1:
                all_ops.append({'type': 'TRIP_SEPARATOR', 'index': trip_idx})

            trip_wps = []
            loaded = 0.0
            for i, wp in enumerate(cargo_waypoints):
                if i in processed:
                    continue
                c = wp.get('cargo', 0) or 0
                if c > 0:
                    if loaded + c <= calc.cargo_size:
                        trip_wps.append(wp)
                        processed.add(i)
                        loaded += c
                else:
                    if current_cargo >= abs(c):
                        trip_wps.append(wp)
                        processed.add(i)

            if not trip_wps and current_cargo == 0:
                break

            trip_ops = plan_trip(calc, start_station if trip_idx == 1 else end_station,
                                 end_station, trip_wps, current_cargo, trip_idx == 1)
            if trip_idx > 1:
                trip_ops = [op for op in trip_ops if op['type'] != 'START']
            all_ops.extend(trip_ops)

            new_cargo = current_cargo
            for wp in trip_wps:
                c = wp.get('cargo', 0) or 0
                if c > 0:
                    new_cargo = min(new_cargo + c, calc.cargo_size)
                elif c < 0:
                    new_cargo = max(0, new_cargo + c)
            current_cargo = new_cargo
            trip_idx += 1

        total = calc.total_time(all_ops)

        for op in all_ops:
            t = op['type']
            if t == 'START':
                print(f"START: {op['location']}")
            elif t == 'UNDOCK':
                print("UNDOCK")
            elif t == 'DOCK':
                print(f"DOCK: {op['location']}")
            elif t == 'GO':
                print(f"GO: {op['route']}")
            elif t == 'LOAD':
                print(f"LOAD: {format_amount(op['amount'])} m3")
            elif t == 'UNLOAD':
                print(f"UNLOAD: {format_amount(op['amount'])} m3")
            elif t == 'TRIP_SEPARATOR':
                print(f"[--- TRIP {op['index']} ---]")

        print(f"DONE: {format_time(total)}")
        if total_moved > 0:
            print(f"MOVED: {format_amount(total_moved)} m3")

    elif args.command == 'contracts':
        contracts_main(args, config, systems, jumps, stations)


if __name__ == '__main__':
    main()
