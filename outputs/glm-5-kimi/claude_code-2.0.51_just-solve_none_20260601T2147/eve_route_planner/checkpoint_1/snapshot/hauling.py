#!/usr/bin/env python3
"""
EVE Online Hauling Route Planner
Calculates optimal routes through New Eden considering warp times and Zarzakh gate locks.
"""

import argparse
import bz2
import csv
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Set
import heapq

# Constants
AU_IN_M = 149_597_870_700  # 1 AU in meters
ZARZAKH_SYSTEM_ID = 30100000
ZARZAKH_LOCK_TIME = 6 * 3600  # 6 hours in seconds


@dataclass
class System:
    """Represents a solar system."""
    system_id: int
    name: str
    security: float
    x: float
    y: float
    z: float


@dataclass
class Station:
    """Represents a station."""
    station_id: int
    name: str
    system_id: int
    x: float
    y: float
    z: float


@dataclass
class Stargate:
    """Represents a stargate."""
    item_id: int
    system_id: int
    name: str
    x: float
    y: float
    z: float
    destination_system_id: Optional[int] = None


@dataclass
class WarpParams:
    """Parameters for warp calculation."""
    align_time: float
    top_speed: float  # m/s
    warp_speed: float  # AU/s
    dock_time: float
    gate_time: float


def load_systems(sde_path: str) -> Dict[int, System]:
    """Load solar systems from SDE."""
    systems = {}
    filepath = os.path.join(sde_path, 'mapSolarSystems.csv.bz2')
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            system_id = int(row['solarSystemID'])
            systems[system_id] = System(
                system_id=system_id,
                name=row['solarSystemName'],
                security=float(row['security']),
                x=float(row['x']),
                y=float(row['y']),
                z=float(row['z'])
            )
    return systems


def load_jumps(sde_path: str) -> Dict[int, Set[int]]:
    """Load system jumps from SDE. Returns bidirectional adjacency list."""
    jumps = {}
    filepath = os.path.join(sde_path, 'mapSolarSystemJumps.csv.bz2')
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            from_id = int(row['fromSolarSystemID'])
            to_id = int(row['toSolarSystemID'])
            if from_id not in jumps:
                jumps[from_id] = set()
            if to_id not in jumps:
                jumps[to_id] = set()
            jumps[from_id].add(to_id)
            jumps[to_id].add(from_id)
    return jumps


def load_stations(sde_path: str) -> Dict[int, Station]:
    """Load stations from SDE."""
    stations = {}
    filepath = os.path.join(sde_path, 'staStations.csv.bz2')
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            station_id = int(row['stationID'])
            stations[station_id] = Station(
                station_id=station_id,
                name=row['stationName'],
                system_id=int(row['solarSystemID']),
                x=float(row['x']),
                y=float(row['y']),
                z=float(row['z'])
            )
    return stations


def load_stargates(sde_path: str) -> Dict[int, List[Stargate]]:
    """Load stargates from SDE. Returns dict mapping system_id -> list of stargates."""
    stargates = {}
    filepath = os.path.join(sde_path, 'mapDenormalize.csv.bz2')
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            group_id = int(row['groupID'])
            if group_id != 10:  # 10 is stargate group
                continue
            system_id = int(row['solarSystemID'])
            if system_id not in stargates:
                stargates[system_id] = []
            stargates[system_id].append(Stargate(
                item_id=int(row['itemID']),
                system_id=system_id,
                name=row['itemName'],
                x=float(row['x']),
                y=float(row['y']),
                z=float(row['z'])
            ))
    return stargates


def calculate_distance(x1: float, y1: float, z1: float, x2: float, y2: float, z2: float) -> float:
    """Calculate Euclidean distance between two points in meters."""
    return math.sqrt((x2 - x1)**2 + (y2 - y1)**2 + (z2 - z1)**2)


