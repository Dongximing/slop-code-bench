#!/usr/bin/env python3
"""
EVE Online Hauling Route Planner
Calculates optimal routes through New Eden considering warp times and Zarzakh gate locks.
Supports cargo hauling operations with manifest files and ship configuration.
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

import yaml

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


@dataclass
class ShipConfig:
    """Ship configuration from config file."""
    name: str
    ship_type: str  # Deep Space Transport, Blockade Runner, Freighter
    align: float
    top_speed: float
    warp_speed: float
    cargo_size: int


@dataclass
class TimeConfig:
    """Time configuration from config file."""
    dock: float
    gate: float
    move_cargo: float


@dataclass
class Waypoint:
    """A waypoint from the manifest."""
    name: str
    cargo: Optional[float]


@dataclass
class Manifest:
    """A cargo manifest."""
    start_cargo: float  # 0 if null
    waypoints: List[Waypoint]


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


def load_config(config_path: str) -> Tuple[Dict[str, ShipConfig], TimeConfig]:
    """Load ship and time configuration from YAML config file."""
    with open(config_path, 'r') as f:
        data = yaml.safe_load(f)

    ships = {}
    for ship_name, ship_data in data.get('ships', {}).items():
        ships[ship_name] = ShipConfig(
            name=ship_name,
            ship_type=ship_data['type'],
            align=float(ship_data['align']),
            top_speed=float(ship_data['top_speed']),
            warp_speed=float(ship_data['warp_speed']),
            cargo_size=int(ship_data['cargo_size'])
        )

    times_data = data.get('times', {})
    times = TimeConfig(
        dock=float(times_data['dock']),
        gate=float(times_data['gate']),
        move_cargo=float(times_data['move_cargo'])
    )

    return ships, times


def load_manifest(manifest_path: str) -> Optional[Manifest]:
    """Load cargo manifest from YAML file. Returns None if no manifest."""
    if manifest_path is None:
        return None

    with open(manifest_path, 'r') as f:
        data = yaml.safe_load(f)

    if data is None:
        return Manifest(start_cargo=0, waypoints=[])

    start_cargo = data.get('start_cargo', 0)
    if start_cargo is None:
        start_cargo = 0

    waypoints = []
    for wp_data in data.get('waypoints', []) or []:
        waypoints.append(Waypoint(
            name=wp_data['name'],
            cargo=wp_data.get('cargo')
        ))

    return Manifest(start_cargo=float(start_cargo), waypoints=waypoints)


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


def _is_high_sec(system_id: int, systems: Dict[int, System]) -> bool:
    """Check if a system is high security (sec >= 0.5)."""
    return systems[system_id].security >= 0.5


def find_route(start_system_id: int, start_station: Optional[Station],
               end_system_id: int, end_station: Optional[Station],
               systems: Dict[int, System], jumps: Dict[int, Set[int]],
               stargates: Dict[int, List[Stargate]], warp_params: WarpParams,
               is_freighter: bool = False) -> Tuple[List[int], float]:
    """
    Find the optimal route using Dijkstra's algorithm.
    Returns (list of system IDs, total time in seconds).
    """
    initial_time = warp_params.dock_time if start_station else 0
    # (time, current_system, zarzakh_lock, prev_system)
    pq = [(initial_time, start_system_id, None, None)]
    best_time: Dict[Tuple[int, Optional[int]], float] = {}
    # came_from[(system, lock)] = (prev_system, prev_lock)
    came_from: Dict[Tuple[int, Optional[int]], Tuple[int, Optional[int]]] = {}

    while pq:
        time, current_system, zarzakh_lock, prev_system = heapq.heappop(pq)

        state_key = (current_system, zarzakh_lock)
        if state_key in best_time:
            continue
        best_time[state_key] = time
        came_from[state_key] = (prev_system, None)  # prev_lock computed during reconstruction

        if current_system == end_system_id:
            final_time = time + (warp_params.dock_time if end_station else 0)
            # Reconstruct path by backtracking
            path = []
            state = state_key
            while state[0] is not None:
                path.append(state[0])
                prev_sys, _ = came_from.get(state, (None, None))
                if prev_sys is None:
                    break
                # Determine previous zarzakh_lock
                if prev_sys == ZARZAKH_SYSTEM_ID:
                    prev_lock = state[0]  # exit lock
                elif state[0] == ZARZAKH_SYSTEM_ID:
                    prev_lock = None
                else:
                    prev_lock = state[1]
                state = (prev_sys, prev_lock)
            path.reverse()
            return path, final_time

        if current_system not in jumps:
            continue

        for neighbor_system in jumps[current_system]:
            # Freighter restriction: skip non-high-sec systems
            if is_freighter and not _is_high_sec(neighbor_system, systems):
                continue

            neighbor_time = time

            gate = find_gate_to_system(current_system, neighbor_system, stargates, systems)
            if gate is None:
                current_sys = systems[current_system]
                gate = Stargate(0, current_system, "Gate", current_sys.x, current_sys.y, current_sys.z)

            # Calculate warp distance from current position to gate
            if prev_system is None:
                # Starting from start location
                start_x, start_y, start_z = _get_position(current_system, start_station, stargates, systems)
            else:
                start_x, start_y, start_z = _get_entry_position(current_system, prev_system, stargates, systems)

            warp_dist = calculate_distance(start_x, start_y, start_z, gate.x, gate.y, gate.z)
            neighbor_time += calculate_warp_time(warp_dist, warp_params) + warp_params.gate_time

            new_zarzakh_lock = zarzakh_lock
            if neighbor_system == ZARZAKH_SYSTEM_ID:
                new_zarzakh_lock = current_system
            elif current_system == ZARZAKH_SYSTEM_ID:
                if zarzakh_lock is not None and neighbor_system != zarzakh_lock:
                    continue
                new_zarzakh_lock = None

            neighbor_state = (neighbor_system, new_zarzakh_lock)
            if neighbor_state not in best_time:
                heapq.heappush(pq, (neighbor_time, neighbor_system, new_zarzakh_lock, current_system))

    return [], float('inf')


def find_route_freighter_fallback(start_system_id: int, start_station: Optional[Station],
                                  end_system_id: int, end_station: Optional[Station],
                                  systems: Dict[int, System], jumps: Dict[int, Set[int]],
                                  stargates: Dict[int, List[Stargate]], warp_params: WarpParams) -> Tuple[List[int], float]:
    """
    Find route for freighter: first try high-sec only, then fall back to including low-sec.
    """
    # Try high-sec only first
    route, time = find_route(start_system_id, start_station,
                             end_system_id, end_station,
                             systems, jumps, stargates, warp_params,
                             is_freighter=True)
    if route:
        return route, time

    # Fallback: allow high-sec and low-sec (but NOT null-sec, security < 0)
    initial_time = warp_params.dock_time if start_station else 0
    pq = [(initial_time, start_system_id, None, None)]
    best_time: Dict[Tuple[int, Optional[int]], float] = {}
    came_from: Dict[Tuple[int, Optional[int]], Tuple[int, Optional[int]]] = {}

    while pq:
        time_val, current_system, zarzakh_lock, prev_system = heapq.heappop(pq)

        state_key = (current_system, zarzakh_lock)
        if state_key in best_time:
            continue
        best_time[state_key] = time_val
        came_from[state_key] = (prev_system, None)

        if current_system == end_system_id:
            final_time = time_val + (warp_params.dock_time if end_station else 0)
            # Reconstruct path
            path = []
            state = state_key
            while state[0] is not None:
                path.append(state[0])
                prev_sys, _ = came_from.get(state, (None, None))
                if prev_sys is None:
                    break
                if prev_sys == ZARZAKH_SYSTEM_ID:
                    prev_lock = state[0]
                elif state[0] == ZARZAKH_SYSTEM_ID:
                    prev_lock = None
                else:
                    prev_lock = state[1]
                state = (prev_sys, prev_lock)
            path.reverse()
            return path, final_time

        if current_system not in jumps:
            continue

        for neighbor_system in jumps[current_system]:
            # Allow high-sec and low-sec only (security >= 0)
            if systems[neighbor_system].security < 0:
                continue

            neighbor_time = time_val

            gate = find_gate_to_system(current_system, neighbor_system, stargates, systems)
            if gate is None:
                current_sys = systems[current_system]
                gate = Stargate(0, current_system, "Gate", current_sys.x, current_sys.y, current_sys.z)

            if prev_system is None:
                start_x, start_y, start_z = _get_position(current_system, start_station, stargates, systems)
            else:
                start_x, start_y, start_z = _get_entry_position(current_system, prev_system, stargates, systems)

            warp_dist = calculate_distance(start_x, start_y, start_z, gate.x, gate.y, gate.z)
            neighbor_time += calculate_warp_time(warp_dist, warp_params) + warp_params.gate_time

            new_zarzakh_lock = zarzakh_lock
            if neighbor_system == ZARZAKH_SYSTEM_ID:
                new_zarzakh_lock = current_system
            elif current_system == ZARZAKH_SYSTEM_ID:
                if zarzakh_lock is not None and neighbor_system != zarzakh_lock:
                    continue
                new_zarzakh_lock = None

            neighbor_state = (neighbor_system, new_zarzakh_lock)
            if neighbor_state not in best_time:
                heapq.heappush(pq, (neighbor_time, neighbor_system, new_zarzakh_lock, current_system))

    return [], float('inf')


def format_time(seconds: float) -> str:
    """Format time in HH:MM format, rounded up to nearest minute."""
    minutes = math.ceil(seconds / 60)
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours:02d}:{mins:02d}"


def format_cargo(amount: float) -> str:
    """Format cargo amount with comma-separated thousands and 2 decimal places."""
    return f"{amount:,.2f} m3"


def _resolve_location(name: str, systems: Dict[int, System], stations: Dict[int, Station]):
    """Resolve a location name to (system_id, station_or_none)."""
    system, station = find_entity(name, systems, stations)
    if station:
        return station.system_id, station
    if system:
        return system.system_id, None
    return None, None


def _find_route_with_freighter_check(start_system_id: int, start_station: Optional[Station],
                                     end_system_id: int, end_station: Optional[Station],
                                     systems: Dict[int, System], jumps: Dict[int, Set[int]],
                                     stargates: Dict[int, List[Stargate]], warp_params: WarpParams,
                                     is_freighter: bool) -> Tuple[List[int], float]:
    """Find route, applying freighter restrictions if applicable."""
    if is_freighter:
        return find_route_freighter_fallback(start_system_id, start_station,
                                             end_system_id, end_station,
                                             systems, jumps, stargates, warp_params)
    else:
        return find_route(start_system_id, start_station,
                         end_system_id, end_station,
                         systems, jumps, stargates, warp_params)


def _get_route_display(route: List[int], systems: Dict[int, System]) -> str:
    """Format route for GO display."""
    return " -> ".join(f"{systems[sid].name} ({systems[sid].security:.1f})" for sid in route)


def main():
    parser = argparse.ArgumentParser(description='EVE Online Hauling Route Planner')
    parser.add_argument('start', type=str, help='Starting location (system or station name)')
    parser.add_argument('end', type=str, help='Ending location (system or station name)')
    parser.add_argument('--manifest', type=str, default=None, help='Path to manifest YAML file')
    parser.add_argument('--config', type=str, default=None, help='Path to ship config YAML file')
    parser.add_argument('--ship', type=str, default=None, help='Ship name from config file')
    parser.add_argument('--sde', type=str, default=None, help='Path to SDE directory')

    # Legacy arguments (for backward compatibility without manifest)
    parser.add_argument('--align', type=float, default=None, help='Align time in seconds (must be > 0)')
    parser.add_argument('--top-speed', type=float, default=None, help='Top subwarp speed in m/s (must be >= 0)')
    parser.add_argument('--warp-speed', type=float, default=None, help='Warp speed in AU/s (must be > 0)')
    parser.add_argument('--dock-time', type=float, default=None, help='Dock/undock time in seconds (must be > 0)')
    parser.add_argument('--gate-time', type=float, default=None, help='Gate time in seconds (must be > 0)')

    args = parser.parse_args()

    # Determine mode: manifest mode vs legacy mode
    manifest_mode = args.manifest is not None or args.config is not None or args.ship is not None

    if manifest_mode:
        # Manifest mode - require config, ship, and sde
        if not args.config:
            print("Error: --config is required in manifest mode", file=sys.stderr)
            sys.exit(1)
        if not args.ship:
            print("Error: --ship is required in manifest mode", file=sys.stderr)
            sys.exit(1)
        if not args.sde:
            print("Error: --sde is required", file=sys.stderr)
            sys.exit(1)

        # Load config
        ships_config, times_config = load_config(args.config)
        if args.ship not in ships_config:
            print(f"Error: Ship '{args.ship}' not found in config", file=sys.stderr)
            sys.exit(1)

        ship = ships_config[args.ship]

        # Load manifest (None if not provided)
        manifest = load_manifest(args.manifest)

        # Load SDE
        systems = load_systems(args.sde)
        jumps = load_jumps(args.sde)
        stations = load_stations(args.sde)
        stargates = load_stargates(args.sde)

        warp_params = WarpParams(
            align_time=ship.align,
            top_speed=ship.top_speed,
            warp_speed=ship.warp_speed,
            dock_time=times_config.dock,
            gate_time=times_config.gate
        )

        is_freighter = ship.ship_type == "Freighter"

        # Resolve start and end locations
        start_system_id, start_station = _resolve_location(args.start, systems, stations)
        end_system_id, end_station = _resolve_location(args.end, systems, stations)

        if start_system_id is None:
            print(f"Error: Start location '{args.start}' not found", file=sys.stderr)
            sys.exit(1)
        if end_system_id is None:
            print(f"Error: End location '{args.end}' not found", file=sys.stderr)
            sys.exit(1)

        # Handle no manifest or no cargo
        if manifest is None or (manifest.start_cargo <= 0 and all(wp.cargo is None or wp.cargo <= 0 for wp in manifest.waypoints)):
            # Simple route from start to end
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

            route, total_time = _find_route_with_freighter_check(
                start_system_id, start_station, end_system_id, end_station,
                systems, jumps, stargates, warp_params, is_freighter)

            if not route:
                print("Error: No route found", file=sys.stderr)
                sys.exit(1)

            print(f"START: {args.start}")
            if start_station:
                print("UNDOCK")
            if len(route) > 1:
                print(f"GO: {_get_route_display(route, systems)}")
            if end_station:
                print(f"DOCK: {args.end}")
            print(f"DONE: {format_time(total_time)}")
            return

        # Manifest mode with cargo - plan trips
        cargo_capacity = ship.cargo_size

        # Resolve waypoint locations
        waypoint_info = []  # [(system_id, station, cargo_amount, name)]
        for wp in manifest.waypoints:
            wp_sys_id, wp_station = _resolve_location(wp.name, systems, stations)
            if wp_sys_id is None:
                print(f"Error: Waypoint location '{wp.name}' not found", file=sys.stderr)
                sys.exit(1)
            cargo_amt = wp.cargo if wp.cargo is not None else 0
            waypoint_info.append((wp_sys_id, wp_station, cargo_amt, wp.name))

        # Build list of all cargo pickups with amounts
        # Each pickup: (system_id, station, remaining_cargo, name)
        pickups = []
        if manifest.start_cargo > 0:
            pickups.append([start_system_id, start_station, manifest.start_cargo, args.start])
        for wp_sys_id, wp_station, cargo_amt, wp_name in waypoint_info:
            if cargo_amt > 0:
                pickups.append([wp_sys_id, wp_station, cargo_amt, wp_name])

        # No cargo to move
        if not pickups:
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

            route, total_time = _find_route_with_freighter_check(
                start_system_id, start_station, end_system_id, end_station,
                systems, jumps, stargates, warp_params, is_freighter)

            if not route:
                print("Error: No route found", file=sys.stderr)
                sys.exit(1)

            print(f"START: {args.start}")
            if start_station:
                print("UNDOCK")
            if len(route) > 1:
                print(f"GO: {_get_route_display(route, systems)}")
            if end_station:
                print(f"DOCK: {args.end}")
            print(f"DONE: {format_time(total_time)}")
            return

        # Multi-trip planning
        output_lines = []
        output_lines.append(f"START: {args.start}")

        total_moved = 0.0
        total_time = 0.0
        trip_num = 1

        # Current position
        current_system_id = start_system_id
        current_station = start_station
        current_name = args.start
        in_station = bool(start_station)

        while any(p[2] > 0 for p in pickups):
            cargo_in_hold = 0.0

            # For trips after the first, return to start to get more cargo
            if trip_num > 1:
                # Undock from end station if we're in a station
                if in_station:
                    output_lines.append("UNDOCK")
                    total_time += warp_params.dock_time
                    in_station = False

                # Travel back to start
                if current_system_id != start_system_id:
                    route, route_time = _find_route_with_freighter_check(
                        current_system_id, None,
                        start_system_id, start_station,
                        systems, jumps, stargates, warp_params, is_freighter)
                    if not route:
                        print("Error: No route back to start", file=sys.stderr)
                        sys.exit(1)
                    output_lines.append(f"GO: {_get_route_display(route, systems)}")
                    total_time += route_time
                    current_system_id = start_system_id

                # Dock at start station
                if start_station:
                    output_lines.append(f"DOCK: {args.start}")
                    total_time += warp_params.dock_time
                    in_station = True

                current_station = start_station
                current_name = args.start

            # Load cargo at current position if it has remaining cargo
            for p in pickups:
                if p[2] > 0 and p[0] == current_system_id and p[1] == current_station:
                    can_load = min(p[2], cargo_capacity - cargo_in_hold)
                    if can_load > 0:
                        output_lines.append(f"LOAD: {format_cargo(can_load)}")
                        total_time += times_config.move_cargo
                        cargo_in_hold += can_load
                        p[2] -= can_load

            # Undock if at a station
            if in_station:
                output_lines.append("UNDOCK")
                total_time += warp_params.dock_time
                in_station = False

            # Visit waypoints in order to pick up more cargo (only on first trip)
            if trip_num == 1:
                for wp_sys_id, wp_station, wp_cargo, wp_name in waypoint_info:
                    if cargo_in_hold >= cargo_capacity:
                        break
                    # Find matching pickup
                    for p in pickups:
                        if p[2] > 0 and p[3] == wp_name:
                            can_load = min(p[2], cargo_capacity - cargo_in_hold)
                            if can_load > 0:
                                # Travel to waypoint
                                if current_system_id != wp_sys_id:
                                    route, route_time = _find_route_with_freighter_check(
                                        current_system_id, None,
                                        wp_sys_id, wp_station,
                                        systems, jumps, stargates, warp_params, is_freighter)
                                    if not route:
                                        print(f"Error: No route to waypoint {wp_name}", file=sys.stderr)
                                        sys.exit(1)
                                    output_lines.append(f"GO: {_get_route_display(route, systems)}")
                                    total_time += route_time
                                    current_system_id = wp_sys_id

                                # Dock at waypoint
                                if wp_station:
                                    output_lines.append(f"DOCK: {wp_name}")
                                    total_time += warp_params.dock_time
                                    in_station = True

                                # Load cargo
                                output_lines.append(f"LOAD: {format_cargo(can_load)}")
                                total_time += times_config.move_cargo
                                cargo_in_hold += can_load
                                p[2] -= can_load

                                # Undock
                                if wp_station:
                                    output_lines.append("UNDOCK")
                                    total_time += warp_params.dock_time
                                    in_station = False

                                current_system_id = wp_sys_id
                                current_station = None  # We're in space
                                current_name = wp_name
                            break

            # Travel to end and deliver
            if current_system_id != end_system_id:
                route, route_time = _find_route_with_freighter_check(
                    current_system_id, None,
                    end_system_id, end_station,
                    systems, jumps, stargates, warp_params, is_freighter)
                if not route:
                    print("Error: No route to destination", file=sys.stderr)
                    sys.exit(1)
                output_lines.append(f"GO: {_get_route_display(route, systems)}")
                total_time += route_time
                current_system_id = end_system_id

            # Dock at end
            if end_station:
                output_lines.append(f"DOCK: {args.end}")
                total_time += warp_params.dock_time
                in_station = True

            # Unload
            if cargo_in_hold > 0:
                output_lines.append(f"UNLOAD: {format_cargo(cargo_in_hold)}")
                total_time += times_config.move_cargo
                total_moved += cargo_in_hold

            # Update position
            current_system_id = end_system_id
            current_station = end_station
            current_name = args.end

            # Check if we need more trips
            if any(p[2] > 0 for p in pickups):
                trip_num += 1
                output_lines.append(f"[--- TRIP {trip_num} ---]")

        output_lines.append(f"DONE: {format_time(total_time)}")
        if total_moved > 0:
            output_lines.append(f"MOVED: {format_cargo(total_moved)}")

        print('\n'.join(output_lines))

    else:
        # Legacy mode - require individual ship parameters
        if args.align is None:
            print("Error: --align is required", file=sys.stderr)
            sys.exit(1)
        if args.top_speed is None:
            print("Error: --top-speed is required", file=sys.stderr)
            sys.exit(1)
        if args.warp_speed is None:
            print("Error: --warp-speed is required", file=sys.stderr)
            sys.exit(1)
        if args.dock_time is None:
            print("Error: --dock-time is required", file=sys.stderr)
            sys.exit(1)
        if args.gate_time is None:
            print("Error: --gate-time is required", file=sys.stderr)
            sys.exit(1)
        if not args.sde:
            print("Error: --sde is required", file=sys.stderr)
            sys.exit(1)

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
