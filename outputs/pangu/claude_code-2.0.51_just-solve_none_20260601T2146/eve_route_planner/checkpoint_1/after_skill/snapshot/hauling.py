#!/usr/bin/env python3
"""
Hauling route planner for New Eden using EVE SDE.
"""

import argparse
import csv
import math
import os
import sys
import bz2
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple, Set


# Constants
AU_IN_M = 149597870700.0  # 1 AU in meters


def load_csv_bz2(filepath: str) -> List[Dict]:
    """Load a CSV file compressed with bz2."""
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return list(reader)


def parse_float(value: str, default: float = None) -> float:
    """Parse a float value from CSV."""
    if value is None or value == '':
        return default
    return float(value)


def load_sde_data(sde_dir: str) -> Tuple[Dict, Dict, Dict, Dict]:
    """Load all SDE data files and return structured data."""
    systems = {}  # system_id -> {name, security, x, y, z}
    jumps = defaultdict(set)  # system_id -> set of connected system_ids
    stations = {}  # station_name -> {id, system_id, x, y, z}
    station_by_id = {}  # station_id -> {name, system_id, x, y, z}
    denormalize = {}  # item_id -> {name, type_id, system_id, x, y, z}

    # Load mapSolarSystems
    systems_file = os.path.join(sde_dir, 'mapSolarSystems.csv.bz2')
    if os.path.exists(systems_file):
        for row in load_csv_bz2(systems_file):
            system_id = int(row['solarSystemID'])
            systems[system_id] = {
                'name': row['solarSystemName'],
                'security': float(row['security']),
                'x': parse_float(row['x']),
                'y': parse_float(row['y']),
                'z': parse_float(row['z'])
            }

    # Load mapSolarSystemJumps
    jumps_file = os.path.join(sde_dir, 'mapSolarSystemJumps.csv.bz2')
    if os.path.exists(jumps_file):
        for row in load_csv_bz2(jumps_file):
            from_id = int(row['fromSolarSystemID'])
            to_id = int(row['toSolarSystemID'])
            jumps[from_id].add(to_id)
            jumps[to_id].add(from_id)  # Bidirectional

    # Load staStations
    stations_file = os.path.join(sde_dir, 'staStations.csv.bz2')
    if os.path.exists(stations_file):
        for row in load_csv_bz2(stations_file):
            station_id = int(row['stationID'])
            station_name = row['stationName']
            system_id = int(row['solarSystemID'])
            stations[station_name] = {
                'id': station_id,
                'system_id': system_id,
                'x': parse_float(row['x']),
                'y': parse_float(row['y']),
                'z': parse_float(row['z'])
            }
            station_by_id[station_id] = stations[station_name]

    # Load mapDenormalize
    denormalize_file = os.path.join(sde_dir, 'mapDenormalize.csv.bz2')
    if os.path.exists(denormalize_file):
        for row in load_csv_bz2(denormalize_file):
            try:
                item_id = int(row['itemID'])
            except (ValueError, KeyError):
                continue
            type_id_str = row.get('typeID', '').strip()
            type_id = int(type_id_str) if type_id_str and type_id_str != 'None' else None
            system_id_str = row.get('solarSystemID', '').strip()
            system_id = int(system_id_str) if system_id_str and system_id_str != 'None' else None
            denormalize[item_id] = {
                'name': row.get('itemName', ''),
                'type_id': type_id,
                'system_id': system_id,
                'x': parse_float(row.get('x', '0')),
                'y': parse_float(row.get('y', '0')),
                'z': parse_float(row.get('z', '0'))
            }

    return systems, jumps, stations, station_by_id, denormalize


def find_system_by_name(systems: Dict, name: str) -> Optional[Dict]:
    """Find a system by name (case-insensitive partial match)."""
    name_lower = name.lower().strip()
    exact = None
    partial = None
    for sys_id, sys_data in systems.items():
        sys_name = sys_data['name'].lower()
        if name_lower == sys_name:
            exact = sys_id
        elif name_lower in sys_name and partial is None:
            partial = sys_id
    match = exact or partial
    if match is None:
        return None
    result = dict(systems[match])
    result['id'] = match
    return result


def find_station_by_name(stations: Dict, name: str) -> Optional[Tuple[str, Dict]]:
    """Find a station by name (case-insensitive). Returns (station_name, station_data)."""
    name_lower = name.lower().strip()
    if name_lower in stations:
        return (name_lower, stations[name_lower])
    for station_name, station_data in stations.items():
        if name_lower in station_name.lower():
            return (station_name, station_data)
    return None


def identify_location(location: str, systems: Dict, stations: Dict) -> Tuple[str, Optional[Dict], Optional[Dict]]:
    """
    Identify if location is a station or system.
    Returns (type, system_data, station_data)
    type is either 'system' or 'station'
    station_data will be None for systems

    Priority: Try system first, then station as fallback.
    """
    # First try to find as system
    system = find_system_by_name(systems, location)
    if system:
        return 'system', system, None

    # Then try as station
    result = find_station_by_name(stations, location)
    if result:
        station_name, station_data = result
        # Return station data with name added
        station_with_name = dict(station_data)
        station_with_name['name'] = station_name
        return 'station', station_with_name, station_with_name

    raise ValueError(f"Could not find location: {location}")


