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
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set


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
    for sys_id, sys_data in systems.items():
        if name_lower == sys_data['name'].lower():
            # Return dict with ID included
            result = dict(sys_data)
            result['id'] = sys_id
            return result
    # Try partial match
    for sys_id, sys_data in systems.items():
        if name_lower in sys_data['name'].lower():
            result = dict(sys_data)
            result['id'] = sys_id
            return result
    return None


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


def calculate_warp_time(distance_m: float, warp_speed_au_s: float, top_speed: float,
                        align_time: float, dock_time: float) -> float:
    """
    Calculate warp time according to the given model.
    Returns time in seconds.
    """
    # Convert speeds
    v_s = top_speed  # Sub warp speed in m/s
    v_drop = min(v_s / 2, 100)  # Dropout speed in m/s

    # k_a and k_d are acceleration/deceleration rates in AU/s
    # The formula gives us the actual peak warp speed based on distance
    k_a = warp_speed_au_s  # Acceleration rate in AU/s
    k_d = min(warp_speed_au_s / 3, 2)  # Deceleration rate in AU/s, capped at 2 AU/s

    # Maximum possible warp speed (when D >= d_min)
    v_warp_max_ms = warp_speed_au_s * AU_IN_M  # in m/s

    # Distances in meters
    d_a = AU_IN_M  # Acceleration distance = 1 AU in meters
    d_d = v_warp_max_ms / k_d  # Deceleration distance in meters
    d_min = d_a + d_d  # Minimum warp distance

    # Distances
    d_a = AU_IN_M  # Acceleration distance = 1 AU in meters
    d_d = v_warp_max_ms / k_d  # Deceleration distance in meters
    d_min = d_a + d_d  # Minimum warp distance

    D = distance_m

    # Calculate warp speed
    if D < d_min:
        # No cruise phase, peak speed is reduced
        v_warp_ms = (D * k_a * k_d) / (k_a + k_d)
    else:
        v_warp_ms = v_warp_max_ms

    # Calculate times
    # Convert to AU for acceleration/deceleration calculations
    v_warp_au_s = v_warp_ms / AU_IN_M
    k_a_au_s = k_a
    k_d_au_s = k_d
    v_drop_au_s = v_drop / AU_IN_M

    # Acceleration time
    t_accel = (1 / k_a_au_s) * math.log(v_warp_au_s / k_a_au_s)

    # Deceleration time
    t_decel = (1 / k_d_au_s) * math.log(v_warp_au_s / v_drop_au_s)

    # Cruise time
    if D >= d_min:
        t_cruise = (D - d_min) / v_warp_ms
    else:
        t_cruise = 0

    return t_accel + t_cruise + t_decel


def get_system_position(system: Dict) -> Tuple[float, float, float]:
    """Get system position (using the sun/station position from the system data)."""
    return (system['x'], system['y'], system['z'])


