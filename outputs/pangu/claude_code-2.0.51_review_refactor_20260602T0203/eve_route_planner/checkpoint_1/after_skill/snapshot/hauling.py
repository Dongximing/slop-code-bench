#!/usr/bin/env python3
"""
Hauling route planner for New Eden using EVE SDE.
Calculates optimal travel routes between stations and systems.
"""

import argparse
import math
import sys
import csv
import bz2
from collections import defaultdict
from typing import Dict, List, Optional, Tuple, Set
import heapq

# Constants
AU_IN_M = 149597870700.0  # 1 Astronomical Unit in meters

# Zarzakh system ID (from EVE Online)
ZARZAKH_ID = 30002667

# Lock reset wait time in seconds (6 hours)
WAIT_TIME = 6 * 3600  # 21600 seconds


def format_duration(total_time: float) -> str:
    """Format time in seconds as HH:MM, rounding up to nearest minute."""
    total_minutes = math.ceil(total_time / 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours:02d}:{minutes:02d}"


def load_compressed_csv(filepath: str) -> List[Dict]:
    """Load a CSV file that may be compressed with bz2."""
    if filepath.endswith('.bz2'):
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            return list(reader)
    else:
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            return list(reader)


def load_sde_data(sde_path: str) -> Tuple[Dict, Dict, Dict, Dict]:
    """Load all SDE data files.

    Returns:
        systems: dict of system_id -> {name, security, x, y, z}
        jumps: dict of system_id -> set of connected system IDs
        stations: dict of station_id -> {name, system_id, x, y, z}
        denormalize: dict of item_id -> {type_id, group_id, solar_system_id, x, y, z}
    """
    # Load solar systems
    systems = {}
    systems_data = load_compressed_csv(f"{sde_path}/mapSolarSystems.csv.bz2")
    for row in systems_data:
        system_id = int(row['solarSystemID'])
        systems[system_id] = {
            'name': row['solarSystemName'],
            'security': float(row['security']),
            'x': float(row['x']),
            'y': float(row['y']),
            'z': float(row['z'])
        }

    # Load solar system jumps
    jumps = defaultdict(set)
    jumps_data = load_compressed_csv(f"{sde_path}/mapSolarSystemJumps.csv.bz2")
    for row in jumps_data:
        from_id = int(row['fromSolarSystemID'])
        to_id = int(row['toSolarSystemID'])
        jumps[from_id].add(to_id)
        jumps[to_id].add(from_id)

    # Load stations
    stations = {}
    stations_data = load_compressed_csv(f"{sde_path}/staStations.csv.bz2")
    for row in stations_data:
        station_id = int(row['stationID'])
        stations[station_id] = {
            'name': row['stationName'],
            'system_id': int(row['solarSystemID']),
            'x': float(row['x']),
            'y': float(row['y']),
            'z': float(row['z'])
        }

    # Load denormalize for gate/planet positions
    denormalize = {}
    denormalize_data = load_compressed_csv(f"{sde_path}/mapDenormalize.csv.bz2")
    for row in denormalize_data:
        item_id = int(row['itemID'])
        solar_system_id = row.get('solarSystemID', '0')
        if solar_system_id == 'None' or not solar_system_id:
            solar_system_id = 0
        else:
            solar_system_id = int(solar_system_id)
        denormalize[item_id] = {
            'type_id': int(row.get('typeID', 0) or 0),
            'group_id': int(row.get('groupID', 0) or 0),
            'solar_system_id': solar_system_id,
            'x': float(row.get('x', 0) or 0),
            'y': float(row.get('y', 0) or 0),
            'z': float(row.get('z', 0) or 0),
            'name': row.get('itemName', '')
        }

    return systems, jumps, stations, denormalize


def parse_location(location_str: str, systems: Dict, stations: Dict) -> Tuple[str, int, int, float, float, float]:
    """Parse a location string and return (name, type, system_id, x, y, z)."""
    # First check if it's a station (exact name match)
    for station_id, station in stations.items():
        if station['name'] == location_str:
            return (location_str, 0, station['system_id'],
                    station['x'], station['y'], station['z'])

    # Check if it's a system
    for system_id, system in systems.items():
        if system['name'] == location_str:
            return (location_str, 1, system_id,
                    system['x'], system['y'], system['z'])

    # Try partial match (for things like "Jita" matching "Jita IV - Moon 4 - ...")
    for station_id, station in stations.items():
        if location_str in station['name']:
            return (station['name'], 0, station['system_id'],
                    station['x'], station['y'], station['z'])

    for system_id, system in systems.items():
        if location_str in system['name']:
            return (system['name'], 1, system_id,
                    system['x'], system['y'], system['z'])

    raise ValueError(f"Location '{location_str}' not found")