def calculate_distance(pos1: Tuple[float, float, float], pos2: Tuple[float, float, float]) -> float:
    """Calculate 3D Euclidean distance between two positions."""
    return math.sqrt((pos1[0] - pos2[0])**2 + (pos1[1] - pos2[1])**2 + (pos1[2] - pos2[2])**2)


def calculate_warp_time(distance_m: float, warp_speed_au_s: float, top_speed: float) -> float:
    """Calculate warp time according to the given model. Returns seconds."""
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




def find_lexicographic_first_system(jumps: Dict, systems: Dict, current_system_id: int) -> int:
    """Find the lexicographically first system connected to current system."""
    connected = jumps.get(current_system_id, set())
    if not connected:
        return None

    # Get names and find lexicographically first
    min_name = None
    min_id = None
    for sys_id in connected:
        if sys_id in systems:
            name = systems[sys_id]['name']
            if min_name is None or name < min_name:
                min_name = name
                min_id = sys_id

    return min_id


def find_shortest_path(jumps: Dict, start_system_id: int, end_system_id: int,
                       zarzakh_id: int = None) -> List[int]:
    """Find shortest path using BFS, with special handling for Zarzakh."""
    if start_system_id == end_system_id:
        return [start_system_id]

    queue = deque([(start_system_id, None, [start_system_id])])
    visited = set()

    while queue:
        current, locked_gate, path = queue.popleft()

        if current == end_system_id:
            return path

        state = (current, locked_gate)
        if state in visited:
            continue
        visited.add(state)

        if current == zarzakh_id and locked_gate is not None:
            neighbors = [(n, (zarzakh_id, n)) for n in jumps.get(current, set())
                        if n == locked_gate[0]]
        else:
            neighbors = [(n, (current, n) if n == zarzakh_id else None)
                        for n in jumps.get(current, set())]

        for neighbor, new_lock in neighbors:
            queue.append((neighbor, new_lock, path + [neighbor]))

    return None


def format_time(total_seconds: float) -> str:
    """Format time in HH:MM format, rounded up to nearest minute."""
    minutes_ceiled = math.ceil(total_seconds / 60)
    hours = minutes_ceiled // 60
    minutes = minutes_ceiled % 60
    return f"{hours:02d}:{minutes:02d}"


def main():
    parser = argparse.ArgumentParser(description='Hauling route planner for New Eden')
    parser.add_argument('start', help='Starting location (station or system name)')
    parser.add_argument('end', help='Ending location (station or system name)')
    parser.add_argument('--align', type=float, required=True,
                        help='Time in seconds to align pre-warp (must be > 0)')
    parser.add_argument('--top-speed', type=float, required=True, dest='top_speed')
    parser.add_argument('--warp-speed', type=float, required=True, dest='warp_speed')
    parser.add_argument('--dock-time', type=float, required=True, dest='dock_time')
    parser.add_argument('--gate-time', type=float, required=True, dest='gate_time')
    parser.add_argument('--sde', required=True)
    args = parser.parse_args()

    for name in ['align', 'warp_speed', 'dock_time', 'gate_time']:
        if getattr(args, name) <= 0:
            raise ValueError(f"--{name} must be > 0")
    if args.top_speed < 0:
        raise ValueError("--top-speed must be >= 0")

    systems, jumps, stations, _, _ = load_sde_data(args.sde)
    zarzakh_id = next((sid for sid, d in systems.items() if d['name'].lower() == 'zarzakh'), None)

    start_type, start_sys, start_st = identify_location(args.start, systems, stations)
    end_type, end_sys, end_st = identify_location(args.end, systems, stations)

    total_time = 0.0
    print(f"START: {start_st['name'] if start_st else start_sys['name']}")

    if start_type == 'station':
        print("UNDOCK")
        total_time += args.dock_time

    # Determine system IDs
    start_sys_id = start_st['system_id'] if start_type == 'station' else start_sys['id']
    end_sys_id = end_st['system_id'] if end_type == 'station' else end_sys['id']

    # Early exit for same system
    if start_sys_id == end_sys_id:
        if end_type == 'station':
            print(f"DOCK: {end_st['name']}")
            total_time += args.dock_time
        print(f"DONE: {format_time(total_time)}")
        return

    path = find_shortest_path(jumps, start_sys_id, end_sys_id, zarzakh_id)

    if not path:
        print("ERROR: No path found", file=sys.stderr)
        sys.exit(1)

    route_str = ' -> '.join(f"{systems[sid]['name']} ({systems[sid]['security']:.1f})" for sid in path)
    print(f"GO: {route_str}")

    for i in range(len(path) - 1):
        s, t = path[i], path[i+1]
        pos1 = (systems[s]['x'], systems[s]['y'], systems[s]['z'])
        pos2 = (systems[t]['x'], systems[t]['y'], systems[t]['z'])
        total_time += args.align + calculate_warp_time(calculate_distance(pos1, pos2), args.warp_speed, args.top_speed)
        if i < len(path) - 2 or end_type == 'station':
            total_time += args.gate_time

    if end_type == 'station':
        total_time += args.dock_time
        print(f"DOCK: {end_st['name']}")

    print(f"DONE: {format_time(total_time)}")


if __name__ == '__main__':
    main()