def calculate_warp_time(distance_m: float, warp_params: WarpParams) -> float:
    """
    Calculate warp time based on the warp model.

    Args:
        distance_m: Distance in meters
        warp_params: Warp parameters

    Returns:
        Time in seconds
    """
    # Align time (ceiling in seconds due to server ticks)
    align_time = math.ceil(warp_params.align_time)

    # Warp speeds
    v_warp_au = warp_params.warp_speed  # AU/s
    v_subwarp = warp_params.top_speed  # m/s

    # Dropout speed
    v_drop = min(v_subwarp / 2, 100)  # m/s

    # Acceleration and deceleration rates
    k_a = v_warp_au  # AU/s
    k_d = min(v_warp_au / 3, 2)  # AU/s, capped at 2

    # Max warp speed in m/s
    v_warp_ms = v_warp_au * AU_IN_M

    # Acceleration distance: 1 AU
    d_a = AU_IN_M

    # Deceleration distance
    d_d = v_warp_ms / k_d

    # Minimum warp distance
    d_min = d_a + d_d

    D = distance_m

    # Check if we can reach full warp speed
    if D < d_min:
        # Reduced peak warp speed
        v_warp_ms = (D * k_a * k_d) / (k_a + k_d)

    # Acceleration time
    # t_accel = (1/k_a) * ln(v_warp_ms / k_a)
    # But k_a is in AU/s, so convert v_warp_ms to AU/s for this calculation
    v_warp_au_actual = v_warp_ms / AU_IN_M
    if k_a > 0 and v_warp_au_actual > 0:
        t_accel = (1 / k_a) * math.log(v_warp_au_actual / k_a)
    else:
        t_accel = 0

    # Deceleration time
    # t_decel = (1/k_d) * ln(v_warp_ms / v_drop)
    if k_d > 0 and v_warp_ms > v_drop:
        t_decel = (1 / k_d) * math.log(v_warp_ms / v_drop)
    else:
        t_decel = 0

    # Cruise time (if distance >= d_min)
    t_cruise = 0
    if D >= d_min:
        t_cruise = (D - d_a - d_d) / v_warp_ms

    # Total warp time
    warp_time = t_accel + t_cruise + t_decel

    return align_time + warp_time


def find_entity(name: str, systems: Dict[int, System], stations: Dict[int, Station]) -> Tuple[Optional[System], Optional[Station]]:
    """Find a system or station by name."""
    # Check if it's a station
    for station in stations.values():
        if station.name == name:
            return None, station

    # Check if it's a system
    for system in systems.values():
        if system.name == name:
            return system, None

    return None, None


def get_first_gate_lexicographically(system_id: int, stargates: Dict[int, List[Stargate]],
                                      jumps: Dict[int, Set[int]], systems: Dict[int, System]) -> Optional[Stargate]:
    """Get the first gate lexicographically in a system."""
    if system_id not in stargates:
        return None

    gates = stargates[system_id]
    if not gates:
        return None

    # Sort gates by destination system name lexicographically
    gate_with_dest = []
    for gate in gates:
        # Extract destination system name from gate name like "Stargate (Jita)"
        gate_name = gate.name
        if '(' in gate_name and ')' in gate_name:
            dest_name = gate_name[gate_name.index('(') + 1:gate_name.rindex(')')]
            gate_with_dest.append((dest_name, gate))
        else:
            gate_with_dest.append((gate_name, gate))

    # Sort by destination name
    gate_with_dest.sort(key=lambda x: x[0])
    return gate_with_dest[0][1] if gate_with_dest else None


def find_gate_to_system(from_system_id: int, to_system_id: int,
                        stargates: Dict[int, List[Stargate]],
                        jumps: Dict[int, Set[int]], systems: Dict[int, System]) -> Optional[Stargate]:
    """Find the stargate in from_system that leads to to_system."""
    if from_system_id not in stargates:
        return None

    target_name = systems[to_system_id].name

    for gate in stargates[from_system_id]:
        gate_name = gate.name
        if '(' in gate_name and ')' in gate_name:
            dest_name = gate_name[gate_name.index('(') + 1:gate_name.rindex(')')]
            if dest_name == target_name:
                return gate

    return None


@dataclass
class RouteState:
    """State for Dijkstra's algorithm."""
    current_system_id: int
    zarzakh_locked_gate: Optional[int] = None  # System ID of the gate we came from
    zarzakh_lock_remaining: float = 0  # Seconds remaining on lock

    def __hash__(self):
        return hash((self.current_system_id, self.zarzakh_locked_gate))

    def __eq__(self, other):
        return (self.current_system_id == other.current_system_id and
                self.zarzakh_locked_gate == other.zarzakh_locked_gate)