def calculate_warp_time(distance: float, max_warp_speed_au: float, subwarp_speed_ms: float,
                        align_time: float) -> float:
    """Calculate warp time for a given distance.

    Args:
        distance: Distance in meters
        max_warp_speed_au: Maximum warp speed in AU/s
        subwarp_speed_ms: Subwarp speed in m/s
        align_time: Alignment time in seconds (ceiling due to server ticks)

    Returns:
        Total time in seconds including align time
    """
    # Warp model calculations
    v_warp_ms = max_warp_speed_au * AU_IN_M  # Max warp speed in m/s
    v_drop = min(subwarp_speed_ms / 2, 100)  # Dropout speed in m/s

    k_a = max_warp_speed_au  # Acceleration rate in AU/s
    k_d = min(max_warp_speed_au / 3, 2)  # Deceleration rate in AU/s, capped at 2

    # Distances
    d_a = AU_IN_M  # Acceleration distance = 1 AU
    d_d = v_warp_ms / k_d  # Deceleration distance
    d_min = d_a + d_d  # Minimum warp distance

    # Adjust warp speed if distance is less than minimum
    if distance < d_min:
        v_warp_ms = (distance * k_a * k_d) / (k_a + k_d)

    v_warp_au = v_warp_ms / AU_IN_M

    # Calculate times
    # Acceleration time: t_accel = (1/k_a) * ln(v_warp_ms / (k_a * AU_IN_M))
    if v_warp_ms > k_a * AU_IN_M:
        t_accel = (1 / k_a) * math.log(v_warp_ms / (k_a * AU_IN_M))
    else:
        t_accel = 0

    # Deceleration time: t_decel = (1/k_d) * ln(v_warp_ms / v_drop)
    if v_warp_ms > v_drop:
        t_decel = (1 / k_d) * math.log(v_warp_ms / v_drop)
    else:
        t_decel = 0

    # Cruise time
    if distance >= d_min:
        t_cruise = (distance - (d_a + d_d)) / v_warp_ms
    else:
        t_cruise = 0

    total_warp_time = t_accel + t_cruise + t_decel

    # Include align time
    return align_time + total_warp_time


def get_system_distance(sys1: Dict, sys2: Dict) -> float:
    """Calculate 3D distance between two systems."""
    dx = sys1['x'] - sys2['x']
    dy = sys1['y'] - sys2['y']
    dz = sys1['z'] - sys2['z']
    return math.sqrt(dx*dx + dy*dy + dz*dz)


