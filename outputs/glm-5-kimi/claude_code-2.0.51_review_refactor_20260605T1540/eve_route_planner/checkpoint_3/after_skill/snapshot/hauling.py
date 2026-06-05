#!/usr/bin/env python3
"""
EVE Online Travel Planning Tool

Calculates optimal routes through New Eden accounting for:
- Warp physics (acceleration, cruise, deceleration phases)
- Zarzakh gate locking mechanics
- Station docking/undocking times
- Gate jump times
- Cargo hauling with manifest support
"""

import argparse
import bz2
import csv
import heapq
import itertools
import json
import math
import os
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Set
import yaml

# Constants
AU_IN_M = 149_597_870_700.0  # 1 AU in meters
ZARZAKH_SYSTEM_ID = 30100000
ZARZAKH_LOCK_TIME = 6 * 60 * 60  # 6 hours in seconds


@dataclass
class System:
    """Represents a solar system."""
    id: int
    name: str
    security: float
    x: float
    y: float
    z: float


@dataclass
class Station:
    """Represents a station."""
    id: int
    name: str
    system_id: int
    x: float
    y: float
    z: float


@dataclass
class Stargate:
    """Represents a stargate."""
    id: int
    system_id: int
    name: str
    x: float
    y: float
    z: float


@dataclass
class TravelParams:
    """Travel parameters from CLI."""
    align_time: float
    top_speed: float
    warp_speed: float
    dock_time: float
    gate_time: float


@dataclass
class ShipConfig:
    """Ship configuration from config file."""
    name: str
    type: str  # Deep Space Transport, Blockade Runner, Freighter
    align_time: float
    top_speed: float
    warp_speed: float
    cargo_size: int
    ehp: Optional[int] = None  # Effective Hit Points


@dataclass
class TimesConfig:
    """Times configuration from config file."""
    dock: float
    gate: float
    move_cargo: float


@dataclass
class Config:
    """Full configuration."""
    ships: Dict[str, ShipConfig]
    times: TimesConfig
    min_isk_per_jump: Optional[float] = None
    max_isk_per_ehp: Optional[float] = None


@dataclass
class Waypoint:
    """A waypoint in the manifest."""
    name: str
    cargo: Optional[float]


@dataclass
class Manifest:
    """Cargo manifest."""
    start_cargo: Optional[float]
    waypoints: List[Waypoint]


@dataclass
class CargoOperation:
    """A cargo operation at a location."""
    location: str
    action: str  # 'load' or 'unload'
    amount: float


@dataclass
class TripSegment:
    """A segment of a trip."""
    start_location: str
    end_location: str
    route: List[str]
    load_before: Optional[float] = None  # Load before departing
    waypoint_loads: List[Tuple[str, float]] = field(default_factory=list)  # (location, amount) loads at waypoints
    unload_at_end: float = 0.0


@dataclass
class Contract:
    """A hauling contract."""
    id: int  # 1-indexed based on file order
    start: str
    end: str
    collateral: float
    m3: float
    actual_value: float
    reward: float
    issuer: str