def find_route(start_system_id: int, start_station: Optional[Station],
               end_system_id: int, end_station: Optional[Station],
               systems: Dict[int, System], jumps: Dict[int, Set[int]],
               stargates: Dict[int, List[Stargate]], warp_params: WarpParams) -> Tuple[List[int], float]:
    """
    Find the optimal route using Dijkstra's algorithm.

    Returns:
        Tuple of (list of system IDs, total time in seconds)
    """
    # State: (current_system_id, zarzakh_locked_gate_from_system_id)
    # zarzakh_locked_gate is None if not in Zarzakh or no lock

    # Priority queue: (time, system_id, zarzakh_locked_gate, path)
    # zarzakh_locked_gate is the system we came from to Zarzakh

    # Initial state
    # If starting from station, we need to undock first
    initial_time = 0
    if start_station:
        initial_time = warp_params.dock_time  # Undock time

    # Priority queue: (time, system_id, zarzakh_locked_gate, path)
    pq = [(initial_time, start_system_id, None, [start_system_id])]

    # Visited: (system_id, zarzakh_locked_gate) -> min time
    visited = {}

    while pq:
        time, current_system, zarzakh_lock, path = heapq.heappop(pq)

        # Check if we've reached the destination
        if current_system == end_system_id:
            # Calculate final time
            final_time = time
            if end_station:
                # Need to dock
                final_time += warp_params.dock_time
            return path, final_time

        state_key = (current_system, zarzakh_lock)
        if state_key in visited and visited[state_key] <= time:
            continue
        visited[state_key] = time

        # Get neighbors
        if current_system not in jumps:
            continue

        for neighbor_system in jumps[current_system]:
            # Calculate time to reach neighbor
            neighbor_time = time

            # Find the gate in current system to neighbor
            gate = find_gate_to_system(current_system, neighbor_system, stargates, jumps, systems)
            if gate is None:
                # If we can't find the gate, use system center as position
                current_sys = systems[current_system]
                gate = Stargate(0, current_system, "Gate", current_sys.x, current_sys.y, current_sys.z)

            # Determine starting position
            if path and len(path) == 1:
                # Starting system - need to determine initial position
                if start_station:
                    # Starting from station, position is station
                    start_x, start_y, start_z = start_station.x, start_station.y, start_station.z
                else:
                    # Starting from system, at first gate lexicographically
                    first_gate = get_first_gate_lexicographically(current_system, stargates, jumps, systems)
                    if first_gate:
                        start_x, start_y, start_z = first_gate.x, first_gate.y, first_gate.z
                    else:
                        # No gates, use system center
                        current_sys = systems[current_system]
                        start_x, start_y, start_z = current_sys.x, current_sys.y, current_sys.z
            else:
                # We just jumped in, so we're at the gate we came from
                # The gate that leads back to the previous system
                prev_system = path[-2] if len(path) >= 2 else start_system_id
                entry_gate = find_gate_to_system(current_system, prev_system, stargates, jumps, systems)
                if entry_gate:
                    start_x, start_y, start_z = entry_gate.x, entry_gate.y, entry_gate.z
                else:
                    current_sys = systems[current_system]
                    start_x, start_y, start_z = current_sys.x, current_sys.y, current_sys.z

            # Warp to gate
            warp_dist = calculate_distance(start_x, start_y, start_z, gate.x, gate.y, gate.z)
            neighbor_time += calculate_warp_time(warp_dist, warp_params)

            # Take the gate
            neighbor_time += warp_params.gate_time

            # Handle Zarzakh lock
            new_zarzakh_lock = zarzakh_lock

            if neighbor_system == ZARZAKH_SYSTEM_ID:
                # Entering Zarzakh - lock to this gate
                new_zarzakh_lock = current_system
            elif current_system == ZARZAKH_SYSTEM_ID:
                # Leaving Zarzakh - can only go to locked gate
                if zarzakh_lock is not None and neighbor_system != zarzakh_lock:
                    # Can't use this gate - locked to different gate
                    continue
                # Successfully leaving via locked gate - lock resets
                new_zarzakh_lock = None

            new_path = path + [neighbor_system]
            heapq.heappush(pq, (neighbor_time, neighbor_system, new_zarzakh_lock, new_path))

    return [], float('inf')


