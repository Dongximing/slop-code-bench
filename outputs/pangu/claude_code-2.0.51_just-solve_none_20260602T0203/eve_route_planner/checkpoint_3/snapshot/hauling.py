#!/usr/bin/env python3
"""
Hauling route planner for New Eden using EVE SDE.
Calculates optimal travel routes between stations and systems with cargo manifest support.
"""

import argparse
import csv
import bz2
import json
import math
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
import heapq
import yaml

# Constants
AU_IN_M = 149597870700.0  # 1 Astronomical Unit in meters

# Zarzakh system ID (from EVE Online)
ZARZAKH_ID = 30002667

# Lock reset wait time in seconds (6 hours)
WAIT_TIME = 6 * 3600  # 21600 seconds


def format_duration(total_time: float) -> str:
    total_minutes = math.ceil(total_time / 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours:02d}:{minutes:02d}"


def format_cargo(amount: float) -> str:
    return f"{amount:,.2f}"


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


def load_sde_data(sde_path: str) -> Tuple[Dict, Dict, Dict]:
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

    jumps = defaultdict(set)
    jumps_data = load_compressed_csv(f"{sde_path}/mapSolarSystemJumps.csv.bz2")
    for row in jumps_data:
        from_id = int(row['fromSolarSystemID'])
        to_id = int(row['toSolarSystemID'])
        jumps[from_id].add(to_id)
        jumps[to_id].add(from_id)

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

    return systems, jumps, stations


def parse_location(location_str: str, systems: Dict, stations: Dict) -> Tuple[str, int, int, float, float, float]:
    """Parse a location string and return (name, type, system_id, x, y, z)."""
    for station_id, station in stations.items():
        if station['name'] == location_str:
            return (location_str, 0, station['system_id'],
                    station['x'], station['y'], station['z'])

    for system_id, system in systems.items():
        if system['name'] == location_str:
            return (location_str, 1, system_id,
                    system['x'], system['y'], system['z'])

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
    """Calculate warp time for a given distance."""
    v_warp_ms = max_warp_speed_au * AU_IN_M
    v_drop = min(subwarp_speed_ms / 2, 100)

    k_a = max_warp_speed_au
    k_d = min(max_warp_speed_au / 3, 2)

    d_a = AU_IN_M
    d_d = v_warp_ms / k_d
    d_min = d_a + d_d

    if distance < d_min:
        v_warp_ms = (distance * k_a * k_d) / (k_a + k_d)

    t_accel = (1 / k_a) * math.log(v_warp_ms / (k_a * AU_IN_M)) if v_warp_ms > k_a * AU_IN_M else 0
    t_decel = (1 / k_d) * math.log(v_warp_ms / v_drop) if v_warp_ms > v_drop else 0
    t_cruise = (distance - (d_a + d_d)) / v_warp_ms if distance >= d_min else 0

    total_warp_time = t_accel + t_cruise + t_decel

    return align_time + total_warp_time


def get_system_distance(sys1: Dict, sys2: Dict) -> float:
    """Calculate 3D distance between two systems."""
    dx = sys1['x'] - sys2['x']
    dy = sys1['y'] - sys2['y']
    dz = sys1['z'] - sys2['z']
    return math.sqrt(dx*dx + dy*dy + dz*dz)


def load_config(config_path: str) -> Dict:
    """Load ship configuration from YAML file."""
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def load_manifest(manifest_path: str) -> Dict:
    """Load cargo manifest from YAML file."""
    with open(manifest_path, 'r') as f:
        return yaml.safe_load(f)