class SDELoader:
    """Loads EVE Static Data Export."""

    def __init__(self, sde_dir: str):
        self.sde_dir = sde_dir
        self.systems: Dict[int, System] = {}
        self.system_names: Dict[str, int] = {}
        self.stations: Dict[int, Station] = {}
        self.station_names: Dict[str, int] = {}
        self.stargates_by_system: Dict[int, List[Stargate]] = {}  # system_id -> list of gates
        self.gate_positions: Dict[int, Tuple[float, float, float]] = {}  # gate_id -> position
        self.gate_connections: Dict[int, int] = {}  # gate_id -> destination_gate_id
        self.system_jumps: Dict[int, List[int]] = {}  # system_id -> list of connected systems
        self.system_stations: Dict[int, List[Station]] = {}  # system_id -> stations

    def load_all(self):
        """Load all SDE data."""
        self._load_systems()
        self._load_stations()
        self._load_stargates()
        self._load_jumps()

    def _load_systems(self):
        """Load solar systems."""
        path = os.path.join(self.sde_dir, "mapSolarSystems.csv.bz2")
        with bz2.open(path, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                system = System(
                    id=int(row['solarSystemID']),
                    name=row['solarSystemName'],
                    security=float(row['security']),
                    x=float(row['x']),
                    y=float(row['y']),
                    z=float(row['z'])
                )
                self.systems[system.id] = system
                self.system_names[system.name.lower()] = system.id

    def _load_stations(self):
        """Load stations."""
        path = os.path.join(self.sde_dir, "staStations.csv.bz2")
        with bz2.open(path, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                station = Station(
                    id=int(row['stationID']),
                    name=row['stationName'],
                    system_id=int(row['solarSystemID']),
                    x=float(row['x']),
                    y=float(row['y']),
                    z=float(row['z'])
                )
                self.stations[station.id] = station
                self.station_names[station.name.lower()] = station.id

                self.system_stations.setdefault(station.system_id, []).append(station)

    def _load_stargates(self):
        """Load stargates from denormalize."""
        path = os.path.join(self.sde_dir, "mapDenormalize.csv.bz2")
        with bz2.open(path, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                group_id = int(row['groupID']) if row['groupID'] else 0
                if group_id != 10:  # Stargate group
                    continue

                system_id = int(row['solarSystemID']) if row['solarSystemID'] else None
                if system_id is None:
                    continue

                gate_id = int(row['itemID'])
                gate = Stargate(
                    id=gate_id,
                    system_id=system_id,
                    name=row['itemName'],
                    x=float(row['x']),
                    y=float(row['y']),
                    z=float(row['z'])
                )

                self.stargates_by_system.setdefault(system_id, []).append(gate)
                self.gate_positions[gate_id] = (gate.x, gate.y, gate.z)

    def _load_jumps(self):
        """Load system jumps and map gate connections."""
        path = os.path.join(self.sde_dir, "mapSolarSystemJumps.csv.bz2")
        with bz2.open(path, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                from_sys = int(row['fromSolarSystemID'])
                to_sys = int(row['toSolarSystemID'])

                if from_sys not in self.system_jumps:
                    self.system_jumps[from_sys] = []
                if to_sys not in self.system_jumps[from_sys]:
                    self.system_jumps[from_sys].append(to_sys)

                # Map gate connections based on system pairs
                from_gates = self.stargates_by_system.get(from_sys, [])
                to_gates = self.stargates_by_system.get(to_sys, [])

                from_system_name = self.systems[from_sys].name
                to_system_name = self.systems[to_sys].name

                for gate in from_gates:
                    # Extract destination name from gate name
                    gate_dest = gate.name.replace("Stargate (", "").replace(")", "")
                    if gate_dest != to_system_name:
                        continue

                    # Find matching destination gate
                    for dest_gate in to_gates:
                        dest_name = dest_gate.name.replace("Stargate (", "").replace(")", "")
                        if dest_name == from_system_name:
                            self.gate_connections[gate.id] = dest_gate.id
                            self.gate_connections[dest_gate.id] = gate.id
                            break
                    break

    def get_system_by_name(self, name: str) -> Optional[System]:
        """Get system by name (case insensitive)."""
        sys_id = self.system_names.get(name.lower())
        return self.systems.get(sys_id) if sys_id else None

    def get_station_by_name(self, name: str) -> Optional[Station]:
        """Get station by name (case insensitive)."""
        sta_id = self.station_names.get(name.lower())
        return self.stations.get(sta_id) if sta_id else None

    def resolve_location(self, name: str) -> Tuple[Optional[System], Optional[Station]]:
        """Resolve a location name to system and optionally station."""
        # Try station first (more specific)
        station = self.get_station_by_name(name)
        if station:
            system = self.systems[station.system_id]
            return system, station

        # Try system
        system = self.get_system_by_name(name)
        return system, None

    def get_gates_for_system(self, system_id: int) -> List[Stargate]:
        """Get all stargates in a system."""
        return self.stargates_by_system.get(system_id, [])

    def get_connected_systems(self, system_id: int) -> List[int]:
        """Get all systems connected via gates."""
        return self.system_jumps.get(system_id, [])

    def get_gate_for_destination(self, from_sys: int, to_sys: int) -> Optional[Stargate]:
        """Get the stargate in from_sys that leads to to_sys."""
        gates = self.stargates_by_system.get(from_sys, [])
        to_system_name = self.systems[to_sys].name

        for gate in gates:
            gate_dest = gate.name.replace("Stargate (", "").replace(")", "")
            if gate_dest == to_system_name:
                return gate
        return None

    def get_gate_destination_system(self, gate_id: int) -> Optional[int]:
        """Get the system ID that a gate leads to."""
        dest_gate_id = self.gate_connections.get(gate_id)
        if dest_gate_id is None:
            return None

        # Find which system the destination gate is in
        for sys_id, gates in self.stargates_by_system.items():
            for gate in gates:
                if gate.id == dest_gate_id:
                    return sys_id
        return None


def calculate_distance(x1: float, y1: float, z1: float,
                       x2: float, y2: float, z2: float) -> float:
    """Calculate Euclidean distance in meters."""
    return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2 + (z2 - z1) ** 2)


def calculate_warp_time_simple(distance_m: float, params: TravelParams) -> float:
    """Calculate warp time following the specification."""
    if distance_m <= 0:
        return 0.0

    v_w = params.warp_speed  # AU/s
    v_s = params.top_speed  # m/s
    v_drop = min(v_s / 2.0, 100.0)  # m/s

    k_a = v_w  # AU/s
    k_d = min(v_w / 3.0, 2.0)  # AU/s

    # Initial max warp speed in m/s
    v_warp_ms = v_w * AU_IN_M

    # Distances
    d_a = AU_IN_M  # 1 AU in meters
    d_d = v_warp_ms / k_d  # deceleration distance
    d_min = d_a + d_d

    # Check if we reach full warp
    t_cruise = 0.0
    if distance_m < d_min:
        # Reduced peak warp speed
        v_warp_ms = (distance_m * k_a * k_d) / (k_a + k_d)
    else:
        # Full warp speed, cruise phase exists
        t_cruise = (distance_m - d_a - d_d) / v_warp_ms

    # Acceleration time
    v_warp_au = v_warp_ms / AU_IN_M
    t_accel = (1.0 / k_a) * math.log(v_warp_au / k_a) if v_warp_au > k_a else 0.0

    # Deceleration time
    t_decel = (1.0 / k_d) * math.log(v_warp_ms / v_drop) if v_warp_ms > v_drop else 0.0

    return t_accel + t_cruise + t_decel


@dataclass(order=True)
class PQItem:
    """Priority queue item."""
    time: float
    data: Tuple = field(compare=False)  # (state_tuple, path, total_time)


class RoutePlanner:
    """Plans optimal routes through New Eden."""

    def __init__(self, sde: SDELoader, params: TravelParams, is_freighter: bool = False):
        self.sde = sde
        self.params = params
        self.is_freighter = is_freighter

    def plan(self, start_name: str, end_name: str) -> Tuple[float, List[str]]:
        """
        Plan route from start to end.

        Returns (total_time, route_systems) where route_systems is a list of system names.
        """
        # Resolve start and end locations
        start_sys, start_sta = self.sde.resolve_location(start_name)
        end_sys, end_sta = self.sde.resolve_location(end_name)

        if start_sys is None:
            raise ValueError(f"Could not resolve start location: {start_name}")
        if end_sys is None:
            raise ValueError(f"Could not resolve end location: {end_name}")

        # Special case: same system
        if start_sys.id == end_sys.id:
            total_time = self._same_system_time(start_sys, start_sta, end_sta)
            return total_time, [start_sys.name]

        # General pathfinding
        return self._find_path(start_sys, start_sta, end_sys, end_sta)

    def _same_system_time(self, system: System, start_sta: Optional[Station],
                          end_sta: Optional[Station]) -> float:
        """Calculate time for travel within same system."""
        gates = self.sde.get_gates_for_system(system.id)
        gates_sorted = sorted(gates, key=lambda g: g.name)
        start_gate = gates_sorted[0] if gates_sorted else None

        if start_sta and end_sta:
            # Station to station
            distance = calculate_distance(start_sta.x, start_sta.y, start_sta.z,
                                          end_sta.x, end_sta.y, end_sta.z)
            warp_time = calculate_warp_time_simple(distance, self.params)
            return self.params.dock_time + self.params.align_time + warp_time + self.params.dock_time

        elif start_sta and not end_sta:
            # Station to system (exit)
            if start_gate:
                distance = calculate_distance(start_sta.x, start_sta.y, start_sta.z,
                                              start_gate.x, start_gate.y, start_gate.z)
                warp_time = calculate_warp_time_simple(distance, self.params)
                return self.params.dock_time + self.params.align_time + warp_time + self.params.gate_time
            return self.params.dock_time

        elif not start_sta and end_sta:
            # System entry to station
            if start_gate:
                distance = calculate_distance(start_gate.x, start_gate.y, start_gate.z,
                                              end_sta.x, end_sta.y, end_sta.z)
                warp_time = calculate_warp_time_simple(distance, self.params)
                return self.params.align_time + warp_time + self.params.dock_time
            return self.params.dock_time

        else:
            # System to system (same system) - no time
            return 0.0

    def _find_path(self, start_sys: System, start_sta: Optional[Station],
                   end_sys: System, end_sta: Optional[Station]) -> Tuple[float, List[str]]:
        """Find optimal path using Dijkstra's algorithm with Zarzakh constraints."""

        # For freighters, we need special handling - first try high-sec only
        if self.is_freighter:
            return self._find_path_freighter_safe(start_sys, start_sta, end_sys, end_sta)

        # Priority queue: (time, state_tuple, path)
        # state_tuple: (system_id, pos_type, pos_id, zarzakh_lock)
        pq: List[PQItem] = []

        # Track best time to reach each state
        best: Dict[Tuple, float] = {}

        # Initialize based on start type
        if start_sta:
            # Starting docked at station - first need to undock
            initial_state = (start_sys.id, 'station', start_sta.id, None)
            initial_time = 0.0
            # After undock, we're in space at station position
            initial_path = [(start_sys.id, 'station', start_sta.id, 0.0)]
        else:
            # Starting in space at first lexicographic gate
            gates = self.sde.get_gates_for_system(start_sys.id)
            gates_sorted = sorted(gates, key=lambda g: g.name)
            if gates_sorted:
                start_gate = gates_sorted[0]
                initial_state = (start_sys.id, 'gate', start_gate.id, None)
            else:
                initial_state = (start_sys.id, 'in_space', 0, None)
            initial_time = 0.0
            initial_path = [(start_sys.id, 'gate', gates_sorted[0].id if gates_sorted else 0, 0.0)]

        heapq.heappush(pq, PQItem(initial_time, (initial_state, initial_path)))
        best[initial_state] = initial_time

        while pq:
            item = heapq.heappop(pq)
            current_time = item.time
            state_tuple, path = item.data
            sys_id, pos_type, pos_id, zarzakh_lock = state_tuple

            # Check if we've reached destination
            if sys_id == end_sys.id:
                # Calculate remaining time to reach end position
                remaining_time = self._time_to_end(sys_id, pos_type, pos_id, end_sta, zarzakh_lock)
                total_time = current_time + remaining_time

                # Build route from path
                route = self._build_route(path, start_sys, start_sta, end_sys, end_sta)
                return total_time, route

            state_key = state_tuple
            if current_time > best.get(state_key, float('inf')):
                continue

            # Expand neighbors
            neighbors = self._get_neighbors(sys_id, pos_type, pos_id, zarzakh_lock)

            for next_state, action_time in neighbors:
                next_key = next_state
                new_time = current_time + action_time

                if next_key not in best or new_time < best[next_key]:
                    best[next_key] = new_time
                    new_path = path + [(next_state[0], next_state[1], next_state[2], action_time)]
                    heapq.heappush(pq, PQItem(new_time, (next_state, new_path)))

        raise ValueError("No route found")

    def _time_to_end(self, sys_id: int, pos_type: str, pos_id: int,
                     end_sta: Optional[Station], zarzakh_lock: Optional[int]) -> float:
        """Calculate remaining time from current position to end."""
        # Get current position
        if pos_type == 'station':
            station = self.sde.stations[pos_id]
            pos = (station.x, station.y, station.z)
        elif pos_type == 'gate':
            pos = self.sde.gate_positions.get(pos_id, (0, 0, 0))
        else:
            pos = (0, 0, 0)

        if end_sta:
            # Need to dock at station
            distance = calculate_distance(pos[0], pos[1], pos[2],
                                          end_sta.x, end_sta.y, end_sta.z)
            warp_time = calculate_warp_time_simple(distance, self.params)

            if pos_type == 'station':
                # Undock, align, warp, dock
                return self.params.dock_time + self.params.align_time + warp_time + self.params.dock_time
            else:
                # Align, warp, dock
                return self.params.align_time + warp_time + self.params.dock_time
        else:
            # End is system - already there
            return 0.0

    def _get_neighbors(self, sys_id: int, pos_type: str, pos_id: int,
                       zarzakh_lock: Optional[int]) -> List[Tuple[Tuple, float]]:
        """Get neighboring states from current position."""
        neighbors = []

        system = self.sde.systems[sys_id]
        gates = self.sde.get_gates_for_system(sys_id)
        stations = self.sde.system_stations.get(sys_id, [])

        # Get current position
        if pos_type == 'station':
            station = self.sde.stations[pos_id]
            pos = (station.x, station.y, station.z)
        elif pos_type == 'gate':
            pos = self.sde.gate_positions.get(pos_id, (0, 0, 0))
        else:
            pos = (0, 0, 0)

        # If at station, can undock and warp to gates
        if pos_type == 'station':
            for gate in gates:
                distance = calculate_distance(pos[0], pos[1], pos[2],
                                              gate.x, gate.y, gate.z)
                warp_time = calculate_warp_time_simple(distance, self.params)
                # Undock + align + warp
                time_cost = self.params.dock_time + self.params.align_time + warp_time

                next_state = (sys_id, 'gate', gate.id, zarzakh_lock)
                neighbors.append((next_state, time_cost))

        # If at gate, can jump through or warp to other gates
        if pos_type == 'gate':
            current_gate_id = pos_id

            # Jump through gate
            dest_sys_id = self.sde.get_gate_destination_system(current_gate_id)
            if dest_sys_id:
                dest_gate_id = self.sde.gate_connections.get(current_gate_id)

                # Check if freighter can enter this system
                if self.is_freighter and dest_sys_id in self.sde.systems:
                    dest_system = self.sde.systems[dest_sys_id]
                    if dest_system.security < 0.45:  # Low-sec or null
                        # Skip this neighbor for freighters
                        pass
                    else:
                        # Process normally
                        can_jump, new_lock = self._check_zarzakh(sys_id, dest_sys_id, current_gate_id, zarzakh_lock)
                        if can_jump and dest_gate_id:
                            next_state = (dest_sys_id, 'gate', dest_gate_id, new_lock)
                            neighbors.append((next_state, self.params.gate_time))
                else:
                    # Check Zarzakh constraints
                    can_jump, new_lock = self._check_zarzakh(sys_id, dest_sys_id, current_gate_id, zarzakh_lock)
                    if can_jump and dest_gate_id:
                        next_state = (dest_sys_id, 'gate', dest_gate_id, new_lock)
                        neighbors.append((next_state, self.params.gate_time))

            # Warp to other gates in same system
            for gate in gates:
                if gate.id == current_gate_id:
                    continue
                distance = calculate_distance(pos[0], pos[1], pos[2],
                                              gate.x, gate.y, gate.z)
                warp_time = calculate_warp_time_simple(distance, self.params)
                time_cost = self.params.align_time + warp_time

                next_state = (sys_id, 'gate', gate.id, zarzakh_lock)
                neighbors.append((next_state, time_cost))

        return neighbors

    def _check_zarzakh(self, sys_id: int, dest_sys_id: int, current_gate_id: int,
                       zarzakh_lock: Optional[int]) -> Tuple[bool, Optional[int]]:
        """Check Zarzakh constraints and return (can_jump, new_lock)."""
        can_jump = True
        new_lock = zarzakh_lock

        # Entering Zarzakh
        if dest_sys_id == ZARZAKH_SYSTEM_ID:
            new_lock = current_gate_id

        # Leaving Zarzakh
        if sys_id == ZARZAKH_SYSTEM_ID:
            if zarzakh_lock is not None and current_gate_id != zarzakh_lock:
                can_jump = False
            else:
                new_lock = None

        return can_jump, new_lock

    def _build_route(self, path: List[Tuple], start_sys: System,
                     start_sta: Optional[Station], end_sys: System,
                     end_sta: Optional[Station]) -> List[str]:
        """Build list of system names from path."""
        route = []
        prev_sys = None

        for state in path:
            sys_id = state[0]
            if sys_id != prev_sys:
                system = self.sde.systems[sys_id]
                route.append(system.name)
                prev_sys = sys_id

        # Ensure end system is included
        if not route or route[-1] != end_sys.name:
            route.append(end_sys.name)

        return route

    def _find_path_freighter_safe(self, start_sys: System, start_sta: Optional[Station],
                                   end_sys: System, end_sta: Optional[Station]) -> Tuple[float, List[str]]:
        """Find path that only goes through high-sec (>= 0.45 security)."""
        # Use BFS to find shortest high-sec only path
        # If no high-sec only path exists, find path with minimum low-sec/null

        # First try: high-sec only
        result = self._bfs_highsec_only(start_sys, end_sys)
        if result:
            route = result
            total_time = self._calculate_route_time(route, start_sta, end_sta)
            return total_time, route

        # Second try: allow traversing low-sec if it's the only route
        # This is the "exception" for freighters
        result = self._bfs_with_lowsec_exception(start_sys, end_sys)
        if result:
            route = result
            total_time = self._calculate_route_time(route, start_sta, end_sta)
            return total_time, route

        raise ValueError("No safe route found for freighter")

    def _bfs_highsec_only(self, start_sys: System, end_sys: System) -> Optional[List[str]]:
        """BFS to find shortest high-sec only route."""
        visited = {start_sys.id}
        queue = deque([(start_sys.id, [start_sys.name])])

        while queue:
            sys_id, path = queue.popleft()

            if sys_id == end_sys.id:
                return path

            for neighbor_id in self.sde.get_connected_systems(sys_id):
                if neighbor_id in visited:
                    continue

                neighbor_sys = self.sde.systems.get(neighbor_id)
                if neighbor_sys and neighbor_sys.security >= 0.45:
                    visited.add(neighbor_id)
                    queue.append((neighbor_id, path + [neighbor_sys.name]))

        return None

    def _bfs_with_lowsec_exception(self, start_sys: System, end_sys: System) -> Optional[List[str]]:
        """BFS that allows low-sec systems as a last resort."""
        visited = {start_sys.id}
        queue = deque([(start_sys.id, [start_sys.name])])

        while queue:
            sys_id, path = queue.popleft()

            if sys_id == end_sys.id:
                return path

            for neighbor_id in self.sde.get_connected_systems(sys_id):
                if neighbor_id in visited:
                    continue

                neighbor_sys = self.sde.systems.get(neighbor_id)
                if neighbor_sys:
                    visited.add(neighbor_id)
                    queue.append((neighbor_id, path + [neighbor_sys.name]))

        return None

    def _calculate_route_time(self, route: List[str], start_sta: Optional[Station],
                              end_sta: Optional[Station]) -> float:
        """Calculate total time for a route."""
        if len(route) <= 1:
            return 0.0

        total_time = 0.0

        # Start docked? Need to undock
        if start_sta:
            total_time += self.params.dock_time

        # For each system hop
        for i in range(len(route) - 1):
            from_sys = self.sde.get_system_by_name(route[i])
            to_sys = self.sde.get_system_by_name(route[i + 1])

            if from_sys and to_sys:
                # Get gate in from_sys leading to to_sys
                gate = self.sde.get_gate_for_destination(from_sys.id, to_sys.id)

                if i == 0 and start_sta and gate:
                    # Warp from station to gate
                    distance = calculate_distance(start_sta.x, start_sta.y, start_sta.z,
                                                  gate.x, gate.y, gate.z)
                    warp_time = calculate_warp_time_simple(distance, self.params)
                    total_time += self.params.align_time + warp_time
                elif gate:
                    # Align + warp to gate (from previous gate)
                    # Estimate: assume 50 AU average warp between gates
                    distance = 50 * AU_IN_M
                    warp_time = calculate_warp_time_simple(distance, self.params)
                    total_time += self.params.align_time + warp_time

                # Jump through gate
                total_time += self.params.gate_time

        # Final warp to station
        if end_sta:
            # Get last gate
            last_sys = self.sde.get_system_by_name(route[-1])
            if last_sys:
                gates = self.sde.get_gates_for_system(last_sys.id)
                gates_sorted = sorted(gates, key=lambda g: g.name)
                if gates_sorted:
                    last_gate = gates_sorted[0]
                    distance = calculate_distance(last_gate.x, last_gate.y, last_gate.z,
                                                  end_sta.x, end_sta.y, end_sta.z)
                    warp_time = calculate_warp_time_simple(distance, self.params)
                    total_time += self.params.align_time + warp_time
                    total_time += self.params.dock_time

        return total_time


def format_time(seconds: float) -> str:
    """Format time in HH:MM format, rounded up to nearest minute."""
    total_minutes = math.ceil(seconds / 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours:02d}:{minutes:02d}"


def format_cargo(amount: float) -> str:
    """Format cargo amount with comma-separated thousands."""
    return f"{amount:,.2f} m3"


def load_config(config_path: str) -> Config:
    """Load and validate configuration file."""
    with open(config_path, 'r') as f:
        data = yaml.safe_load(f)

    ships = {}
    for name, ship_data in data.get('ships', {}).items():
        # Default EHP based on ship type
        ehp = ship_data.get('ehp')
        if ehp is None:
            ship_type = ship_data['type']
            if ship_type == 'Deep Space Transport':
                ehp = 60000
            elif ship_type == 'Freighter':
                ehp = 300000
            else:
                ehp = None  # Blockade runners don't care about EHP

        ships[name] = ShipConfig(
            name=name,
            type=ship_data['type'],
            align_time=float(ship_data['align']),
            top_speed=float(ship_data['top_speed']),
            warp_speed=float(ship_data['warp_speed']),
            cargo_size=int(ship_data['cargo_size']),
            ehp=ehp
        )

    times_data = data.get('times', {})
    times = TimesConfig(
        dock=float(times_data['dock']),
        gate=float(times_data['gate']),
        move_cargo=float(times_data['move_cargo'])
    )

    min_isk_per_jump = data.get('min_isk_per_jump')
    if min_isk_per_jump is not None:
        min_isk_per_jump = float(min_isk_per_jump)

    max_isk_per_ehp = data.get('max_isk_per_ehp')
    if max_isk_per_ehp is not None:
        max_isk_per_ehp = float(max_isk_per_ehp)

    return Config(ships=ships, times=times, min_isk_per_jump=min_isk_per_jump, max_isk_per_ehp=max_isk_per_ehp)


def load_manifest(manifest_path: Optional[str]) -> Manifest:
    """Load and validate manifest file."""
    if manifest_path is None:
        return Manifest(start_cargo=None, waypoints=[])

    with open(manifest_path, 'r') as f:
        data = yaml.safe_load(f)

    start_cargo = data.get('start_cargo')
    if start_cargo is not None:
        start_cargo = float(start_cargo)

    waypoints = []
    for wp in data.get('waypoints', []):
        cargo = wp.get('cargo')
        if cargo is not None:
            cargo = float(cargo)
        waypoints.append(Waypoint(name=wp['name'], cargo=cargo))

    return Manifest(start_cargo=start_cargo, waypoints=waypoints)


def load_contracts(contracts_path: str) -> List[Contract]:
    """Load contracts from JSONL file."""
    contracts = []
    with open(contracts_path, 'r') as f:
        for idx, line in enumerate(f):
            if line.strip():
                data = json.loads(line)
                contract = Contract(
                    id=idx + 1,  # 1-indexed
                    start=data['start'],
                    end=data['end'],
                    collateral=float(data['collateral']),
                    m3=float(data['m3']),
                    actual_value=float(data['actual_value']),
                    reward=float(data['reward']),
                    issuer=data['issuer']
                )
                contracts.append(contract)
    return contracts


def output_manifest_plan(sde: SDELoader, config: Config, ship_name: str,
                         start: str, end: str, manifest: Manifest):
    """Output the formatted manifest travel plan."""
    ship = config.ships.get(ship_name)
    params = TravelParams(
        align_time=math.ceil(ship.align_time),
        top_speed=ship.top_speed,
        warp_speed=ship.warp_speed,
        dock_time=config.times.dock,
        gate_time=config.times.gate
    )

    is_freighter = ship.type == "Freighter"
    planner = RoutePlanner(sde, params, is_freighter)

    # Resolve locations
    start_sys, start_sta = sde.resolve_location(start)
    end_sys, end_sta = sde.resolve_location(end)

    # Build list of cargo pickups (location, amount) in order they appear
    # start_cargo first, then waypoints in order
    cargo_pickups = []
    if manifest.start_cargo and manifest.start_cargo > 0:
        cargo_pickups.append((start, manifest.start_cargo))
    for wp in manifest.waypoints:
        if wp.cargo and wp.cargo > 0:
            cargo_pickups.append((wp.name, wp.cargo))

    # If no cargo, just do a simple trip
    if not cargo_pickups:
        print("START: {}".format(start))
        if start_sta:
            print("UNDOCK")
        _, route = planner.plan(start, end)
        route_parts = []
        for sys_name in route:
            sys_obj = sde.get_system_by_name(sys_name)
            if sys_obj:
                route_parts.append("{} ({:.1f})".format(sys_obj.name, sys_obj.security))
        if len(route) > 1:
            print("GO: " + " -> ".join(route_parts))
        if end_sta:
            print("DOCK: {}".format(end))
        total_time = 0.0  # Simplified calculation
        print("DONE: {}".format(format_time(total_time)))
        return

    # Track remaining cargo at each location
    cargo_remaining = [(loc, amt) for loc, amt in cargo_pickups]

    total_cargo_moved = 0.0
    total_time = 0.0
    trip_num = 1
    current_loc = start
    capacity = ship.cargo_size

    while any(amt > 0 for _, amt in cargo_remaining):
        if trip_num > 1:
            print("[--- TRIP {} ---]".format(trip_num))

        trip_time = 0.0
        cargo_this_trip = 0.0

        # Determine pickups for this trip
        # We need to fill cargo hold optimally
        # Strategy: Follow manifest order, fill as much as possible per location
        trip_pickups = []
        hold = 0.0

        for loc, amt in cargo_remaining:
            if amt > 0 and hold < capacity:
                take = min(amt, capacity - hold)
                trip_pickups.append((loc, take))
                hold += take

        # Now execute this trip
        # First pickup location
        first_pickup_loc = trip_pickups[0][0]
        first_pickup_amt = trip_pickups[0][1]

        # Are we at the first pickup location?
        if first_pickup_loc.lower() == current_loc.lower():
            # We're at the pickup location
            if trip_num == 1:
                print("START: {}".format(current_loc))
            else:
                # Need to dock first after returning
                curr_sys, curr_sta = sde.resolve_location(current_loc)
                if curr_sta:
                    print("DOCK: {}".format(current_loc))
                    trip_time += config.times.dock

            # Load cargo
            print("LOAD: {}".format(format_cargo(first_pickup_amt)))
            trip_time += config.times.move_cargo

            # Update cargo remaining
            for i, (loc, amt) in enumerate(cargo_remaining):
                if loc.lower() == current_loc.lower():
                    cargo_remaining[i] = (loc, amt - first_pickup_amt)
                    break

            cargo_this_trip += first_pickup_amt

            # Undock if at station
            curr_sys, curr_sta = sde.resolve_location(current_loc)
            if curr_sta:
                print("UNDOCK")
                trip_time += config.times.dock

            trip_pickups = trip_pickups[1:]  # Remove first pickup
        else:
            # Need to travel to first pickup
            if trip_num == 1:
                print("START: {}".format(current_loc))
                curr_sys, curr_sta = sde.resolve_location(current_loc)
                if curr_sta:
                    print("UNDOCK")
                    trip_time += config.times.dock

        # Process remaining pickups
        prev_loc = current_loc
        for loc, amt in trip_pickups:
            # Travel to pickup location
            _, route = planner.plan(prev_loc, loc)
            route_parts = []
            for sys_name in route:
                sys_obj = sde.get_system_by_name(sys_name)
                if sys_obj:
                    route_parts.append("{} ({:.1f})".format(sys_obj.name, sys_obj.security))
            print("GO: " + " -> ".join(route_parts))

            # Calculate travel time (simplified)
            trip_time += (len(route) - 1) * (params.align_time + params.gate_time)

            # Dock at pickup
            pick_sys, pick_sta = sde.resolve_location(loc)
            if pick_sta:
                print("DOCK: {}".format(loc))
                trip_time += config.times.dock

            # Load cargo
            print("LOAD: {}".format(format_cargo(amt)))
            trip_time += config.times.move_cargo

            # Update cargo remaining
            for i, (c_loc, c_amt) in enumerate(cargo_remaining):
                if c_loc.lower() == loc.lower():
                    cargo_remaining[i] = (c_loc, c_amt - amt)
                    break

            cargo_this_trip += amt

            # Undock
            if pick_sta:
                print("UNDOCK")
                trip_time += config.times.dock

            prev_loc = loc

        # Travel to end
        _, route = planner.plan(prev_loc, end)
        route_parts = []
        for sys_name in route:
            sys_obj = sde.get_system_by_name(sys_name)
            if sys_obj:
                route_parts.append("{} ({:.1f})".format(sys_obj.name, sys_obj.security))
        print("GO: " + " -> ".join(route_parts))
        trip_time += (len(route) - 1) * (params.align_time + params.gate_time)

        # Dock at end
        if end_sta:
            print("DOCK: {}".format(end))
            trip_time += config.times.dock

        # Unload
        print("UNLOAD: {}".format(format_cargo(cargo_this_trip)))
        trip_time += config.times.move_cargo

        total_time += trip_time
        total_cargo_moved += cargo_this_trip
        current_loc = end
        trip_num += 1

    print("DONE: {}".format(format_time(total_time)))
    if total_cargo_moved > 0:
        print("MOVED: {}".format(format_cargo(total_cargo_moved)))


def select_best_ship(contracts: List[Contract], config: Config, sde: SDELoader,
                      start_system: str, max_isk_per_ehp: Optional[float]) -> Optional[ShipConfig]:
    """
    Select the best ship to minimize travel time.
    Tiebreaker: EHP then Name.

    For each ship, check if it can carry all contract combinations.
    A ship is viable if it can carry the total volume of any set of contracts we might pick.
    """
    if not contracts:
        # Pick the ship with lowest travel time (fastest), tiebreak by EHP then name
        viable_ships = list(config.ships.values())
        viable_ships.sort(key=lambda s: (s.align_time, -(s.ehp or 0), s.name))
        return viable_ships[0] if viable_ships else None

    # Find the maximum total volume of any subset of contracts we might haul
    # We need to consider all subsets that fit in cargo
    max_volume = max(c.m3 for c in contracts) if contracts else 0

    # For each ship, check if it can carry at least the largest single contract
    viable_ships = []
    for ship in config.ships.values():
        if ship.cargo_size >= max_volume:
            # Check max_isk_per_ehp constraint (doesn't apply to blockade runners)
            if max_isk_per_ehp is not None and ship.type != 'Blockade Runner':
                # Will need to check at contract selection time
                viable_ships.append(ship)
            else:
                viable_ships.append(ship)

    if not viable_ships:
        return None

    # Sort by: fastest align_time (minimizes travel time), then EHP (higher is better), then name
    # For freighters, they may take longer routes (high-sec only), but align time is still key
    viable_ships.sort(key=lambda s: (s.align_time, -(s.ehp or 0), s.name))

    return viable_ships[0]


def calculate_route_jumps(sde: SDELoader, start: str, end: str, is_freighter: bool) -> int:
    """Calculate number of jumps between two locations."""
    start_sys, start_sta = sde.resolve_location(start)
    end_sys, end_sta = sde.resolve_location(end)

    if start_sys is None or end_sys is None:
        return float('inf')

    if start_sys.id == end_sys.id:
        return 0

    # BFS for shortest path
    if is_freighter:
        # High-sec only
        visited = {start_sys.id}
        queue = deque([(start_sys.id, 0)])
        while queue:
            sys_id, jumps = queue.popleft()
            if sys_id == end_sys.id:
                return jumps
            for neighbor_id in sde.get_connected_systems(sys_id):
                if neighbor_id in visited:
                    continue
                neighbor_sys = sde.systems.get(neighbor_id)
                if neighbor_sys and neighbor_sys.security >= 0.45:
                    visited.add(neighbor_id)
                    queue.append((neighbor_id, jumps + 1))
        # Fallback: allow low-sec
        visited = {start_sys.id}
        queue = deque([(start_sys.id, 0)])
        while queue:
            sys_id, jumps = queue.popleft()
            if sys_id == end_sys.id:
                return jumps
            for neighbor_id in sde.get_connected_systems(sys_id):
                if neighbor_id in visited:
                    continue
                neighbor_sys = sde.systems.get(neighbor_id)
                if neighbor_sys:
                    visited.add(neighbor_id)
                    queue.append((neighbor_id, jumps + 1))
        return float('inf')
    else:
        # Normal BFS
        visited = {start_sys.id}
        queue = deque([(start_sys.id, 0)])
        while queue:
            sys_id, jumps = queue.popleft()
            if sys_id == end_sys.id:
                return jumps
            for neighbor_id in sde.get_connected_systems(sys_id):
                if neighbor_id in visited:
                    continue
                neighbor_sys = sde.systems.get(neighbor_id)
                if neighbor_sys:
                    visited.add(neighbor_id)
                    queue.append((neighbor_id, jumps + 1))
        return float('inf')


def evaluate_contract_combination(contracts: List[Contract], sde: SDELoader,
                                   start_system: str, ship: ShipConfig,
                                   config: Config, is_freighter: bool,
                                   min_isk_per_jump: Optional[float],
                                   max_isk_per_ehp: Optional[float],
                                   max_time_minutes: Optional[int]) -> Tuple[float, int, List[Tuple[Contract, int, int]]]:
    """Evaluate contracts using nearest-neighbor route planning."""
    if not contracts:
        return 0.0, 0, []
    current_loc = start_system
    total_jumps = 0
    carrying: List[Contract] = []
    total_volume = 0.0
    contract_details: List[Tuple[Contract, int, int]] = []  # (contract, jumps_holding, jumps_to_pickup)

    # Track remaining pickups and deliveries
    remaining_pickups = set(c.id for c in contracts)
    remaining_deliveries = set(c.id for c in contracts)
    contracts_by_id = {c.id: c for c in contracts}

    # Track jumps while holding each contract
    jumps_while_holding = {c.id: 0 for c in contracts}

    while remaining_pickups or remaining_deliveries:
        # Find nearest location (pickup or delivery)
        best_loc = None
        best_dist = float('inf')
        is_pickup = True
        best_contract_id = None

        # Check pickups (can only pick up if we have space)
        for cid in remaining_pickups:
            c = contracts_by_id[cid]
            if total_volume + c.m3 <= ship.cargo_size:
                dist = calculate_route_jumps(sde, current_loc, c.start, is_freighter)
                if dist < best_dist:
                    best_dist = dist
                    best_loc = c.start
                    is_pickup = True
                    best_contract_id = cid

        # Check deliveries (can only deliver if we're carrying it)
        for cid in remaining_deliveries:
            if cid not in [c.id for c in carrying]:
                continue
            c = contracts_by_id[cid]
            dist = calculate_route_jumps(sde, current_loc, c.end, is_freighter)
            if dist < best_dist:
                best_dist = dist
                best_loc = c.end
                is_pickup = False
                best_contract_id = cid

        if best_loc is None:
            # Can't pick up more (full), must deliver
            if carrying:
                # Find nearest delivery
                for c in carrying:
                    if c.id in remaining_deliveries:
                        dist = calculate_route_jumps(sde, current_loc, c.end, is_freighter)
                        if dist < best_dist:
                            best_dist = dist
                            best_loc = c.end
                            is_pickup = False
                            best_contract_id = c.id

        if best_loc is None:
            break

        # Travel to best location
        total_jumps += best_dist

        # Update jumps while holding for all carrying contracts
        for c in carrying:
            jumps_while_holding[c.id] += best_dist

        if is_pickup:
            # Pick up contract
            c = contracts_by_id[best_contract_id]
            carrying.append(c)
            total_volume += c.m3
            remaining_pickups.remove(best_contract_id)
        else:
            # Deliver contract
            c = contracts_by_id[best_contract_id]
            carrying = [cc for cc in carrying if cc.id != best_contract_id]
            total_volume -= c.m3
            remaining_deliveries.remove(best_contract_id)
            # Record the jumps while holding this contract
            # Note: we need to track the jumps to pickup as well
            # For now, estimate it

        current_loc = best_loc

    # Return to start
    return_jumps = calculate_route_jumps(sde, current_loc, start_system, is_freighter)
    total_jumps += return_jumps

    # Update jumps for remaining carrying (shouldn't be any, but just in case)
    for c in carrying:
        jumps_while_holding[c.id] += return_jumps

    # Calculate profit
    total_profit = sum(c.reward for c in contracts)

    # Estimate time (simplified: jumps * align + gate time, plus dock times)
    # Each location visit involves dock/undock
    num_docks = len(contracts) * 2 + 2  # pickup and delivery for each, plus start/end
    time_seconds = total_jumps * (ship.align_time + config.times.gate)
    time_seconds += num_docks * config.times.dock
    time_seconds += len(contracts) * 2 * config.times.move_cargo

    time_minutes = time_seconds / 60.0

    # Check max_time constraint
    if max_time_minutes is not None:
        if time_minutes > max_time_minutes:
            return -float('inf'), float('inf'), []

    # Calculate ISK per jump (total jumps including return)
    isk_per_jump = total_profit / total_jumps if total_jumps > 0 else float('inf')

    # Check min_isk_per_jump
    if min_isk_per_jump is not None:
        if isk_per_jump < min_isk_per_jump:
            return -float('inf'), float('inf'), []

    # Build contract details
    for c in contracts:
        contract_details.append((c, jumps_while_holding[c.id], 0))  # jumps_to_pickup not tracked precisely

    return total_profit, time_minutes, contract_details


def find_optimal_contracts(all_contracts: List[Contract], sde: SDELoader,
                            start_system: str, ship: ShipConfig,
                            config: Config, is_freighter: bool,
                            max_time_minutes: Optional[int]) -> Tuple[List[Contract], float, int, List[Tuple[Contract, int, int]]]:
    """
    Find the optimal set of contracts to haul.
    Returns: (selected_contracts, profit, time_minutes, contract_details)
    """
    if not all_contracts:
        return [], 0.0, 0, []

    min_isk_per_jump = config.min_isk_per_jump
    max_isk_per_ehp = config.max_isk_per_ehp

    # Filter contracts that can fit in the ship
    viable_contracts = [c for c in all_contracts if c.m3 <= ship.cargo_size]

    # Filter by max_isk_per_ehp if applicable
    if max_isk_per_ehp is not None and ship.type != 'Blockade Runner' and ship.ehp:
        max_value = max_isk_per_ehp * ship.ehp
        viable_contracts = [c for c in viable_contracts if c.actual_value <= max_value]

    if not viable_contracts:
        return [], 0.0, 0, []

    # Try all subsets of contracts, find the most profitable
    best_profit = -float('inf')
    best_time = 0
    best_contracts = []
    best_details = []

    # For efficiency, try subsets in order of decreasing size
    # Start with largest subsets
    for size in range(len(viable_contracts), 0, -1):
        for subset in itertools.combinations(viable_contracts, size):
            # Check total volume
            total_volume = sum(c.m3 for c in subset)
            if total_volume > ship.cargo_size:
                continue

            profit, time_min, details = evaluate_contract_combination(
                list(subset), sde, start_system, ship, config, is_freighter,
                min_isk_per_jump, max_isk_per_ehp, max_time_minutes
            )

            if profit > best_profit or (profit == best_profit and time_min < best_time):
                best_profit = profit
                best_time = time_min
                best_contracts = list(subset)
                best_details = details
                # If we found a full cargo load, we can stop early
                if total_volume >= ship.cargo_size * 0.9:
                    break

        # If we found good contracts, stop searching smaller subsets
        if best_contracts and best_profit > 0:
            break

    # Sort by issuer name for tiebreaking
    if best_contracts:
        best_contracts.sort(key=lambda c: c.issuer)

    return best_contracts, best_profit, best_time, best_details


def plan_contracts_route(sde: SDELoader, config: Config, ship: ShipConfig,
                          start_system: str, contracts: List[Contract]) -> Tuple[List[str], List[Tuple[str, str, Contract]], float]:
    """
    Plan the actual route for a set of contracts.
    Returns: (route_system_names, actions, total_time)
    actions: List of (location, action, contract) where action is 'load' or 'unload'
    """
    if not contracts:
        return [], [], 0.0

    is_freighter = ship.type == "Freighter"
    params = TravelParams(
        align_time=math.ceil(ship.align_time),
        top_speed=ship.top_speed,
        warp_speed=ship.warp_speed,
        dock_time=config.times.dock,
        gate_time=config.times.gate
    )
    planner = RoutePlanner(sde, params, is_freighter)

    # Nearest neighbor route planning
    current_loc = start_system
    route_systems = [start_system]
    actions = []
    total_time = 0.0

    carrying: List[Contract] = []
    total_volume = 0.0

    remaining_pickups = set(c.id for c in contracts)
    remaining_deliveries = set(c.id for c in contracts)
    contracts_by_id = {c.id: c for c in contracts}

    while remaining_pickups or remaining_deliveries:
        best_loc = None
        best_dist = float('inf')
        is_pickup = True
        best_contract_id = None

        # Check pickups
        for cid in remaining_pickups:
            c = contracts_by_id[cid]
            if total_volume + c.m3 <= ship.cargo_size:
                dist = calculate_route_jumps(sde, current_loc, c.start, is_freighter)
                if dist < best_dist:
                    best_dist = dist
                    best_loc = c.start
                    is_pickup = True
                    best_contract_id = cid

        # Check deliveries
        for cid in remaining_deliveries:
            if cid not in [c.id for c in carrying]:
                continue
            c = contracts_by_id[cid]
            dist = calculate_route_jumps(sde, current_loc, c.end, is_freighter)
            if dist < best_dist:
                best_dist = dist
                best_loc = c.end
                is_pickup = False
                best_contract_id = cid

        if best_loc is None:
            if carrying:
                for c in carrying:
                    if c.id in remaining_deliveries:
                        dist = calculate_route_jumps(sde, current_loc, c.end, is_freighter)
                        if dist < best_dist:
                            best_dist = dist
                            best_loc = c.end
                            is_pickup = False
                            best_contract_id = c.id

        if best_loc is None:
            break

        # Travel to location
        _, travel_route = planner.plan(current_loc, best_loc)

        # Add route systems
        for sys_name in travel_route:
            if not route_systems or route_systems[-1] != sys_name:
                route_systems.append(sys_name)

        # Calculate time (simplified)
        total_time += best_dist * (params.align_time + params.gate_time)

        if is_pickup:
            c = contracts_by_id[best_contract_id]
            actions.append((best_loc, 'load', c))
            carrying.append(c)
            total_volume += c.m3
            remaining_pickups.remove(best_contract_id)
            total_time += config.times.move_cargo
        else:
            c = contracts_by_id[best_contract_id]
            actions.append((best_loc, 'unload', c))
            carrying = [cc for cc in carrying if cc.id != best_contract_id]
            total_volume -= c.m3
            remaining_deliveries.remove(best_contract_id)
            total_time += config.times.move_cargo

        current_loc = best_loc

    # Return to start
    _, return_route = planner.plan(current_loc, start_system)
    for sys_name in return_route:
        if not route_systems or route_systems[-1] != sys_name:
            route_systems.append(sys_name)

    return_jumps = calculate_route_jumps(sde, current_loc, start_system, is_freighter)
    total_time += return_jumps * (params.align_time + params.gate_time)

    return route_systems, actions, total_time


def output_contracts_plan(sde: SDELoader, config: Config, ship: ShipConfig,
                           start_system: str, contracts: List[Contract],
                           target_iph: Optional[float], max_time: Optional[int]):
    """Output the formatted contracts hauling plan."""
    if not contracts:
        print("No Good Contracts")
        return

    is_freighter = ship.type == "Freighter"
    params = TravelParams(
        align_time=math.ceil(ship.align_time),
        top_speed=ship.top_speed,
        warp_speed=ship.warp_speed,
        dock_time=config.times.dock,
        gate_time=config.times.gate
    )
    planner = RoutePlanner(sde, params, is_freighter)

    print(f"SHIP: {ship.name}")

    # Plan the route
    current_loc = start_system
    total_time = 0.0
    total_jumps = 0
    total_profit = sum(c.reward for c in contracts)
    total_volume_moved = sum(c.m3 for c in contracts)

    carrying: List[Contract] = []
    total_volume = 0.0

    # Track jumps while holding each contract
    jumps_while_holding = {c.id: 0 for c in contracts}

    remaining_pickups = set(c.id for c in contracts)
    remaining_deliveries = set(c.id for c in contracts)
    contracts_by_id = {c.id: c for c in contracts}

    # Track visited systems for route output
    route_systems = []

    # Print start
    start_sys, start_sta = sde.resolve_location(start_system)
    print(f"START: {start_system}")

    first_action = True

    while remaining_pickups or remaining_deliveries:
        best_loc = None
        best_dist = float('inf')
        is_pickup = True
        best_contract_id = None

        # Check pickups
        for cid in remaining_pickups:
            c = contracts_by_id[cid]
            if total_volume + c.m3 <= ship.cargo_size:
                dist = calculate_route_jumps(sde, current_loc, c.start, is_freighter)
                if dist < best_dist:
                    best_dist = dist
                    best_loc = c.start
                    is_pickup = True
                    best_contract_id = cid

        # Check deliveries
        for cid in remaining_deliveries:
            if cid not in [c.id for c in carrying]:
                continue
            c = contracts_by_id[cid]
            dist = calculate_route_jumps(sde, current_loc, c.end, is_freighter)
            if dist < best_dist:
                best_dist = dist
                best_loc = c.end
                is_pickup = False
                best_contract_id = cid

        if best_loc is None:
            if carrying:
                for c in carrying:
                    if c.id in remaining_deliveries:
                        dist = calculate_route_jumps(sde, current_loc, c.end, is_freighter)
                        best_dist = dist
                        best_loc = c.end
                        is_pickup = False
                        best_contract_id = c.id
                        break

        if best_loc is None:
            break

        # Travel to location
        if best_dist > 0:
            _, travel_route = planner.plan(current_loc, best_loc)
            route_parts = []
            for sys_name in travel_route:
                sys_obj = sde.get_system_by_name(sys_name)
                if sys_obj:
                    route_parts.append(f"{sys_obj.name} ({sys_obj.security:.1f})")
            if route_parts:
                print("GO: " + " -> ".join(route_parts))

            total_jumps += best_dist
            total_time += best_dist * (params.align_time + params.gate_time)

            # Update jumps for carrying contracts
            for c in carrying:
                jumps_while_holding[c.id] += best_dist

        # Dock/perform action
        best_sys, best_sta = sde.resolve_location(best_loc)

        if is_pickup:
            c = contracts_by_id[best_contract_id]

            # Dock
            if best_sta:
                print(f"DOCK: {best_loc}")
                total_time += config.times.dock

            # Load
            reward_m = round(c.reward / 1_000_000.0, 2)
            print(f"LOAD {c.issuer} (id={c.id}): {reward_m:.2f}M ISK | {c.m3:,.2f} m3")
            total_time += config.times.move_cargo

            carrying.append(c)
            total_volume += c.m3
            remaining_pickups.remove(best_contract_id)

            # Undock
            if best_sta:
                print("UNDOCK")
                total_time += config.times.dock
        else:
            c = contracts_by_id[best_contract_id]

            # Dock
            if best_sta:
                print(f"DOCK: {best_loc}")
                total_time += config.times.dock

            # Unload
            jumps = jumps_while_holding[c.id]
            print(f"UNLOAD {c.issuer} (id={c.id}): {jumps} Jumps | {c.m3:,.2f} m3")
            total_time += config.times.move_cargo

            carrying = [cc for cc in carrying if cc.id != best_contract_id]
            total_volume -= c.m3
            remaining_deliveries.remove(best_contract_id)

            # Undock (if more actions to do)
            if remaining_pickups or remaining_deliveries:
                if best_sta:
                    print("UNDOCK")
                    total_time += config.times.dock

        current_loc = best_loc

    # Return to start
    return_jumps = calculate_route_jumps(sde, current_loc, start_system, is_freighter)
    if return_jumps > 0:
        _, return_route = planner.plan(current_loc, start_system)
        route_parts = []
        for sys_name in return_route:
            sys_obj = sde.get_system_by_name(sys_name)
            if sys_obj:
                route_parts.append(f"{sys_obj.name} ({sys_obj.security:.1f})")
        if route_parts:
            print("GO: " + " -> ".join(route_parts))

        total_jumps += return_jumps
        total_time += return_jumps * (params.align_time + params.gate_time)

    # Dock at start
    start_sys, start_sta = sde.resolve_location(start_system)
    if start_sta:
        print(f"DOCK: {start_system}")
        total_time += config.times.dock

    # Print summary
    time_minutes = total_time / 60.0
    print(f"DONE: {format_time(total_time)}")
    print(f"MOVED: {total_volume_moved:,.2f} m3")
    print(f"NUM CONTRACTS: {len(contracts)}")

    profit_m = round(total_profit / 1_000_000.0, 2)
    print(f"PROFIT: {profit_m:.2f}M")

    # ISK/m3
    isk_per_m3 = total_profit / total_volume_moved if total_volume_moved > 0 else 0
    print(f"ISK/M3: {isk_per_m3:.2f}")

    # ISK/Jump (includes return)
    isk_per_jump = total_profit / total_jumps if total_jumps > 0 else 0
    isk_per_jump_m = round(isk_per_jump / 1_000_000.0, 2)
    print(f"ISK/Jump: {isk_per_jump_m:.2f}M")

    # ISK/Hour
    hours = time_minutes / 60.0
    isk_per_hour = total_profit / hours if hours > 0 else 0
    isk_per_hour_m = round(isk_per_hour / 1_000_000.0, 2)
    print(f"ISK/Hour: {isk_per_hour_m:.2f}M")


def main():
    parser = argparse.ArgumentParser(description="EVE Online Travel Planner")
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')

    # Plan command (original functionality)
    plan_parser = subparsers.add_parser('plan', help='Plan a route')
    plan_parser.add_argument("start", help="Starting location (system or station name)")
    plan_parser.add_argument("end", help="Ending location (system or station name)")
    plan_parser.add_argument("--manifest", type=str, help="Path to manifest YAML file")
    plan_parser.add_argument("--config", type=str, help="Path to config YAML file")
    plan_parser.add_argument("--ship", type=str, help="Ship name from config file")
    plan_parser.add_argument("--align", type=float, help="Alignment time in seconds")
    plan_parser.add_argument("--top-speed", type=float, help="Top subwarp speed in m/s")
    plan_parser.add_argument("--warp-speed", type=float, help="Warp speed in AU/s")
    plan_parser.add_argument("--dock-time", type=float, help="Dock/undock time in seconds")
    plan_parser.add_argument("--gate-time", type=float, help="Gate jump time in seconds")
    plan_parser.add_argument("--sde", type=str, required=True, help="Path to SDE directory")

    # Contracts command
    contracts_parser = subparsers.add_parser('contracts', help='Find optimal contracts to haul')
    contracts_parser.add_argument("start", help="Starting location (system or station name)")
    contracts_parser.add_argument("contracts", type=str, help="Path to contracts JSONL file")
    contracts_parser.add_argument("--config", type=str, required=True, help="Path to config YAML file")
    contracts_parser.add_argument("--sde", type=str, required=True, help="Path to SDE directory")
    contracts_parser.add_argument("--target-iph", type=float, help="Target ISK per hour in millions")
    contracts_parser.add_argument("--max-time", type=int, help="Maximum time in minutes")

    args = parser.parse_args()

    # Handle 'contracts' command
    if args.command == 'contracts':
        # Load config
        try:
            config = load_config(args.config)
        except Exception as e:
            print("Error loading config: {}".format(e), file=sys.stderr)
            sys.exit(1)

        # Load SDE
        sde = SDELoader(args.sde)
        try:
            sde.load_all()
        except Exception as e:
            print("Error loading SDE: {}".format(e), file=sys.stderr)
            sys.exit(1)

        # Load contracts
        try:
            contracts = load_contracts(args.contracts)
        except Exception as e:
            print("Error loading contracts: {}".format(e), file=sys.stderr)
            sys.exit(1)

        # Select best ship
        ship = select_best_ship(contracts, config, sde, args.start, config.max_isk_per_ehp)
        if ship is None:
            print("No Good Contracts")
            return

        is_freighter = ship.type == "Freighter"

        # Find optimal contracts
        selected_contracts, profit, time_min, details = find_optimal_contracts(
            contracts, sde, args.start, ship, config, is_freighter, args.max_time
        )

        if not selected_contracts:
            print("No Good Contracts")
            return

        # Output the plan
        output_contracts_plan(sde, config, ship, args.start, selected_contracts, args.target_iph, args.max_time)
        return

    # Handle 'plan' command (original functionality)
    if args.command == 'plan' or args.command is None:
        # Determine mode
        manifest_mode = args.manifest or args.config or args.ship

        if manifest_mode:
            # Manifest mode - require all manifest arguments
            if not args.config:
                print("Error: --config is required when using manifest mode", file=sys.stderr)
                sys.exit(1)
            if not args.ship:
                print("Error: --ship is required when using manifest mode", file=sys.stderr)
                sys.exit(1)

            # Load config
            try:
                config = load_config(args.config)
            except Exception as e:
                print("Error loading config: {}".format(e), file=sys.stderr)
                sys.exit(1)

            # Validate ship exists
            if args.ship not in config.ships:
                print("Error: Ship '{}' not found in config".format(args.ship), file=sys.stderr)
                sys.exit(1)

            # Load manifest (optional)
            manifest = load_manifest(args.manifest) if args.manifest else Manifest(start_cargo=None, waypoints=[])

            # Load SDE
            sde = SDELoader(args.sde)
            try:
                sde.load_all()
            except Exception as e:
                print("Error loading SDE: {}".format(e), file=sys.stderr)
                sys.exit(1)

            # Output manifest plan
            try:
                output_manifest_plan(sde, config, args.ship, args.start, args.end, manifest)
            except ValueError as e:
                print("Error: {}".format(e), file=sys.stderr)
                sys.exit(1)

        else:
            # Original direct parameter mode
            if args.align is None:
                print("Error: --align is required when not using manifest mode", file=sys.stderr)
                sys.exit(1)
            if args.top_speed is None:
                print("Error: --top-speed is required when not using manifest mode", file=sys.stderr)
                sys.exit(1)
            if args.warp_speed is None:
                print("Error: --warp-speed is required when not using manifest mode", file=sys.stderr)
                sys.exit(1)
            if args.dock_time is None:
                print("Error: --dock-time is required when not using manifest mode", file=sys.stderr)
                sys.exit(1)
            if args.gate_time is None:
                print("Error: --gate-time is required when not using manifest mode", file=sys.stderr)
                sys.exit(1)

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

            # Align time is ceiling'd due to server ticks
            align_time = math.ceil(args.align)

            params = TravelParams(
                align_time=align_time,
                top_speed=args.top_speed,
                warp_speed=args.warp_speed,
                dock_time=args.dock_time,
                gate_time=args.gate_time
            )

            # Load SDE
            sde = SDELoader(args.sde)
            try:
                sde.load_all()
            except Exception as e:
                print("Error loading SDE: {}".format(e), file=sys.stderr)
                sys.exit(1)

            # Plan route
            planner = RoutePlanner(sde, params)
            try:
                total_time, route = planner.plan(args.start, args.end)
            except ValueError as e:
                print("Error: {}".format(e), file=sys.stderr)
                sys.exit(1)

            # Output formatted plan
            output_plan(sde, args.start, args.end, total_time, route)

    # Legacy mode - handle when no subcommand is used
    elif not hasattr(args, 'start') or args.start is None:
        # Parse again for legacy mode
        parser = argparse.ArgumentParser(description="EVE Online Travel Planner")
        parser.add_argument("start", help="Starting location (system or station name)")
        parser.add_argument("end", help="Ending location (system or station name)")
        parser.add_argument("--manifest", type=str, help="Path to manifest YAML file")
        parser.add_argument("--config", type=str, help="Path to config YAML file")
        parser.add_argument("--ship", type=str, help="Ship name from config file")
        parser.add_argument("--align", type=float, help="Alignment time in seconds")
        parser.add_argument("--top-speed", type=float, help="Top subwarp speed in m/s")
        parser.add_argument("--warp-speed", type=float, help="Warp speed in AU/s")
        parser.add_argument("--dock-time", type=float, help="Dock/undock time in seconds")
        parser.add_argument("--gate-time", type=float, help="Gate jump time in seconds")
        parser.add_argument("--sde", type=str, required=True, help="Path to SDE directory")
        args = parser.parse_args()

        # Legacy handling code (same as above)
        manifest_mode = args.manifest or args.config or args.ship

        if manifest_mode:
            if not args.config:
                print("Error: --config is required when using manifest mode", file=sys.stderr)
                sys.exit(1)
            if not args.ship:
                print("Error: --ship is required when using manifest mode", file=sys.stderr)
                sys.exit(1)

            try:
                config = load_config(args.config)
            except Exception as e:
                print("Error loading config: {}".format(e), file=sys.stderr)
                sys.exit(1)

            if args.ship not in config.ships:
                print("Error: Ship '{}' not found in config".format(args.ship), file=sys.stderr)
                sys.exit(1)

            manifest = load_manifest(args.manifest) if args.manifest else Manifest(start_cargo=None, waypoints=[])

            sde = SDELoader(args.sde)
            try:
                sde.load_all()
            except Exception as e:
                print("Error loading SDE: {}".format(e), file=sys.stderr)
                sys.exit(1)

            try:
                output_manifest_plan(sde, config, args.ship, args.start, args.end, manifest)
            except ValueError as e:
                print("Error: {}".format(e), file=sys.stderr)
                sys.exit(1)
        else:
            if args.align is None:
                print("Error: --align is required when not using manifest mode", file=sys.stderr)
                sys.exit(1)
            if args.top_speed is None:
                print("Error: --top-speed is required when not using manifest mode", file=sys.stderr)
                sys.exit(1)
            if args.warp_speed is None:
                print("Error: --warp-speed is required when not using manifest mode", file=sys.stderr)
                sys.exit(1)
            if args.dock_time is None:
                print("Error: --dock-time is required when not using manifest mode", file=sys.stderr)
                sys.exit(1)
            if args.gate_time is None:
                print("Error: --gate-time is required when not using manifest mode", file=sys.stderr)
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

            align_time = math.ceil(args.align)

            params = TravelParams(
                align_time=align_time,
                top_speed=args.top_speed,
                warp_speed=args.warp_speed,
                dock_time=args.dock_time,
                gate_time=args.gate_time
            )

            sde = SDELoader(args.sde)
            try:
                sde.load_all()
            except Exception as e:
                print("Error loading SDE: {}".format(e), file=sys.stderr)
                sys.exit(1)

            planner = RoutePlanner(sde, params)
            try:
                total_time, route = planner.plan(args.start, args.end)
            except ValueError as e:
                print("Error: {}".format(e), file=sys.stderr)
                sys.exit(1)

            output_plan(sde, args.start, args.end, total_time, route)


def output_plan(sde: SDELoader, start_name: str, end_name: str,
                total_time: float, route: List[str]):
    """Output the formatted travel plan (original mode)."""
    # Resolve locations
    start_sys, start_sta = sde.resolve_location(start_name)
    end_sys, end_sta = sde.resolve_location(end_name)

    # START line
    if start_sta:
        print("START: {}".format(start_sta.name))
    else:
        print("START: {}".format(start_sys.name))

    # UNDOCK if starting from station
    if start_sta:
        print("UNDOCK")

    # GO line with route
    if len(route) > 1:
        route_parts = []
        for sys_name in route:
            system = sde.get_system_by_name(sys_name)
            if system:
                route_parts.append("{} ({:.1f})".format(system.name, system.security))
            else:
                route_parts.append(sys_name)
        print("GO: " + " -> ".join(route_parts))
    elif len(route) == 1 and not end_sta:
        # Single system route, end is system
        system = sde.get_system_by_name(route[0])
        if system:
            print("GO: {} ({:.1f})".format(system.name, system.security))

    # DOCK if ending at station
    if end_sta:
        print("DOCK: {}".format(end_sta.name))

    # DONE line
    print("DONE: {}".format(format_time(total_time)))


if __name__ == "__main__":
    main()
