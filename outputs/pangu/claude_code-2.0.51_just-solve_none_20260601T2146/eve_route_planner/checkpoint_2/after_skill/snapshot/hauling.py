#!/usr/bin/env python3

import argparse
import csv
import math
import os
import sys
import bz2
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple, Set
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
    denormalize = {}

    path = os.path.join(sde_dir, 'mapSolarSystems.csv.bz2')
    if os.path.exists(path):
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
        with bz2.open(path, 'rt', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                f_id = int(row['fromSolarSystemID'])
                t_id = int(row['toSolarSystemID'])
                jumps[f_id].add(t_id)
                jumps[t_id].add(f_id)

    path = os.path.join(sde_dir, 'staStations.csv.bz2')
    if os.path.exists(path):
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


def find_system_by_name(systems, name):
    name_lower = name.lower().strip()
    match = None
    for sys_id, sys_data in systems.items():
        if name_lower == sys_data['name'].lower():
            match = sys_id
            break
    if match is None:
        for sys_id, sys_data in systems.items():
            if name_lower in sys_data['name'].lower():
                match = sys_id
                break
    if match:
        result = dict(systems[match])
        result['id'] = match
        return match, result
    return None, None


def find_station_by_name(stations, name):
    name_lower = name.lower().strip()
    match = None
    if name_lower in stations:
        match = name_lower
    if match is None:
        for sname in stations:
            if name_lower in sname.lower():
                match = sname
                break
    if match:
        result = dict(stations[match])
        result['name'] = match
        return match, result
    return None, None


def identify_location(location, systems, stations):
    sys_id, sys_data = find_system_by_name(systems, location)
    if sys_id:
        return 'system', sys_data, None
    st_name, st_data = find_station_by_name(stations, location)
    if st_name:
        st_with_name = dict(st_data)
        st_with_name['name'] = st_name
        return 'station', st_with_name, st_with_name
    raise ValueError(f"Could not find location: {location}")


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
    queue = deque([start_id])
    visited = {start_id}
    parent = {start_id: None}
    while queue:
        current = queue.popleft()
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
                    sid, _ = find_system_by_name(self.systems, part)
                    if sid:
                        path.append(sid)
                if len(path) > 1:
                    total += self.path_time(path)
            elif t in ('LOAD', 'UNLOAD'):
                total += self.times['move_cargo']
        return total


def route_for_waypoint(calc, current_sys_id, wp):
    wp_type, wp_sys, wp_st = identify_location(wp['name'], calc.systems, calc.stations)
    wp_id = wp_st['system_id'] if wp_type == 'station' else wp_sys['id']
    route = find_path(calc.jumps, calc.systems, current_sys_id, wp_id, allow_low_sec=not calc.is_freighter)
    if not route:
        raise ValueError(f"No route from {calc.systems[current_sys_id]['name']} to {wp['name']}")
    path_str = ' -> '.join(f"{calc.systems[sid]['name']} ({calc.systems[sid]['security']:.1f})"
                           for sid in route)
    return route, path_str, wp_type, wp_sys, wp_st


def format_path(path, systems):
    return ' -> '.join(f"{systems[sid]['name']} ({systems[sid]['security']:.1f})" for sid in path)


def plan_trip(calc, start_station, end_station, cargo_waypoints, starting_cargo, is_first):
    ops = []
    start_type, start_sys, start_st = identify_location(start_station['name'], calc.systems, calc.stations)
    current_sys = start_st['system_id'] if start_type == 'station' else start_sys['id']
    if is_first:
        ops.append({'type': 'START', 'location': start_station['name']})
    if start_type == 'station':
        ops.append({'type': 'UNDOCK'})
    current_cargo = starting_cargo
    for i, wp in enumerate(cargo_waypoints):
        wp_cargo = wp.get('cargo')
        if wp_cargo is None:
            continue
        route, path_str, wp_type, wp_sys, wp_st = route_for_waypoint(calc, current_sys, wp)
        wp_id = wp_st['system_id'] if wp_type == 'station' else wp_sys['id']
        ops.append({'type': 'GO', 'route': path_str})
        current_sys = wp_id
        if wp_type == 'station':
            ops.append({'type': 'DOCK', 'location': wp['name']})
        abs_cargo = abs(wp_cargo)
        if wp_cargo > 0:
            actual = min(abs_cargo, calc.cargo_size - current_cargo)
            if actual > 0:
                ops.append({'type': 'LOAD', 'amount': actual})
                current_cargo += actual
        else:
            if current_cargo >= abs_cargo:
                ops.append({'type': 'UNLOAD', 'amount': abs_cargo})
                current_cargo -= abs_cargo
        remaining = [w for j, w in enumerate(cargo_waypoints) if j > i and w.get('cargo') is not None]
        if wp_type == 'station' and remaining:
            ops.append({'type': 'UNDOCK'})
    if current_cargo > 0:
        route, path_str, end_type, end_sys, end_st = route_for_waypoint(calc, current_sys, end_station)
        ops.append({'type': 'GO', 'route': path_str})
        if end_type == 'station':
            ops.append({'type': 'DOCK', 'location': end_station['name']})
            ops.append({'type': 'UNLOAD', 'amount': current_cargo})
    return ops


def main():
    parser = argparse.ArgumentParser(description='Hauling route planner for New Eden')
    parser.add_argument('start', help='Starting location')
    parser.add_argument('end', help='Ending location')
    parser.add_argument('--manifest', required=True, help='Manifest YAML path')
    parser.add_argument('--config', required=True, help='Config YAML path')
    parser.add_argument('--ship', required=True, help='Ship name')
    parser.add_argument('--sde', required=True, help='SDE directory')
    args = parser.parse_args()

    config = load_yaml(args.config)
    if 'ships' not in config or 'times' not in config:
        raise ValueError("Config must contain 'ships' and 'times'")

    manifest = load_yaml(args.manifest)
    start_cargo = manifest.get('start_cargo') or 0
    waypoints = manifest.get('waypoints', [])

    systems, jumps, stations, _, _ = load_sde_data(args.sde)

    start_type, start_sys, start_st = identify_location(args.start, systems, stations)
    end_type, end_sys, end_st = identify_location(args.end, systems, stations)

    if args.ship not in config['ships']:
        raise ValueError(f"Ship '{args.ship}' not found")
    calc = HaulingCalculator(config, args.ship, systems, jumps, stations, config['times'])

    def resolve_station(stype, ssys, sst):
        if stype == 'station':
            return sst
        sid = ssys['id']
        for sname, sdata in stations.items():
            if sdata['system_id'] == sid:
                return {'name': sname, 'system_id': sid}
        raise ValueError(f"No station in system: {ssys['name']}")

    start_station = resolve_station(start_type, start_sys, start_st)
    end_station = resolve_station(end_type, end_sys, end_st)

    cargo_waypoints = [wp for wp in waypoints if wp.get('cargo') is not None]
    total_moved = sum(abs(wp.get('cargo', 0) or 0) for wp in cargo_waypoints) + start_cargo

    if not cargo_waypoints and start_cargo == 0:
        print(f"START: {start_station['name']}")
        if start_type == 'station':
            print("UNDOCK")
        sid = start_station.get('system_id', start_sys['id'])
        eid = end_station.get('system_id', end_sys['id'])
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

        trip_ops = plan_trip(calc, start_station if trip_idx == 1 else end_station, end_station,
                             trip_wps, current_cargo, trip_idx == 1)
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


if __name__ == '__main__':
    main()
