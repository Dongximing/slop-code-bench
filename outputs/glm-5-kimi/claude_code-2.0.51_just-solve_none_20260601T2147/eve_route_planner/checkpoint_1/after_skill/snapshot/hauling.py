#!/usr/bin/env python3
"""
EVE Online Hauling Route Planner
Calculates optimal routes through New Eden considering warp times and Zarzakh gate locks.
"""

import argparse
import bz2
import csv
import heapq
import math
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Set

# Constants
AU_IN_M = 149_597_870_700  # 1 AU in meters
ZARZAKH_SYSTEM_ID = 30100000


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
    jumps: Dict[int, Set[int]] = {}
    filepath = os.path.join(sde_path, 'mapSolarSystemJumps.csv.bz2')
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            from_id = int(row['fromSolarSystemID'])
            to_id = int(row['toSolarSystemID'])
            jumps.setdefault(from_id, set()).add(to_id)
            jumps.setdefault(to_id, set()).add(from_id)
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
    stargates: Dict[int, List[Stargate]] = {}
    filepath = os.path.join(sde_path, 'mapDenormalize.csv.bz2')
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if int(row['groupID']) != 10:  # 10 is stargate group
                continue
            system_id = int(row['solarSystemID'])
            stargates.setdefault(system_id, []).append(Stargate(
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
    """Calculate warp time in seconds based on the EVE warp model."""
    align_time = math.ceil(warp_params.align_time)
    v_warp_au = warp_params.warp_speed  # AU/s
    v_subwarp = warp_params.top_speed  # m/s
    v_drop = min(v_subwarp / 2, 100)  # m/s

    k_a = v_warp_au  # AU/s acceleration rate
    k_d = min(v_warp_au / 3, 2)  # AU/s deceleration rate, capped at 2

    v_warp_ms = v_warp_au * AU_IN_M
    d_a = AU_IN_M  # acceleration distance: 1 AU
    d_d = v_warp_ms / k_d  # deceleration distance
    d_min = d_a + d_d

    D = distance_m
    if D < d_min:
        v_warp_ms = (D * k_a * k_d) / (k_a + k_d)

    v_warp_au_actual = v_warp_ms / AU_IN_M
    t_accel = (1 / k_a) * math.log(v_warp_au_actual / k_a) if k_a > 0 and v_warp_au_actual > 0 else 0
    t_decel = (1 / k_d) * math.log(v_warp_ms / v_drop) if k_d > 0 and v_warp_ms > v_drop else 0
    t_cruise = (D - d_a - d_d) / v_warp_ms if D >= d_min else 0

    return align_time + t_accel + t_cruise + t_decel


def find_entity(name: str, systems: Dict[int, System], stations: Dict[int, Station]) -> Tuple[Optional[System], Optional[Station]]:
    """Find a system or station by name."""
    for station in stations.values():
        if station.name == name:
            return None, station
    for system in systems.values():
        if system.name == name:
            return system, None
    return None, None


def _gate_destination_name(gate_name: str) -> str:
    """Extract destination system name from gate name like 'Stargate (Jita)'."""
    if '(' in gate_name and ')' in gate_name:
        return gate_name[gate_name.index('(') + 1:gate_name.rindex(')')]
    return gate_name


def _get_position(system_id: int, station: Optional[Station],
                  stargates: Dict[int, List[Stargate]], systems: Dict[int, System]) -> Tuple[float, float, float]:
    """Get position: station if provided, else first gate lexicographically, else system center."""
    if station:
        return station.x, station.y, station.z
    first_gate = get_first_gate_lexicographically(system_id, stargates, systems)
    if first_gate:
        return first_gate.x, first_gate.y, first_gate.z
    sys_obj = systems[system_id]
    return sys_obj.x, sys_obj.y, sys_obj.z


def _get_entry_position(system_id: int, prev_system_id: int,
                        stargates: Dict[int, List[Stargate]], systems: Dict[int, System]) -> Tuple[float, float, float]:
    """Get position after jumping in: entry gate from previous system, else system center."""
    entry_gate = find_gate_to_system(system_id, prev_system_id, stargates, systems)
    if entry_gate:
        return entry_gate.x, entry_gate.y, entry_gate.z
    sys_obj = systems[system_id]
    return sys_obj.x, sys_obj.y, sys_obj.z


def get_first_gate_lexicographically(system_id: int, stargates: Dict[int, List[Stargate]],
                                      systems: Dict[int, System]) -> Optional[Stargate]:
    """Get the first gate lexicographically in a system."""
    gates = stargates.get(system_id)
    if not gates:
        return None
    return min(gates, key=lambda g: _gate_destination_name(g.name))


def find_gate_to_system(from_system_id: int, to_system_id: int,
                        stargates: Dict[int, List[Stargate]], systems: Dict[int, System]) -> Optional[Stargate]:
    """Find the stargate in from_system that leads to to_system."""
    target_name = systems[to_system_id].name
    for gate in stargates.get(from_system_id, []):
        if _gate_destination_name(gate.name) == target_name:
            return gate
    return None


def find_route(start_system_id: int, start_station: Optional[Station],
               end_system_id: int, end_station: Optional[Station],
               systems: Dict[int, System], jumps: Dict[int, Set[int]],
               stargates: Dict[int, List[Stargate]], warp_params: WarpParams) -> Tuple[List[int], float]:
    """
    Find the optimal route using Dijkstra's algorithm.
    Returns (list of system IDs, total time in seconds).
    """
    initial_time = warp_params.dock_time if start_station else 0
    pq = [(initial_time, start_system_id, None, [start_system_id])]
    visited = {}

    while pq:
        time, current_system, zarzakh_lock, path = heapq.heappop(pq)

        if current_system == end_system_id:
            final_time = time + (warp_params.dock_time if end_station else 0)
            return path, final_time

        state_key = (current_system, zarzakh_lock)
        if state_key in visited and visited[state_key] <= time:
            continue
        visited[state_key] = time

        if current_system not in jumps:
            continue

        for neighbor_system in jumps[current_system]:
            neighbor_time = time

            gate = find_gate_to_system(current_system, neighbor_system, stargates, systems)
            if gate is None:
                current_sys = systems[current_system]
                gate = Stargate(0, current_system, "Gate", current_sys.x, current_sys.y, current_sys.z)

            if len(path) == 1:
                start_x, start_y, start_z = _get_position(current_system, start_station, stargates, systems)
            else:
                start_x, start_y, start_z = _get_entry_position(current_system, path[-2], stargates, systems)

            warp_dist = calculate_distance(start_x, start_y, start_z, gate.x, gate.y, gate.z)
            neighbor_time += calculate_warp_time(warp_dist, warp_params) + warp_params.gate_time

            new_zarzakh_lock = zarzakh_lock
            if neighbor_system == ZARZAKH_SYSTEM_ID:
                new_zarzakh_lock = current_system
            elif current_system == ZARZAKH_SYSTEM_ID:
                if zarzakh_lock is not None and neighbor_system != zarzakh_lock:
                    continue
                new_zarzakh_lock = None

            heapq.heappush(pq, (neighbor_time, neighbor_system, new_zarzakh_lock, path + [neighbor_system]))

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

    validations = [
        ('align', args.align, '> 0', args.align <= 0),
        ('top-speed', args.top_speed, '>= 0', args.top_speed < 0),
        ('warp-speed', args.warp_speed, '> 0', args.warp_speed <= 0),
        ('dock-time', args.dock_time, '> 0', args.dock_time <= 0),
        ('gate-time', args.gate_time, '> 0', args.gate_time <= 0),
    ]
    for name, _, cond, failed in validations:
        if failed:
            print(f"Error: --{name} must be {cond}", file=sys.stderr)
            sys.exit(1)

    systems = load_systems(args.sde)
    jumps = load_jumps(args.sde)
    stations = load_stations(args.sde)
    stargates = load_stargates(args.sde)

    start_system, start_station = find_entity(args.start, systems, stations)
    end_system, end_station = find_entity(args.end, systems, stations)

    if start_system is None and start_station is None:
        print(f"Error: Start location '{args.start}' not found", file=sys.stderr)
        sys.exit(1)

    if end_system is None and end_station is None:
        print(f"Error: End location '{args.end}' not found", file=sys.stderr)
        sys.exit(1)

    start_system_id = start_system.system_id if start_system else start_station.system_id
    end_system_id = end_system.system_id if end_system else end_station.system_id

    warp_params = WarpParams(
        align_time=args.align,
        top_speed=args.top_speed,
        warp_speed=args.warp_speed,
        dock_time=args.dock_time,
        gate_time=args.gate_time
    )

    if start_system_id == end_system_id:
        total_time = 0
        print(f"START: {args.start}")

        if start_station:
            print("UNDOCK")
            total_time += warp_params.dock_time

        start_pos = _get_position(start_system_id, start_station, stargates, systems)
        end_pos = (end_station.x, end_station.y, end_station.z) if end_station else start_pos

        if start_pos != end_pos:
            warp_dist = calculate_distance(*start_pos, *end_pos)
            total_time += calculate_warp_time(warp_dist, warp_params)

        if end_station:
            print(f"DOCK: {args.end}")
            total_time += warp_params.dock_time

        print(f"DONE: {format_time(total_time)}")
        return

    route, total_time = find_route(
        start_system_id, start_station,
        end_system_id, end_station,
        systems, jumps, stargates, warp_params
    )

    if not route:
        print("Error: No route found", file=sys.stderr)
        sys.exit(1)

    print(f"START: {args.start}")

    if start_station:
        print("UNDOCK")

    if len(route) > 1:
        route_str = " -> ".join(f"{systems[sid].name} ({systems[sid].security:.1f})" for sid in route)
        print(f"GO: {route_str}")

    if end_station:
        print(f"DOCK: {args.end}")

    print(f"DONE: {format_time(total_time)}")


if __name__ == '__main__':
    main()