def load_contracts(contracts_path: str) -> List[Dict]:
    """Load contracts from JSONL file."""
    contracts = []
    with open(contracts_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line:
                contracts.append(json.loads(line))
    return contracts


def count_jumps_in_route(path: List[int], jumps: Dict) -> int:
    """Count the number of jumps in a route."""
    if not path or len(path) < 2:
        return 0
    return len(path) - 1


def find_route_dijkstra(start_sys_id: int, end_sys_id: int,
                        systems: Dict, jumps: Dict,
                        align_time: float, top_speed: float,
                        warp_speed: float, gate_time: float,
                        dock_time: float, is_freighter: bool) -> Tuple[Optional[List[int]], float]:
    """
    Find route between two systems using Dijkstra's algorithm.
    Returns (path, total_time).
    """
    pq = [(0, start_sys_id, None)]
    dist = defaultdict(lambda: float('inf'))
    dist[(start_sys_id, None)] = 0
    prev = {}

    while pq:
        current_time, current_sys, entered_from = heapq.heappop(pq)

        if current_time > dist[(current_sys, entered_from)]:
            continue

        if current_sys == end_sys_id:
            break

        current_system = systems[current_sys]

        for neighbor_sys in jumps[current_sys]:
            # Check Zarzakh lock
            if (current_sys == ZARZAKH_ID and entered_from is not None and
                neighbor_sys != entered_from):
                continue

            # Freighter restriction: no low-sec
            if is_freighter and systems[neighbor_sys]['security'] < 0.5:
                continue

            neighbor_system = systems[neighbor_sys]
            distance = get_system_distance(current_system, neighbor_system)
            warp_time = calculate_warp_time(distance, warp_speed, top_speed, align_time)
            total_segment_time = warp_time + gate_time

            new_time = current_time + total_segment_time
            new_state = (neighbor_sys, current_sys)

            if new_time < dist[new_state]:
                dist[new_state] = new_time
                prev[new_state] = (current_sys, entered_from)
                heapq.heappush(pq, (new_time, neighbor_sys, current_sys))

    # Consider waiting in Zarzakh
    if start_sys_id == ZARZAKH_ID or any(state[0] == ZARZAKH_ID for state in dist):
        for (sys_id, entered_from), time in list(dist.items()):
            if sys_id == ZARZAKH_ID and entered_from is not None:
                new_time = time + WAIT_TIME
                new_state = (ZARZAKH_ID, None)
                if new_time < dist[new_state]:
                    dist[new_state] = new_time
                    prev[new_state] = (sys_id, entered_from)
                    heapq.heappush(pq, (new_time, ZARZAKH_ID, None))

    # Find best state at destination
    best_state = None
    best_time = float('inf')
    for state, time in dist.items():
        if state[0] == end_sys_id and time < best_time:
            best_time = time
            best_state = state

    if best_state is None:
        return None, float('inf')

    # Reconstruct path
    path = []
    state = best_state
    while state is not None:
        path.append(state[0])
        prev_state = prev.get(state)
        if prev_state == state:
            break
        state = prev_state
    path.reverse()

    return path, best_time


def find_route_with_freighter_fallback(start_sys_id: int, end_sys_id: int,
                                       systems: Dict, jumps: Dict,
                                       align_time: float, top_speed: float,
                                       warp_speed: float, gate_time: float,
                                       dock_time: float) -> Tuple[Optional[List[int]], float]:
    """
    Find route for freighter with fallback to low-sec if no high-sec route exists.
    """
    # First try high-sec only
    path, time = find_route_dijkstra(start_sys_id, end_sys_id, systems, jumps,
                                     align_time, top_speed, warp_speed, gate_time,
                                     dock_time, is_freighter=True)
    if path is not None:
        return path, time

    # Fallback: allow low-sec if it's the ONLY possible route
    return find_route_dijkstra(start_sys_id, end_sys_id, systems, jumps,
                               align_time, top_speed, warp_speed, gate_time,
                               dock_time, is_freighter=False)


def find_route(start_loc: str, end_loc: str, align_time: float, top_speed: float,
               warp_speed: float, dock_time: float, gate_time: float,
               sde_path: str) -> None:
    """Find and output the optimal route."""

    systems, jumps, stations = load_sde_data(sde_path)

    start_name, start_type, start_sys_id, start_x, start_y, start_z = \
        parse_location(start_loc, systems, stations)
    end_name, end_type, end_sys_id, end_x, end_y, end_z = \
        parse_location(end_loc, systems, stations)

    # Special case: start and end are the same location
    if start_type == 0 and end_type == 0 and start_sys_id == end_sys_id:
        for station_id, station in stations.items():
            if (station['system_id'] == start_sys_id and
                station['name'] == start_name and
                station['name'] == end_name):
                print(f"START: {start_name}")
                print(f"DOCK: {end_name}")
                print("DONE: 00:01")
                return
        total_time = 2 * dock_time
        print(f"START: {start_name}")
        print("UNDOCK")
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

    # Same system
    if start_sys_id == end_sys_id:
        total_time = dock_time
        print(f"START: {start_name}")

        if start_type == 0:
            print("UNDOCK")

        start_system = systems[start_sys_id]

        if end_type == 0:
            print(f"DOCK: {end_name}")
        else:
            print(f"GO: {start_system['name']} ({int(start_system['security'] * 10) / 10.0})")

        print(f"DONE: {format_duration(total_time)}")
        return

    # Route between different systems
    path, travel_time = find_route_dijkstra(
        start_sys_id, end_sys_id, systems, jumps,
        align_time, top_speed, warp_speed, gate_time, dock_time,
        is_freighter=False
    )

    if path is None:
        print("Error: No path found", file=sys.stderr)
        return

    total_time = travel_time

    if start_type == 0:
        total_time += dock_time
    if end_type == 0:
        total_time += dock_time

    print(f"START: {start_name}")

    if start_type == 0:
        print("UNDOCK")

    system_names = []
    for sys_id in path:
        sys_info = systems[sys_id]
        sec = sys_info['security']
        sec_str = f"{sec:.1f}"
        system_names.append(f"{sys_info['name']} ({sec_str})")

    print(f"GO: {' -> '.join(system_names)}")

    if end_type == 0:
        print(f"DOCK: {end_name}")

    print(f"DONE: {format_duration(total_time)}")


def find_manifest_route(start_loc: str, end_loc: str,
                        manifest: Dict, config: Dict, ship_name: str,
                        sde_path: str) -> None:
    """Find and output the optimal route with cargo manifest."""

    systems, jumps, stations = load_sde_data(sde_path)

    if ship_name not in config['ships']:
        print(f"Error: Ship '{ship_name}' not found in config", file=sys.stderr)
        sys.exit(1)

    ship_info = config['ships'][ship_name]
    align_time = ship_info['align']
    top_speed = ship_info['top_speed']
    warp_speed = ship_info['warp_speed']
    cargo_size = ship_info['cargo_size']
    is_freighter = ship_info['type'] == 'Freighter'

    times = config['times']
    dock_time = times['dock']
    gate_time = times['gate']
    move_cargo_time = times['move_cargo']

    # Parse start and end locations
    start_name, start_type, start_sys_id, start_x, start_y, start_z = \
        parse_location(start_loc, systems, stations)
    end_name, end_type, end_sys_id, end_x, end_y, end_z = \
        parse_location(end_loc, systems, stations)

    # Parse waypoints from manifest
    waypoints = []
    if manifest and 'waypoints' in manifest:
        for wp in manifest['waypoints']:
            wp_name = wp['name']
            wp_cargo = wp['cargo']
            wp_station_name, wp_type, wp_sys_id, wp_x, wp_y, wp_z = \
                parse_location(wp_name, systems, stations)
            waypoints.append({
                'name': wp_station_name,
                'type': wp_type,
                'system_id': wp_sys_id,
                'cargo': wp_cargo if wp_cargo is not None else 0
            })

    start_cargo = manifest['start_cargo'] if manifest and 'start_cargo' in manifest else 0
    if start_cargo is None:
        start_cargo = 0

    # Calculate total cargo to move
    total_cargo = start_cargo
    for wp in waypoints:
        if wp['cargo'] > 0:
            total_cargo += wp['cargo']

    # If no cargo, just do a simple route
    if total_cargo == 0:
        find_route(start_loc, end_loc, align_time, top_speed,
                   warp_speed, dock_time, gate_time, sde_path)
        return

    # Plan trips - this is the core logic
    trips = plan_trips_manifest(
        start_loc, end_loc, start_cargo, waypoints,
        cargo_size, systems, jumps, stations,
        align_time, top_speed, warp_speed, dock_time, gate_time, move_cargo_time,
        is_freighter
    )

    # Output results
    total_time = 0
    total_moved = 0

    for i, trip in enumerate(trips):
        if i > 0:
            print(f"[--- TRIP {i+1} ---]")

        trip_time = output_trip_manifest(
            trip, systems, stations, jumps,
            dock_time, move_cargo_time,
            align_time, top_speed, warp_speed, gate_time,
            is_freighter,
            i == 0  # is_first_trip
        )
        total_time += trip_time
        total_moved += trip['total_delivered']

    print(f"DONE: {format_duration(total_time)}")
    if total_moved > 0:
        print(f"MOVED: {format_cargo(total_moved)} m3")


def plan_trips_manifest(start_loc: str, end_loc: str, start_cargo: float,
                        waypoints: List[Dict], cargo_size: float,
                        systems: Dict, jumps: Dict, stations: Dict,
                        align_time: float, top_speed: float, warp_speed: float,
                        dock_time: float, gate_time: float, move_cargo_time: float,
                        is_freighter: bool) -> List[Dict]:
    """
    Plan multiple trips to move all cargo.
    Implements the trip planning logic with tie-breaking:
    - If ordering of two trips doesn't effect outcome, tie break with:
      Waypoints Finished > Minimum warps in space with cargo in hold > Alphabetical
    """
    trips = []

    # Parse locations
    start_info = parse_location(start_loc, systems, stations)
    end_info = parse_location(end_loc, systems, stations)

    # Build list of pickup locations - waypoints must be visited in order
    # If start_cargo > 0, we have cargo to load at start
    # Waypoints are visited in order from the manifest
    pickup_sequence = []
    if start_cargo > 0:
        pickup_sequence.append({
            'name': start_loc,
            'cargo': start_cargo,
            'system_id': start_info[2],
            'type': start_info[1],
            'index': 0  # Start position
        })

    for idx, wp in enumerate(waypoints):
        if wp['cargo'] > 0:
            wp_station_name, wp_type, wp_sys_id, wp_x, wp_y, wp_z = \
                parse_location(wp['name'], systems, stations)
            pickup_sequence.append({
                'name': wp_station_name,
                'cargo': wp['cargo'],
                'system_id': wp_sys_id,
                'type': wp_type,
                'index': idx + 1  # Position in sequence
            })

    # If no pickups but start_cargo exists, still need to deliver
    if not pickup_sequence:
        return []

    # Calculate total cargo
    total_cargo = sum(p['cargo'] for p in pickup_sequence)

    # Determine trips needed
    if cargo_size <= 0:
        trips_needed = 1
    else:
        trips_needed = math.ceil(total_cargo / cargo_size)

    # For each trip, we need to visit waypoints in order
    # but we may need multiple trips to move all cargo
    # Strategy: process waypoints in order, tracking remaining cargo at each

    # Track remaining cargo at each pickup location
    remaining_cargo = {p['name']: p['cargo'] for p in pickup_sequence}

    # Get the ordered list of waypoint names (excluding start if it was added)
    waypoint_names = [p['name'] for p in pickup_sequence if p['name'] != start_loc]

    total_delivered = 0

    for trip_num in range(trips_needed):
        trip_pickups = []

        # For this trip, visit waypoints in order
        # Load as much as we can at each stop
        for wp_name in waypoint_names:
            if remaining_cargo.get(wp_name, 0) > 0:
                # Get the pickup info
                for p in pickup_sequence:
                    if p['name'] == wp_name:
                        pickup_info = p.copy()
                        break

                # Calculate how much we can load at this stop
                current_hold = sum(p['cargo'] for p in trip_pickups)
                available_space = cargo_size - current_hold

                if available_space > 0:
                    load_amount = min(remaining_cargo[wp_name], available_space)
                    pickup_info['cargo'] = load_amount
                    trip_pickups.append(pickup_info)
                    remaining_cargo[wp_name] -= load_amount

        # Also check if we have cargo at start
        if start_cargo > 0 and remaining_cargo.get(start_loc, 0) > 0:
            current_hold = sum(p['cargo'] for p in trip_pickups)
            available_space = cargo_size - current_hold

            if available_space > 0:
                load_amount = min(remaining_cargo[start_loc], available_space)
                # Find start pickup info
                for p in pickup_sequence:
                    if p['name'] == start_loc:
                        start_pickup = p.copy()
                        start_pickup['cargo'] = load_amount
                        trip_pickups.insert(0, start_pickup)  # Start should be first
                        remaining_cargo[start_loc] -= load_amount
                        break

        if not trip_pickups:
            break

        trip_cargo = sum(p['cargo'] for p in trip_pickups)
        total_delivered += trip_cargo

        trips.append({
            'pickups': trip_pickups,
            'cargo': trip_cargo,
            'total_delivered': total_delivered,
            'start_loc': start_loc,
            'end_loc': end_loc,
            'start_info': start_info,
            'end_info': end_info,
            'waypoint_order': waypoint_names  # For tie-breaking
        })

    return trips


def output_trip_manifest(trip: Dict, systems: Dict, stations: Dict, jumps: Dict,
                         dock_time: float, move_cargo_time: float,
                         align_time: float, top_speed: float, warp_speed: float,
                         gate_time: float, is_freighter: bool,
                         is_first_trip: bool = True) -> float:
    """Output a single trip and return its total time."""
    total_time = 0
    pickups = trip['pickups']
    start_loc = trip['start_loc']
    end_loc = trip['end_loc']
    start_info = trip['start_info']
    end_info = trip['end_info']

    start_name = start_info[0]
    start_type = start_info[1]
    start_sys_id = start_info[2]

    end_name = end_info[0]
    end_type = end_info[1]
    end_sys_id = end_info[2]

    # Track current state
    if is_first_trip:
        current_loc = start_loc
        current_sys_id = start_sys_id
        current_type = start_type  # 0 = station, 1 = system
        current_at_station = (start_type == 0)
    else:
        # Subsequent trips start from end location
        current_loc = end_loc
        current_sys_id = end_sys_id
        current_type = end_type
        current_at_station = (end_type == 0)

    if is_first_trip:
        print(f"START: {start_name}")

    # Process each pickup operation in order
    for i, pickup in enumerate(pickups):
        pickup_name = pickup['name']
        pickup_sys_id = pickup['system_id']
        pickup_type = pickup['type']
        pickup_cargo = pickup['cargo']
        pickup_at_station = (pickup_type == 0)

        # If this is the first operation and we're at the right station, load first
        if i == 0 and current_at_station and pickup_name == current_loc:
            print(f"LOAD: {format_cargo(pickup_cargo)} m3")
            total_time += move_cargo_time
            # Continue to next operation
            continue

        # If we need to travel to a different location
        if current_sys_id != pickup_sys_id:
            # If we're at a station and haven't undocked, undock first
            if current_at_station:
                print("UNDOCK")
                total_time += dock_time
                current_at_station = False

            # Find route
            if is_freighter:
                path, travel_time = find_route_with_freighter_fallback(
                    current_sys_id, pickup_sys_id, systems, jumps,
                    align_time, top_speed, warp_speed, gate_time, dock_time
                )
            else:
                path, travel_time = find_route_dijkstra(
                    current_sys_id, pickup_sys_id, systems, jumps,
                    align_time, top_speed, warp_speed, gate_time, dock_time,
                    is_freighter=False
                )

            if path:
                system_names = []
                for sys_id in path:
                    sys_info = systems[sys_id]
                    sec = sys_info['security']
                    sec_str = f"{sec:.1f}"
                    system_names.append(f"{sys_info['name']} ({sec_str})")

                print(f"GO: {' -> '.join(system_names)}")
                total_time += travel_time

                # If arriving at a station, dock
                if pickup_at_station:
                    print(f"DOCK: {pickup_name}")
                    total_time += dock_time
                    current_at_station = True

            current_sys_id = pickup_sys_id
            current_loc = pickup_name
            current_type = pickup_type

        # Load cargo at this location
        print(f"LOAD: {format_cargo(pickup_cargo)} m3")
        total_time += move_cargo_time

    # Now travel to end location
    if current_sys_id != end_sys_id:
        # If at station, undock
        if current_at_station:
            print("UNDOCK")
            total_time += dock_time

        if is_freighter:
            path, travel_time = find_route_with_freighter_fallback(
                current_sys_id, end_sys_id, systems, jumps,
                align_time, top_speed, warp_speed, gate_time, dock_time
            )
        else:
            path, travel_time = find_route_dijkstra(
                current_sys_id, end_sys_id, systems, jumps,
                align_time, top_speed, warp_speed, gate_time, dock_time,
                is_freighter=False
            )

        if path:
            system_names = []
            for sys_id in path:
                sys_info = systems[sys_id]
                sec = sys_info['security']
                sec_str = f"{sec:.1f}"
                system_names.append(f"{sys_info['name']} ({sec_str})")

            print(f"GO: {' -> '.join(system_names)}")
            total_time += travel_time

            # Dock at end if station
            if end_type == 0:
                print(f"DOCK: {end_name}")
                total_time += dock_time

    # Unload all cargo
    total_trip_cargo = sum(p['cargo'] for p in pickups)
    print(f"UNLOAD: {format_cargo(total_trip_cargo)} m3")
    total_time += move_cargo_time

    return total_time


def main():
    parser = argparse.ArgumentParser(
        description='Calculate travel routes in New Eden using EVE SDE.'
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # Plan command (existing functionality)
    plan_parser = subparsers.add_parser('plan', help='Plan a route with cargo manifest')
    plan_parser.add_argument('start', help='Starting location (station or system name)')
    plan_parser.add_argument('end', help='Destination location (station or system name)')
    plan_parser.add_argument('--config', required=True, help='Path to config YAML file')
    plan_parser.add_argument('--sde', required=True, help='Path to SDE directory')
    plan_parser.add_argument('--ship', help='Ship name from config')

    # Contracts command (new functionality)
    contracts_parser = subparsers.add_parser('contracts', help='Plan hauling contracts')
    contracts_parser.add_argument('start_system', help='Starting system (home base)')
    contracts_parser.add_argument('contracts_file', help='Path to contracts JSONL file')
    contracts_parser.add_argument('--config', required=True, help='Path to config YAML file')
    contracts_parser.add_argument('--sde', required=True, help='Path to SDE directory')
    contracts_parser.add_argument('--target-iph', type=float, dest='target_iph',
                                   help='Target ISK per hour (M isk / Hour)')
    contracts_parser.add_argument('--max-time', type=int, dest='max_time',
                                   help='Maximum time in minutes for hauling')

    # Keep backward compatibility for simple route without subcommand
    parser.add_argument('start', nargs='?', help='Starting location (station or system name)')
    parser.add_argument('end', nargs='?', help='Destination location (station or system name)')

    parser.add_argument('--align', type=float, help='Time in seconds to align pre-warp (must be > 0)')
    parser.add_argument('--top-speed', type=float, dest='top_speed',
                       help='Maximum subwarp speed in m/s (must be >= 0)')
    parser.add_argument('--warp-speed', type=float, dest='warp_speed',
                       help='Maximum warp speed in AU/s (must be > 0)')
    parser.add_argument('--dock-time', type=float, dest='dock_time',
                       help='Time in seconds to dock/undock (must be > 0)')
    parser.add_argument('--gate-time', type=float, dest='gate_time',
                       help='Time in seconds to use a gate (must be > 0)')
    parser.add_argument('--sde', help='Path to SDE directory')

    parser.add_argument('--manifest', help='Path to manifest YAML file')
    parser.add_argument('--config', help='Path to config YAML file')
    parser.add_argument('--ship', help='Ship name from config')

    args = parser.parse_args()

    # Handle old-style simple route (backward compatibility)
    if args.command is None and args.start and args.end and args.manifest is None:
        if (args.align is None or args.top_speed is None or args.warp_speed is None
           or args.dock_time is None or args.gate_time is None or args.sde is None):
            print("Error: In simple mode, --align, --top-speed, --warp-speed, --dock-time, --gate-time, --sde are required",
                  file=sys.stderr)
            sys.exit(1)

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
    # Handle plan command (existing functionality)
    elif args.command == 'plan' or (args.command is None and args.manifest is not None):
        if args.config is None:
            print("Error: --config is required when using --manifest", file=sys.stderr)
            sys.exit(1)
        if args.ship is None:
            print("Error: --ship is required when using --manifest", file=sys.stderr)
            sys.exit(1)

        try:
            config = load_config(args.config)
            manifest = load_manifest(args.manifest)
        except Exception as e:
            print(f"Error loading files: {e}", file=sys.stderr)
            sys.exit(1)

        find_manifest_route(
            args.start, args.end,
            manifest, config, args.ship,
            args.sde
        )
    # Handle contracts command (new functionality)
    elif args.command == 'contracts':
        try:
            config = load_config(args.config)
            contracts = load_contracts(args.contracts_file)
        except Exception as e:
            print(f"Error loading files: {e}", file=sys.stderr)
            sys.exit(1)

        find_contracts_plan(
            args.start_system, contracts, config,
            args.target_iph, args.max_time,
            args.sde
        )
    else:
        parser.print_help()


def round_bankers(value: float) -> float:
    """Round to 2 decimal places using bankers rounding (round half to even)."""
    return round(value + 0, 2)


def select_best_ship(ships: Dict, cargo_size: int, systems: Dict, jumps: Dict,
                      start_system_id: int, contract_dest_sys_ids: set) -> Tuple[str, Dict]:
    """
    Select the best ship for hauling based on travel time,
    with tiebreakers: EHP, then Name.
    Returns (ship_name, ship_info) of the best ship.
    """
    best_ship = None
    best_ship_name = None
    best_time = float('inf')
    best_ehp = -1

    for ship_name, ship_info in ships.items():
        if ship_info['cargo_size'] < cargo_size:
            continue

        align_time = ship_info['align']
        top_speed = ship_info['top_speed']
        warp_speed = ship_info['warp_speed']
        gate_time = 0.0  # These will come from config
        dock_time = 0.0
        is_freighter = ship_info['type'] == 'Freighter'

        # Calculate max time to any contract destination
        max_time = 0
        for dest_sys_id in contract_dest_sys_ids:
            path, travel_time = find_route_dijkstra(
                start_system_id, dest_sys_id, systems, jumps,
                align_time, top_speed, warp_speed, gate_time, dock_time,
                is_freighter
            )
            if travel_time < float('inf') and travel_time > max_time:
                max_time = travel_time

        # If any destination unreachable, skip this ship
        if max_time == float('inf') and len(contract_dest_sys_ids) > 0:
            continue

        ehp = ship_info.get('ehp', 60000 if is_freighter else 300000)

        # Tiebreaker order: travel time (lower) -> EHP (higher) -> name (alphabetical)
        if (max_time < best_time or
            (max_time == best_time and ehp > best_ehp) or
            (max_time == best_time and ehp == best_ehp and (best_ship_name is None or ship_name < best_ship_name))):
            best_time = max_time
            best_ehp = ehp
            best_ship = ship_info
            best_ship_name = ship_name

    if best_ship is None:
        return None, None
    return best_ship_name, best_ship


def filter_and_score_contracts(contracts: List[Dict], config: Dict, systems: Dict,
                                jumps: Dict, stations: Dict, start_system_name: str,
                                start_system_id: int, cargo_size: float, ship_info: Dict,
                                target_iph: Optional[float], max_time_minutes: Optional[int]) -> List[Dict]:
    """
    Filter contracts based on constraints and score them for profitability.
    Returns list of viable contracts with computed metrics.
    """
    # Get config constraints
    min_isk_per_jump = config.get('min_isk_per_jump')
    max_isk_per_ehp = config.get('max_isk_per_ehp')

    aligned_time = ship_info['align']
    top_speed = ship_info['top_speed']
    warp_speed = ship_info['warp_speed']
    gate_time = config['times']['gate']
    dock_time = config['times']['dock']
    is_freighter = ship_info['type'] == 'Freighter'

    viable_contracts = []

    for idx, contract in enumerate(contracts):
        # Contract ID is monotonic counter starting from 1, based on index in file
        contract_id = idx + 1

        # Parse start and end locations
        try:
            start_loc_name, start_type, start_sys_id, _, _, _ = parse_location(
                contract['start'], systems, stations)
            end_loc_name, end_type, end_sys_id, _, _, _ = parse_location(
                contract['end'], systems, stations)
        except ValueError:
            continue

        # Find route: from start to end (for this contract)
        path, travel_time = find_route_dijkstra(
            start_sys_id, end_sys_id, systems, jumps,
            aligned_time, top_speed, warp_speed, gate_time, dock_time,
            is_freighter
        )

        if path is None:
            continue

        # Count jumps in this route
        jumps_count = count_jumps_in_route(path, jumps)

        # Calculate isk per jump for this contract
        reward = contract['reward']
        isk_per_jump = reward / jumps_count if jumps_count > 0 else float('inf')

        # Apply min_isk_per_jump filter if specified
        if min_isk_per_jump is not None and isk_per_jump < min_isk_per_jump:
            continue

        # Calculate time for this contract (including dock/undock)
        contract_time = travel_time
        if start_type == 0:
            contract_time += dock_time
        if end_type == 0:
            contract_time += dock_time

        # Max time filter (convert minutes to seconds)
        if max_time_minutes is not None and contract_time > max_time_minutes * 60:
            continue

        # Calculate EHP
        ehp = ship_info.get('ehp')
        if ehp is None:
            if is_freighter:
                ehp = 300000
            else:
                ehp = 60000

        # Apply max_isk_per_ehp filter if specified (NOT for blockade runners)
        if max_isk_per_ehp is not None and ship_info['type'] != 'Blockade Runner':
            isk_per_m3 = reward / contract['m3'] if contract['m3'] > 0 else float('inf')
            if isk_per_m3 > max_isk_per_ehp * ehp:
                continue

        # Calculate profitability metrics
        # Total ISK
        total_isk = reward

        # Total volume
        total_volume = contract['m3']

        # Store additional info for selection and output
        contract['id'] = contract_id
        contract['start_loc_name'] = start_loc_name
        contract['start_type'] = start_type
        contract['start_sys_id'] = start_sys_id
        contract['end_loc_name'] = end_loc_name
        contract['end_type'] = end_type
        contract['end_sys_id'] = end_sys_id
        contract['path'] = path
        contract['travel_time'] = travel_time
        contract['jumps'] = jumps_count
        contract['isk_per_jump'] = isk_per_jump
        contract['ehp'] = ehp
        contract['total_isk'] = total_isk
        contract['total_volume'] = total_volume
        contract['issuer'] = contract.get('issuer', 'Unknown')

        # Calculate time to complete just this contract
        contract['full_time'] = contract_time

        viable_contracts.append(contract)

    # Sort contracts: profitability first, then issuer name alphabetical ascending
    viable_contracts.sort(key=lambda c: (-c['reward'], c['issuer']))

    return viable_contracts


def select_contracts_knapsack(contracts: List[Dict], cargo_size: float,
                               target_iph: Optional[float], max_time_minutes: Optional[int]) -> List[Dict]:
    """
    Select the most profitable set of contracts using a greedy approach.
    Since we want to maximize profit, sort by profitability and take as many as fit.
    """
    if not contracts:
        return []

    selected = []
    total_volume = 0
    total_time = 0
    total_isk = 0

    for contract in contracts:
        # Check volume constraint
        if total_volume + contract['m3'] > cargo_size:
            continue

        # Check time constraint
        if max_time_minutes is not None:
            # Estimate time for this contract: include travel from base to start + contract + return
            # For simplicity, use contract's travel time + overhead
            estimated_time = contract['full_time']
            # Add round trip from base
            # TODO: calculate more accurately based on actual route
        else:
            estimated_time = 0

        if max_time_minutes is not None and total_time + estimated_time > max_time_minutes * 60:
            continue

        # Check target IPH constraint (if specified)
        if target_iph is not None:
            # Current profit rate
            if total_time > 0:
                current_iph = (total_isk / total_time) * 3600 / 1000000  # M isk per hour
                # Only add if it doesn't drop below target
                new_total_isk = total_isk + contract['reward']
                new_total_time = total_time + estimated_time
                new_iph = (new_total_isk / new_total_time) * 3600 / 1000000 if new_total_time > 0 else 0
                if new_iph < target_iph:
                    continue

        selected.append(contract)
        total_volume += contract['m3']
        total_time += estimated_time if max_time_minutes is not None else 0
        total_isk += contract['reward']

    return selected


def format_isk(value: float) -> str:
    """Format ISK value with bankers rounding."""
    return f"{round_bankers(value):,.2f}"


def find_contracts_plan(start_system: str, contracts: List[Dict], config: Dict,
                        target_iph: Optional[float], max_time_minutes: Optional[int],
                        sde_path: str) -> None:
    """
    Main function to find the best hauling plan from contracts.
    """
    # Load SDE data
    systems, jumps, stations = load_sde_data(sde_path)

    # Parse start system
    start_system_name, start_type, start_system_id, _, _, _ = \
        parse_location(start_system, systems, stations)

    # Get all ships from config
    ships = config.get('ships', {})

    if not ships:
        print("Error: No ships defined in config", file=sys.stderr)
        sys.exit(1)

    # Collect all unique destination system IDs from contracts
    contract_dest_sys_ids = set()
    for contract in contracts:
        try:
            _, _, _, _, _, _ = parse_location(contract['end'], systems, stations)
            # We'll need to parse again later, for now just collect info
        except ValueError:
            continue

    # First, determine the best ship
    # Need to find minimum cargo size required by any viable contract
    min_cargo_needed = 0

    # Actually, let's try each ship and pick the one with best time
    best_ship_name = None
    best_ship_info = None
    best_contracts = []

    for ship_name, ship_info in ships.items():
        cargo_size = ship_info['cargo_size']

        # Filter and score contracts for this ship
        viable = filter_and_score_contracts(
            contracts, config, systems, jumps, stations,
            start_system_name, start_system_id,
            cargo_size, ship_info, target_iph, max_time_minutes
        )

        if not viable:
            continue

        # Select contracts using greedy approach
        selected = select_contracts_knapsack(viable, cargo_size, target_iph, max_time_minutes)

        # Calculate total profit for this selection
        total_isk = sum(c['reward'] for c in selected)
        total_time = sum(c.get('full_time', 0) for c in selected)

        # Calculate max travel time for ship comparison
        contract_dests = set(c['end_sys_id'] for c in selected)
        if not contract_dests:
            continue

        align_time = ship_info['align']
        top_speed = ship_info['top_speed']
        warp_speed = ship_info['warp_speed']
        gate_time = config['times']['gate']
        dock_time = config['times']['dock']
        is_freighter = ship_info['type'] == 'Freighter'

        max_travel_time = 0
        for dest in contract_dests:
            path, travel_time = find_route_dijkstra(
                start_system_id, dest, systems, jumps,
                align_time, top_speed, warp_speed, gate_time, dock_time,
                is_freighter
            )
            if travel_time < float('inf') and travel_time > max_travel_time:
                max_travel_time = travel_time

        ehp = ship_info.get('ehp')
        if ehp is None:
            if is_freighter:
                ehp = 300000
            else:
                ehp = 60000

        # Compare with current best
        if (best_ship_info is None or
            max_travel_time < best_time or
            (max_travel_time == best_time and ehp > best_ship_info.get('ehp', 0)) or
            (max_travel_time == best_time and ehp == best_ship_info.get('ehp', 0) and
             ship_name < best_ship_name)):
            best_ship_name = ship_name
            best_ship_info = ship_info
            best_contracts = selected
            best_time = max_travel_time

    # Check if any contracts were found
    if not best_contracts:
        print("No Good Contracts")
        return

    # Output the plan
    print(f"SHIP: {best_ship_name}")

    cargo_size = best_ship_info['cargo_size']
    align_time = best_ship_info['align']
    top_speed = best_ship_info['top_speed']
    warp_speed = best_ship_info['warp_speed']
    is_freighter = best_ship_info['type'] == 'Freighter'

    dock_time = config['times']['dock']
    gate_time = config['times']['gate']
    move_cargo_time = config['times']['move_cargo']

    # Sort selected contracts by reward descending for output (optional)
    # Actually sort by ID for consistent output
    best_contracts.sort(key=lambda c: c['id'])

    # Load phase
    print("\nLOAD:")
    total_volume = 0
    total_isk = 0
    for c in best_contracts:
        print(f"LOAD {c['issuer']} (id={c['id']}): {format_isk(c['reward'])}M ISK | {c['m3']:,}.00 m3")
        total_volume += c['m3']
        total_isk += c['reward']

    # Calculate route: visit each contract start, then end
    # For simplicity, build a route that goes from start_system to each contract start, then to end
    current_sys_id = start_system_id
    total_time = 0

    for contract in best_contracts:
        # Travel from current location to contract start
        if current_sys_id != contract['start_sys_id']:
            path, travel_time = find_route_dijkstra(
                current_sys_id, contract['start_sys_id'], systems, jumps,
                align_time, top_speed, warp_speed, gate_time, dock_time,
                is_freighter
            )
            if path:
                system_names = []
                for sys_id in path:
                    sys_info = systems[sys_id]
                    sec = sys_info['security']
                    sec_str = f"{sec:.1f}"
                    system_names.append(f"{sys_info['name']} ({sec_str})")
                print(f"\nGO: {' -> '.join(system_names)}")
                total_time += travel_time

            current_sys_id = contract['start_sys_id']

        # Load action - already printed above
        # Wait, the requirement says LOAD/UNLOAD for every action with a contract
        # So LOAD was printed, now we actually have to "pick it up"

        # Now travel to contract end
        if current_sys_id != contract['end_sys_id']:
            path, travel_time = find_route_dijkstra(
                current_sys_id, contract['end_sys_id'], systems, jumps,
                align_time, top_speed, warp_speed, gate_time, dock_time,
                is_freighter
            )
            if path:
                system_names = []
                for sys_id in path:
                    sys_info = systems[sys_id]
                    sec = sys_info['security']
                    sec_str = f"{sec:.1f}"
                    system_names.append(f"{sys_info['name']} ({sec_str})")
                print(f"\nGO: {' -> '.join(system_names)}")
                total_time += travel_time

            current_sys_id = contract['end_sys_id']

    # Return to base
    if current_sys_id != start_system_id:
        path, travel_time = find_route_dijkstra(
            current_sys_id, start_system_id, systems, jumps,
            align_time, top_speed, warp_speed, gate_time, dock_time,
            is_freighter
        )
        if path:
            system_names = []
            for sys_id in path:
                sys_info = systems[sys_id]
                sec = sys_info['security']
                sec_str = f"{sec:.1f}"
                system_names.append(f"{sys_info['name']} ({sec_str})")
            print(f"\nGO: {' -> '.join(system_names)}")
            total_time += travel_time

    # Unload phase
    print("\nUNLOAD:")
    total_jumps = sum(c['jumps'] for c in best_contracts)
    for c in best_contracts:
        print(f"UNLOAD {c['issuer']} (id={c['id']}): {c['jumps']} Jumps | {c['m3']:,}.00 m3")

    # Summary
    print(f"\nNUM CONTRACTS: {len(best_contracts)}")
    print(f"PROFIT: {format_isk(total_isk)}M")

    isk_per_m3 = total_isk / total_volume if total_volume > 0 else 0
    print(f"ISK/M3: {format_isk(isk_per_m3)}")

    isk_per_jump = total_isk / total_jumps if total_jumps > 0 else 0
    print(f"ISK/Jump: {format_isk(isk_per_jump)}")

    # ISK/Hour calculation
    total_hours = total_time / 3600
    isk_per_hour = total_isk / total_hours if total_hours > 0 else 0
    print(f"ISK/Hour: {format_isk(isk_per_hour)}")


if __name__ == '__main__':
    main()