def get_gate_position_between_systems(systems: Dict, from_id: int, to_id: int) -> Tuple[float, float, float]:
    """
    Calculate gate position between two systems.
    For simplicity, use a position roughly at the midpoint between the two system centers.
    In EVE, gates are typically located at a specific position in the system.
    We'll approximate this as being at the system's position for simplicity.
    """
    from_sys = systems[from_id]
    to_sys = systems[to_id]

    from_pos = get_system_position(from_sys)
    to_pos = get_system_position(to_sys)

    # Return the position of the "from" system (gate is in the from system)
    return from_pos


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
    """
    Find shortest path using BFS, with special handling for Zarzakh.
    Returns list of system IDs from start to end (including both).
    """
    if start_system_id == end_system_id:
        return [start_system_id]

    # Handle Zarzakh locking
    if zarzakh_id:
        # BFS with state (system_id, locked_gate)
        # locked_gate is None or (from_system_id, to_system_id)
        visited = set()
        queue = deque()
        queue.append((start_system_id, None, [start_system_id]))

        while queue:
            current, locked_gate, path = queue.popleft()

            # Check if we reached the end
            if current == end_system_id:
                return path

            # Check Zarzakh lock
            if current == zarzakh_id and locked_gate is not None:
                # Only allow exit through the gate we entered from
                allowed_exit = locked_gate[0]  # From that system
                neighbors = [(n, (zarzakh_id, n)) for n in jumps.get(current, set())
                            if n == allowed_exit]
            else:
                neighbors = [(n, (current, n) if n == zarzakh_id else None)
                            for n in jumps.get(current, set())]

            for neighbor, new_lock in neighbors:
                state = (neighbor, new_lock)
                if state not in visited:
                    visited.add(state)
                    new_path = path + [neighbor]
                    queue.append((neighbor, new_lock, new_path))
    else:
        # Regular BFS without Zarzakh
        visited = set([start_system_id])
        queue = deque()
        queue.append((start_system_id, [start_system_id]))

        while queue:
            current, path = queue.popleft()

            if current == end_system_id:
                return path

            for neighbor in jumps.get(current, set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    new_path = path + [neighbor]
                    queue.append((neighbor, new_path))

    return None  # No path found


def format_time(total_seconds: float) -> str:
    """Format time in HH:MM format, rounded up to nearest minute."""
    minutes_ceiled = math.ceil(total_seconds / 60)
    hours = minutes_ceiled // 60
    minutes = minutes_ceiled % 60
    return f"{hours:02d}:{minutes:02d}"


def main():
    parser = argparse.ArgumentParser(
        description='Hauling route planner for New Eden'
    )
    parser.add_argument('start', help='Starting location (station or system name)')
    parser.add_argument('end', help='Ending location (station or system name)')
    parser.add_argument('--align', type=float, required=True,
                        help='Time in seconds to align pre-warp (must be > 0)')
    parser.add_argument('--top-speed', type=float, required=True, dest='top_speed',
                        help='Maximum subwarp speed in m/s (must be >= 0)')
    parser.add_argument('--warp-speed', type=float, required=True, dest='warp_speed',
                        help='Maximum warp speed in AU/s (must be > 0)')
    parser.add_argument('--dock-time', type=float, required=True, dest='dock_time',
                        help='Time in seconds to dock/undock (must be > 0)')
    parser.add_argument('--gate-time', type=float, required=True, dest='gate_time',
                        help='Time in seconds to use a gate (must be > 0)')
    parser.add_argument('--sde', required=True, help='Path to SDE directory')

    args = parser.parse_args()

    # Validate arguments
    if args.align <= 0:
        raise ValueError("--align must be > 0")
    if args.top_speed < 0:
        raise ValueError("--top-speed must be >= 0")
    if args.warp_speed <= 0:
        raise ValueError("--warp-speed must be > 0")
    if args.dock_time <= 0:
        raise ValueError("--dock-time must be > 0")
    if args.gate_time <= 0:
        raise ValueError("--gate-time must be > 0")

    # Load SDE data
    systems, jumps, stations, station_by_id, denormalize = load_sde_data(args.sde)

    # Find Zarzakh system ID (system name is "Zarzakh")
    zarzakh_id = None
    for sys_id, sys_data in systems.items():
        if sys_data['name'].lower() == 'zarzakh':
            zarzakh_id = sys_id
            break

    # Identify start and end locations
    start_type, start_system, start_station = identify_location(args.start, systems, stations)
    end_type, end_system, end_station = identify_location(args.end, systems, stations)

    # Calculate total time
    total_time = 0.0

    # START line
    start_display = start_station['name'] if start_station else start_system['name']
    print(f"START: {start_display}")

    # Handle starting in a station (must undock)
    if start_type == 'station':
        print("UNDOCK")
        total_time += args.dock_time
        current_location = start_station
        current_system_id = start_station['system_id']
    else:
        # Starting in space at the gate to the first system lexicographically
        first_lex = find_lexicographic_first_system(jumps, systems, start_system['id'])
        current_system_id = start_system['id']

    # If start and end are the same
    if (start_type == 'station' and end_type == 'station' and
        start_station['system_id'] == end_station['system_id']):
        # Must dock
        print(f"DOCK: {end_station['name']}")
        total_time += args.dock_time
        print(f"DONE: {format_time(total_time)}")
        return

    if (start_type == 'system' and end_type == 'system' and
        start_system['id'] == end_system['id']):
        # Same system, just warp directly to station if needed
        if end_type == 'station':
            print(f"DOCK: {end_station['name']}")
            total_time += args.dock_time
        else:
            # Just entering system, done
            pass
        print(f"DONE: {format_time(total_time)}")
        return

    # Find path
    if start_type == 'station':
        start_sys_id = start_station['system_id']
    else:
        start_sys_id = start_system['id']

    end_sys_id = end_station['system_id'] if end_station else end_system['id']

    path = find_shortest_path(jumps, start_sys_id, end_sys_id, zarzakh_id)

    if not path:
        print("ERROR: No path found", file=sys.stderr)
        sys.exit(1)

    # Build route string
    route_systems = [systems[sid]['name'] for sid in path]
    route_with_security = []
    for i, sys_id in enumerate(path):
        sys_name = systems[sys_id]['name']
        security = systems[sys_id]['security']
        route_with_security.append(f"{sys_name} ({security:.1f})")

    route_str = " -> ".join(route_with_security)

    # Calculate time for the route
    # For each hop in the path:
    # - Align
    # - Warp
    # - Gate (except last if ending in system)
    # - Dock (if ending in station)

    for i in range(len(path) - 1):
        from_sys_id = path[i]
        to_sys_id = path[i + 1]

        # Get positions (approximate gates at system positions)
        from_pos = get_system_position(systems[from_sys_id])
        to_pos = get_system_position(systems[to_sys_id])

        distance = calculate_distance(from_pos, to_pos)

        # Warp time
        warp_time = calculate_warp_time(
            distance, args.warp_speed, args.top_speed,
            args.align, args.dock_time
        )
        total_time += args.align + warp_time

        # Gate time (unless this is the last hop and ending in a system)
        if i < len(path) - 2 or end_type == 'station':
            total_time += args.gate_time

    # Handle docking if ending at station
    if end_type == 'station':
        total_time += args.dock_time

    # Output GO line
    print(f"GO: {route_str}")

    # Output DOCK line if ending at station
    if end_type == 'station':
        print(f"DOCK: {end_station['name']}")

    # Output DONE line
    print(f"DONE: {format_time(total_time)}")


if __name__ == '__main__':
    main()
