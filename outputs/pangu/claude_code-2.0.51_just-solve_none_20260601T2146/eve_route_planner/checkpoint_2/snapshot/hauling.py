#!/usr/bin/env python3
"""
Hauling route planner for New Eden using EVE SDE with manifest support.
"""

import argparse
import csv
import math
import os
import sys
import bz2
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple, Set
import yaml


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


def load_sde_data(sde_dir: str) -> Tuple[Dict, Dict, Dict, Dict, Dict]:
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
    """
    # First try to find as system
    system = find_system_by_name(systems, location)
    if system:
        return 'system', system, None

    # Then try as station
    result = find_station_by_name(stations, location)
    if result:
        station_name, station_data = result
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


def find_shortest_path(jumps: Dict, systems: Dict, start_system_id: int, end_system_id: int,
                       allow_low_sec: bool = False) -> Optional[List[int]]:
    """Find shortest path using BFS, with optional low-sec restriction."""
    if start_system_id == end_system_id:
        return [start_system_id]

    queue = deque([start_system_id])
    visited = {start_system_id}
    parent = {start_system_id: None}

    while queue:
        current = queue.popleft()

        if current == end_system_id:
            path = []
            while current is not None:
                path.append(current)
                current = parent[current]
            return path[::-1]

        for neighbor in jumps.get(current, set()):
            if neighbor not in visited:
                if not allow_low_sec and systems[neighbor]['security'] < 0.5:
                    continue
                visited.add(neighbor)
                parent[neighbor] = current
                queue.append(neighbor)

    return None


def find_path_with_freighter_restriction(jumps: Dict, systems: Dict, start_system_id: int,
                                         end_system_id: int) -> Optional[List[int]]:
    """Find path for freighter - only high-sec unless no alternative."""
    high_sec_path = find_shortest_path(jumps, systems, start_system_id, end_system_id, allow_low_sec=False)
    if high_sec_path:
        return high_sec_path
    return find_shortest_path(jumps, systems, start_system_id, end_system_id, allow_low_sec=True)


def format_time(total_seconds: float) -> str:
    """Format time in HH:MM format, rounded up to nearest minute."""
    minutes_ceiled = math.ceil(total_seconds / 60)
    hours = minutes_ceiled // 60
    minutes = minutes_ceiled % 60
    return f"{hours:02d}:{minutes:02d}"


def format_cargo_amount(amount: float) -> str:
    """Format cargo amount with comma-separated thousands."""
    return f"{amount:,.2f}"


def load_config(config_path: str) -> Dict:
    """Load configuration YAML file."""
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def load_manifest(manifest_path: str) -> Dict:
    """Load manifest YAML file."""
    with open(manifest_path, 'r') as f:
        return yaml.safe_load(f)


def calculate_total_moved(waypoints: List[Dict]) -> float:
    """Calculate total cargo moved (sum of absolute values)."""
    total = 0.0
    for wp in waypoints:
        cargo = wp.get('cargo')
        if cargo is not None:
            total += abs(cargo)
    return total


def find_route_for_waypoint(calculator, current_system_id: int, wp: Dict) -> Tuple[List[int], str, Dict]:
    """Find route to a waypoint and return (route, path_string, wp_type, wp_sys, wp_st)."""
    wp_name = wp['name']
    wp_type, wp_sys, wp_st = identify_location(wp_name, calculator.systems, calculator.stations)
    wp_system_id = wp_st['system_id'] if wp_type == 'station' else wp_sys['id']

    if calculator.is_freighter:
        route = find_path_with_freighter_restriction(calculator.jumps, calculator.systems,
                                                    current_system_id, wp_system_id)
    else:
        route = find_shortest_path(calculator.jumps, calculator.systems,
                                   current_system_id, wp_system_id, allow_low_sec=True)

    if not route:
        raise ValueError(f"No route found from {calculator.systems[current_system_id]['name']} "
                       f"to {wp_name}")

    path_str = ' -> '.join(f"{calculator.systems[sid]['name']} ({calculator.systems[sid]['security']:.1f})"
                           for sid in route)
    return route, path_str, wp_type, wp_sys, wp_st


class HaulingCalculator:
    """Calculate hauling operations with waypoints and multiple trips."""

    def __init__(self, config: Dict, ship_name: str, systems: Dict, jumps: Dict,
                 stations: Dict, times: Dict):
        self.config = config
        self.ship_name = ship_name
        self.systems = systems
        self.jumps = jumps
        self.stations = stations
        self.times = times

        if ship_name not in config['ships']:
            raise ValueError(f"Ship '{ship_name}' not found in config")
        self.ship = config['ships'][ship_name]
        self.ship_type = self.ship['type']
        self.align = self.ship['align']
        self.top_speed = self.ship['top_speed']
        self.warp_speed = self.ship['warp_speed']
        self.cargo_size = self.ship['cargo_size']
        self.is_freighter = self.ship_type == 'Freighter'

    def calculate_path_time(self, path: List[int]) -> float:
        """Calculate total time for a path."""
        total_time = 0.0
        for i in range(len(path) - 1):
            s, t = path[i], path[i+1]
            pos1 = (self.systems[s]['x'], self.systems[s]['y'], self.systems[s]['z'])
            pos2 = (self.systems[t]['x'], self.systems[t]['y'], self.systems[t]['z'])
            total_time += self.align + calculate_warp_time(calculate_distance(pos1, pos2),
                                                           self.warp_speed, self.top_speed)
            total_time += self.times['gate']
        return total_time

    def get_path_string(self, path: List[int]) -> str:
        """Get formatted path string with security levels."""
        return ' -> '.join(f"{self.systems[sid]['name']} ({self.systems[sid]['security']:.1f})"
                          for sid in path)

    def find_route(self, start_system_id: int, end_system_id: int) -> Optional[List[int]]:
        """Find appropriate route based on ship type."""
        if self.is_freighter:
            return find_path_with_freighter_restriction(self.jumps, self.systems,
                                                        start_system_id, end_system_id)
        return find_shortest_path(self.jumps, self.systems, start_system_id, end_system_id, allow_low_sec=True)

    def calculate_total_time(self, operations: List[Dict]) -> float:
        """Calculate total time for all operations."""
        total_time = 0.0
        for op in operations:
            if op['type'] == 'UNDOCK':
                total_time += self.times['dock']
            elif op['type'] == 'DOCK':
                total_time += self.times['dock']
            elif op['type'] == 'GO':
                route_str = op['route']
                route_parts = [p.split(' (')[0] for p in route_str.split(' -> ')]
                path = []
                for part in route_parts:
                    sys_info = find_system_by_name(self.systems, part)
                    if sys_info:
                        path.append(sys_info['id'])
                if len(path) > 1:
                    total_time += self.calculate_path_time(path)
            elif op['type'] == 'LOAD':
                total_time += self.times['move_cargo']
            elif op['type'] == 'UNLOAD':
                total_time += self.times['move_cargo']
        return total_time


def plan_trip(calc: HaulingCalculator, start_station: Dict, end_station: Dict,
              cargo_waypoints: List[Dict], starting_cargo: float, is_first_trip: bool) -> List[Dict]:
    """
    Plan a single hauling trip through waypoints.
    Returns list of operations.
    """
    operations = []

    # Start system
    start_type, start_sys, start_st = identify_location(start_station['name'], calc.systems, calc.stations)
    current_system = start_st['system_id'] if start_type == 'station' else start_sys['id']

    # Add START only for first trip
    if is_first_trip:
        operations.append({'type': 'START', 'location': start_station['name']})

    # Starting from station?
    if start_type == 'station':
        operations.append({'type': 'UNDOCK'})

    # Current cargo
    current_cargo = starting_cargo

    # Process waypoints
    for i, wp in enumerate(cargo_waypoints):
        wp_cargo = wp.get('cargo')
        if wp_cargo is None:
            continue

        # Find route to waypoint
        route, path_str, wp_type, wp_sys, wp_st = find_route_for_waypoint(calc, current_system, wp)
        wp_system_id = wp_st['system_id'] if wp_type == 'station' else wp_sys['id']

        # Add GO
        operations.append({'type': 'GO', 'route': path_str})
        current_system = wp_system_id

        # Dock if station
        if wp_type == 'station':
            operations.append({'type': 'DOCK', 'location': wp['name']})

        # Cargo operation
        abs_cargo = abs(wp_cargo)
        if wp_cargo > 0:  # Pickup
            space = calc.cargo_size - current_cargo
            actual = min(abs_cargo, space)
            if actual > 0:
                operations.append({'type': 'LOAD', 'amount': actual})
                current_cargo += actual
        else:  # Dropoff
            if current_cargo >= abs_cargo:
                operations.append({'type': 'UNLOAD', 'amount': abs_cargo})
                current_cargo -= abs_cargo

        # Undock if station and more cargo stops
        remaining = [w for j, w in enumerate(cargo_waypoints) if j > i and w.get('cargo') is not None]
        if wp_type == 'station' and remaining:
            operations.append({'type': 'UNDOCK'})

    # Return to end station if carrying cargo
    if current_cargo > 0:
        route, path_str, end_type, end_sys, end_st = find_route_for_waypoint(calc, current_system, end_station)
        operations.append({'type': 'GO', 'route': path_str})
        if end_type == 'station':
            operations.append({'type': 'DOCK', 'location': end_station['name']})
            operations.append({'type': 'UNLOAD', 'amount': current_cargo})

    return operations


def main():
    parser = argparse.ArgumentParser(description='Hauling route planner for New Eden')
    parser.add_argument('start', help='Starting location (station or system name)')
    parser.add_argument('end', help='Ending location (station or system name)')
    parser.add_argument('--manifest', required=True, help='Path to manifest YAML file')
    parser.add_argument('--config', required=True, help='Path to config YAML file')
    parser.add_argument('--ship', required=True, help='Ship name from config')
    parser.add_argument('--sde', required=True, help='Path to SDE directory')
    args = parser.parse_args()

    # Load config and manifest
    config = load_config(args.config)
    if 'ships' not in config or 'times' not in config:
        raise ValueError("Config must contain 'ships' and 'times' sections")

    manifest = load_manifest(args.manifest)
    start_cargo = manifest.get('start_cargo', 0) or 0
    waypoints = manifest.get('waypoints', [])

    # Load SDE
    systems, jumps, stations, _, _ = load_sde_data(args.sde)

    # Identify locations
    start_type, start_sys, start_st = identify_location(args.start, systems, stations)
    end_type, end_sys, end_st = identify_location(args.end, systems, stations)

    # Ship specs
    if args.ship not in config['ships']:
        raise ValueError(f"Ship '{args.ship}' not found in config")
    calc = HaulingCalculator(config, args.ship, systems, jumps, stations, config['times'])

    # Station info
    if start_type == 'station':
        start_station = start_st
    else:
        sid = start_sys['id']
        start_station = next(({'name': n, 'system_id': d['system_id']} for n, d in stations.items()
                             if d['system_id'] == sid), None)
        if not start_station:
            raise ValueError(f"No station in system: {start_sys['name']}")

    if end_type == 'station':
        end_station = end_st
    else:
        sid = end_sys['id']
        end_station = next(({'name': n, 'system_id': d['system_id']} for n, d in stations.items()
                           if d['system_id'] == sid), None)
        if not end_station:
            raise ValueError(f"No station in system: {end_sys['name']}")

    # Filter waypoints with cargo
    cargo_waypoints = [wp for wp in waypoints if wp.get('cargo') is not None]
    total_moved = calculate_total_moved(cargo_waypoints)
    if start_cargo:
        total_moved += start_cargo

    # No cargo at all
    if not cargo_waypoints and start_cargo == 0:
        print(f"START: {start_station['name']}")
        if start_type == 'station':
            print("UNDOCK")
        sid = start_station['system_id'] if 'system_id' in start_station else start_sys['id']
        eid = end_station['system_id'] if 'system_id' in end_station else end_sys['id']
        route = calc.find_route(sid, eid)
        if route:
            print(f"GO: {calc.get_path_string(route)}")
            if end_type == 'station':
                print(f"DOCK: {end_station['name']}")
        tt = 0.0
        if start_type == 'station':
            tt += calc.times['dock']
        if route and len(route) > 1:
            tt += calc.calculate_path_time(route)
        if end_type == 'station':
            tt += calc.times['dock']
        print(f"DONE: {format_time(tt)}")
        return

    # Plan trips - iterate through waypoints in order
    all_ops = []
    trip_idx = 1
    current_cargo = start_cargo
    processed = set()

    while len(processed) < len(cargo_waypoints) or current_cargo > 0:
        if trip_idx > 1:
            all_ops.append({'type': 'TRIP_SEPARATOR', 'index': trip_idx})

        # Select unprocessed waypoints for this trip
        trip_wps = []
        loaded = 0.0

        for i, wp in enumerate(cargo_waypoints):
            if i in processed:
                continue
            c = wp.get('cargo', 0) or 0
            if c > 0:  # Pickup
                if loaded + c <= calc.cargo_size:
                    trip_wps.append(wp)
                    processed.add(i)
                    loaded += c
            else:  # Dropoff - always process all dropoffs we can
                if current_cargo >= abs(c):
                    trip_wps.append(wp)
                    processed.add(i)

        # If no waypoints selected, we're done
        if not trip_wps and current_cargo == 0:
            break

        # Start location
        trip_start = start_station if trip_idx == 1 else end_station
        trip_is_first = trip_idx == 1

        # Plan trip
        trip_ops = plan_trip(calc, trip_start, end_station, trip_wps, current_cargo, trip_is_first)

        # Remove START for subsequent trips
        if trip_idx > 1:
            trip_ops = [op for op in trip_ops if op['type'] != 'START']

        all_ops.extend(trip_ops)

        # Calculate remaining cargo
        new_cargo = current_cargo
        for wp in trip_wps:
            c = wp.get('cargo', 0) or 0
            if c > 0:
                new_cargo = min(new_cargo + c, calc.cargo_size)
            elif c < 0:
                new_cargo = max(0, new_cargo + c)
        current_cargo = new_cargo

        trip_idx += 1

    # Total time calculation
    total_time = 0.0
    for op in all_ops:
        if op['type'] == 'UNDOCK':
            total_time += calc.times['dock']
        elif op['type'] == 'DOCK':
            total_time += calc.times['dock']
        elif op['type'] == 'GO':
            parts = [p.split(' (')[0] for p in op['route'].split(' -> ')]
            path = [s for part in parts for s in [find_system_by_name(calc.systems, part)] if s]
            if len(path) > 1:
                total_time += calc.calculate_path_time(path)
        elif op['type'] == 'LOAD':
            total_time += calc.times['move_cargo']
        elif op['type'] == 'UNLOAD':
            total_time += calc.times['move_cargo']

    # Output
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
            print(f"LOAD: {format_cargo_amount(op['amount'])} m3")
        elif t == 'UNLOAD':
            print(f"UNLOAD: {format_cargo_amount(op['amount'])} m3")
        elif t == 'TRIP_SEPARATOR':
            print(f"[--- TRIP {op['index']} ---]")

    print(f"DONE: {format_time(total_time)}")
    if total_moved > 0:
        print(f"MOVED: {format_cargo_amount(total_moved)} m3")


if __name__ == '__main__':
    main()