def format_time(seconds: float) -> str:
    """Format time in HH:MM format, rounded up to nearest minute."""
    minutes = math.ceil(seconds / 60)
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours:02d}:{mins:02d}"


def main():
    parser = argparse.ArgumentParser(description='EVE Online Hauling Route Planner')
    parser.add_argument('start', type=str, help='Starting location (system or station name)')
    parser.add_argument('end', type=str, help='Ending location (system or station name)')
    parser.add_argument('--align', type=float, required=True, help='Align time in seconds (must be > 0)')
    parser.add_argument('--top-speed', type=float, required=True, help='Top subwarp speed in m/s (must be >= 0)')
    parser.add_argument('--warp-speed', type=float, required=True, help='Warp speed in AU/s (must be > 0)')
    parser.add_argument('--dock-time', type=float, required=True, help='Dock/undock time in seconds (must be > 0)')
    parser.add_argument('--gate-time', type=float, required=True, help='Gate time in seconds (must be > 0)')
    parser.add_argument('--sde', type=str, required=True, help='Path to SDE directory')

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

    # Load SDE data
    systems = load_systems(args.sde)
    jumps = load_jumps(args.sde)
    stations = load_stations(args.sde)
    stargates = load_stargates(args.sde)

    # Find start and end
    start_system, start_station = find_entity(args.start, systems, stations)
    end_system, end_station = find_entity(args.end, systems, stations)

    if start_system is None and start_station is None:
        print(f"Error: Start location '{args.start}' not found", file=sys.stderr)
        sys.exit(1)

    if end_system is None and end_station is None:
        print(f"Error: End location '{args.end}' not found", file=sys.stderr)
        sys.exit(1)

    # Get actual system IDs
    start_system_id = start_system.system_id if start_system else start_station.system_id
    end_system_id = end_system.system_id if end_system else end_station.system_id

    warp_params = WarpParams(
        align_time=args.align,
        top_speed=args.top_speed,
        warp_speed=args.warp_speed,
        dock_time=args.dock_time,
        gate_time=args.gate_time
    )

    # Handle same system case
    if start_system_id == end_system_id:
        total_time = 0
        start_name = args.start
        end_name = args.end

        # Print START
        print(f"START: {start_name}")

        # If starting from station, undock
        if start_station:
            print("UNDOCK")
            total_time += warp_params.dock_time

        # Warp within system
        # Determine positions
        if start_station:
            start_pos = (start_station.x, start_station.y, start_station.z)
        else:
            # Starting at first gate lexicographically
            first_gate = get_first_gate_lexicographically(start_system_id, stargates, jumps, systems)
            if first_gate:
                start_pos = (first_gate.x, first_gate.y, first_gate.z)
            else:
                sys_obj = systems[start_system_id]
                start_pos = (sys_obj.x, sys_obj.y, sys_obj.z)

        if end_station:
            end_pos = (end_station.x, end_station.y, end_station.z)
        else:
            # End is a system - route completes upon entering
            # Since we're already in the system, no travel needed
            end_pos = start_pos

        # Calculate warp time if positions differ
        if start_pos != end_pos:
            warp_dist = calculate_distance(*start_pos, *end_pos)
            total_time += calculate_warp_time(warp_dist, warp_params)

        # If ending at station, dock
        if end_station:
            print(f"DOCK: {end_name}")
            total_time += warp_params.dock_time

        print(f"DONE: {format_time(total_time)}")
        return

    # Find route
    route, total_time = find_route(
        start_system_id, start_station,
        end_system_id, end_station,
        systems, jumps, stargates, warp_params
    )

    if not route:
        print("Error: No route found", file=sys.stderr)
        sys.exit(1)

    # Output
    print(f"START: {args.start}")

    if start_station:
        print("UNDOCK")

    # Print GO line if route has multiple systems
    if len(route) > 1:
        route_str = " -> ".join(f"{systems[sid].name} ({systems[sid].security:.1f})" for sid in route)
        print(f"GO: {route_str}")

    if end_station:
        print(f"DOCK: {args.end}")

    print(f"DONE: {format_time(total_time)}")


if __name__ == '__main__':
    main()