def find_route(start_loc: str, end_loc: str, align_time: float, top_speed: float,
               warp_speed: float, dock_time: float, gate_time: float,
               sde_path: str) -> None:
    """Find and output the optimal route."""

    # Load SDE data
    systems, jumps, stations, denormalize = load_sde_data(sde_path)

    # Parse start and end locations
    start_name, start_type, start_sys_id, start_x, start_y, start_z = \
        parse_location(start_loc, systems, stations)
    end_name, end_type, end_sys_id, end_x, end_y, end_z = \
        parse_location(end_loc, systems, stations)

    # Special case: start and end are the same location
    if start_type == 0 and end_type == 0 and start_sys_id == end_sys_id:
        # Check if it's actually the same station
        for station_id, station in stations.items():
            if (station['system_id'] == start_sys_id and
                station['name'] == start_name and
                station['name'] == end_name):
                # Same station - just dock
                print(f"START: {start_name}")
                print(f"DOCK: {end_name}")
                print("DONE: 00:01")
                return
        # Same system, different stations
        total_time = 2 * dock_time  # undock + dock
        print(f"START: {start_name}")
        print("UNDOCK")
        # Warp to the other station in same system
        dist = math.sqrt(
            (start_x - end_x)**2 + (start_y - end_y)**2 + (start_z - end_z)**2
        )
        warp_time = calculate_warp_time(dist, warp_speed, top_speed, align_time)
        total_time += warp_time
        start_system = systems[start_sys_id]
        print(f"GO: {start_system['name']} ({int(start_system['security'] * 10) / 10.0})")
        print(f"DOCK: {end_name}")
        print(f"DONE: {format_duration(total_time)}")
        return

    # Same system, no routing needed
    if start_sys_id == end_sys_id:
        total_time = dock_time  # Start with dock time
        print(f"START: {start_name}")

        if start_type == 0:
            print("UNDOCK")

        # No warp time needed - we're just docking within the same system
        start_system = systems[start_sys_id]

        if end_type == 0:
            print(f"DOCK: {end_name}")
        else:
            # Just entering the system from somewhere else in the system
            print(f"GO: {start_system['name']} ({int(start_system['security'] * 10) / 10.0})")

        print(f"DONE: {format_duration(total_time)}")
        return

    # For routing between different systems, use Dijkstra's algorithm
    # State: (current_system_id, entered_from_system_id)
    # entered_from_system_id is None if we haven't entered a system with Zarzakh lock

    # Priority queue: (total_time, current_system, entered_from)
    pq = [(0, start_sys_id, None)]

    # dist[(sys, from_sys)] = min time to reach this state
    dist = defaultdict(lambda: float('inf'))
    dist[(start_sys_id, None)] = 0

    # prev[(sys, from_sys)] = (prev_sys, prev_from)
    prev = {}

    while pq:
        current_time, current_sys, entered_from = heapq.heappop(pq)

        if current_time > dist[(current_sys, entered_from)]:
            continue

        # If we reached the destination
        if current_sys == end_sys_id:
            break

        current_system = systems[current_sys]

        # Check Zarzakh lock
        if current_sys == ZARZAKH_ID and entered_from is not None:
            # We're locked to the system we came from
            # Can only go back to that system
            allowed_neighbor = entered_from
        else:
            allowed_neighbor = None

        # Explore neighbors
        for neighbor_sys in jumps[current_sys]:
            # Check Zarzakh lock - can only go back to where we came from
            if (current_sys == ZARZAKH_ID and entered_from is not None and
                neighbor_sys != entered_from):
                continue

            neighbor_system = systems[neighbor_sys]

            # Calculate travel time between systems
            distance = get_system_distance(current_system, neighbor_system)
            warp_time = calculate_warp_time(distance, warp_speed, top_speed, align_time)
            total_segment_time = warp_time + gate_time

            new_time = current_time + total_segment_time
            new_state = (neighbor_sys, current_sys)

            if new_time < dist[new_state]:
                dist[new_state] = new_time
                prev[new_state] = (current_sys, entered_from)
                heapq.heappush(pq, (new_time, neighbor_sys, current_sys))

    # Also consider waiting in Zarzakh to reset the lock
    if start_sys_id == ZARZAKH_ID or any(state[0] == ZARZAKH_ID for state in dist):
        # For each state in Zarzakh, consider waiting 6 hours
        for (sys_id, entered_from), time in list(dist.items()):
            if sys_id == ZARZAKH_ID and entered_from is not None:
                new_time = time + WAIT_TIME
                new_state = (ZARZAKH_ID, None)  # Lock is reset

                if new_time < dist[new_state]:
                    dist[new_state] = new_time
                    prev[new_state] = (sys_id, entered_from)
                    heapq.heappush(pq, (new_time, ZARZAKH_ID, None))

    # Reconstruct the best path
    # Find the state with minimum time at the destination
    best_state = None
    best_time = float('inf')

    for state, time in dist.items():
        if state[0] == end_sys_id and time < best_time:
            best_time = time
            best_state = state

    if best_state is None:
        print("Error: No path found", file=sys.stderr)
        return

    # Reconstruct path
    path = []
    state = best_state
    while state is not None:
        path.append(state[0])
        prev_state = prev.get(state)
        if prev_state == state:  # Prevent infinite loop
            break
        state = prev_state
    path.reverse()

    # Calculate total time including docking if needed
    total_time = best_time

    # Add undock time if starting from station
    if start_type == 0:
        total_time += dock_time

    # Add dock time if ending at station
    if end_type == 0:
        total_time += dock_time

    # Output the result
    print(f"START: {start_name}")

    if start_type == 0:
        print("UNDOCK")

    # Build the GO line with system names and security
    system_names = []
    for sys_id in path:
        sys_info = systems[sys_id]
        sec = sys_info['security']
        # Format security to 1 decimal place
        sec_str = f"{sec:.1f}"
        system_names.append(f"{sys_info['name']} ({sec_str})")

    print(f"GO: {' -> '.join(system_names)}")

    if end_type == 0:
        print(f"DOCK: {end_name}")

    print(f"DONE: {format_duration(total_time)}")


def main():
    parser = argparse.ArgumentParser(
        description='Calculate travel routes in New Eden using EVE SDE.'
    )

    parser.add_argument('start', help='Starting location (station or system name)')
    parser.add_argument('end', help='Destination location (station or system name)')

    parser.add_argument('--align', type=float, required=True,
                       help='Time in seconds to align pre-warp (must be > 0)')
    parser.add_argument('--top-speed', type=float, dest='top_speed', required=True,
                       help='Maximum subwarp speed in m/s (must be >= 0)')
    parser.add_argument('--warp-speed', type=float, dest='warp_speed', required=True,
                       help='Maximum warp speed in AU/s (must be > 0)')
    parser.add_argument('--dock-time', type=float, dest='dock_time', required=True,
                       help='Time in seconds to dock/undock (must be > 0)')
    parser.add_argument('--gate-time', type=float, dest='gate_time', required=True,
                       help='Time in seconds to use a gate (must be > 0)')
    parser.add_argument('--sde', required=True,
                       help='Path to SDE directory')

    args = parser.parse_args()

    # Validate arguments
    if args.align <= 0:
        print("Error: --align must be > 0", file=sys.stderr)
        sys.exit(1)
    if args.top_speed < 0:
        print("Error: --top-speed must be >= 0", file=sys.stderr)
        sys.exit(1)
    if args.warp_speed <= 0:
        print("Error: --warp-speed must be > 0", file=sys.stderr)
        sys.exit(1)
    if args.dock_time <= 0:
        print("Error: --dock-time must be > 0", file=sys.stderr)
        sys.exit(1)
    if args.gate_time <= 0:
        print("Error: --gate-time must be > 0", file=sys.stderr)
        sys.exit(1)

    find_route(
        args.start, args.end,
        args.align, args.top_speed, args.warp_speed,
        args.dock_time, args.gate_time,
        args.sde
    )


if __name__ == '__main__':
    main()
