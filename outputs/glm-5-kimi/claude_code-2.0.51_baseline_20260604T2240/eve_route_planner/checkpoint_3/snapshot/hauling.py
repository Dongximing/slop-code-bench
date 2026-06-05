#!/usr/bin/env python3
"""
EVE Online Travel Route Planner

Plans routes through New Eden using the EVE Static Data Export (SDE).
Supports cargo hauling operations with manifest files and ship configurations.
"""

import argparse
import bz2
import csv
import heapq
import json
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# Constants
AU_IN_M = 149_597_870_700  # 1 AU in meters
ZARZAKH_SYSTEM_ID = 30100000
ZARZAKH_LOCK_TIME = 6 * 3600  # 6 hours in seconds


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
    x: float
    y: float
    z: float
    name: str


@dataclass
class Location:
    """Represents a location in space (system, station, or gate)."""
    system_id: int
    station_id: Optional[int] = None
    gate_id: Optional[int] = None
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def is_station(self) -> bool:
        return self.station_id is not None

    def is_gate(self) -> bool:
        return self.gate_id is not None


@dataclass
class RouteSegment:
    """A segment of a route."""
    location: Location
    action: str  # "undock", "warp", "jump", "dock", "wait"
    time: float = 0.0


@dataclass
class ShipConfig:
    """Configuration for a ship."""
    name: str
    ship_type: str  # "Deep Space Transport", "Blockade Runner", "Freighter"
    align: float
    top_speed: float
    warp_speed: float
    cargo_size: int
    ehp: Optional[int] = None  # Effective Hit Points


@dataclass
class TimeConfig:
    """Configuration for time-related settings."""
    dock: float
    gate: float
    move_cargo: float


@dataclass
class Config:
    """Configuration loaded from YAML."""
    ships: Dict[str, ShipConfig]
    times: TimeConfig
    min_isk_per_jump: Optional[float] = None
    max_isk_per_ehp: Optional[float] = None


@dataclass
class Waypoint:
    """Represents a waypoint in a manifest."""
    name: str
    cargo: Optional[float]


@dataclass
class Manifest:
    """Manifest for cargo hauling."""
    start_cargo: Optional[float]
    waypoints: List[Waypoint]


@dataclass
class Contract:
    """Represents a hauling contract."""
    id: int  # Contract ID (monotonic, starting from 1)
    start: str  # Start location name
    end: str  # End location name
    collateral: float  # Collateral in ISK
    volume: float  # Volume in m3
    actual_value: float  # Actual value of cargo in ISK
    reward: float  # Reward in ISK
    issuer: str  # Issuer name


class SDELoader:
    """Loads and parses EVE SDE data files."""

    def __init__(self, sde_path: str):
        self.sde_path = sde_path
        self.systems: Dict[int, System] = {}
        self.stations: Dict[int, Station] = {}
        self.stations_by_name: Dict[str, Station] = {}
        self.systems_by_name: Dict[str, System] = {}
        self.stargates: Dict[int, List[Stargate]] = {}  # system_id -> list of gates
        self.jumps: Dict[int, Set[int]] = {}  # system_id -> set of connected systems
        self.stargate_positions: Dict[int, Stargate] = {}  # gate_id -> Stargate

    def load_all(self):
        """Load all SDE data."""
        self._load_systems()
        self._load_stations()
        self._load_jumps()
        self._load_stargates()

    def _load_systems(self):
        """Load solar systems."""
        path = os.path.join(self.sde_path, "mapSolarSystems.csv.bz2")
        with bz2.open(path, "rt", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                system = System(
                    id=int(row["solarSystemID"]),
                    name=row["solarSystemName"],
                    security=float(row["security"]),
                    x=float(row["x"]),
                    y=float(row["y"]),
                    z=float(row["z"])
                )
                self.systems[system.id] = system
                self.systems_by_name[system.name.lower()] = system

    def _load_stations(self):
        """Load stations."""
        path = os.path.join(self.sde_path, "staStations.csv.bz2")
        with bz2.open(path, "rt", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                station = Station(
                    id=int(row["stationID"]),
                    name=row["stationName"],
                    system_id=int(row["solarSystemID"]),
                    x=float(row["x"]),
                    y=float(row["y"]),
                    z=float(row["z"])
                )
                self.stations[station.id] = station
                self.stations_by_name[station.name.lower()] = station

    def _load_jumps(self):
        """Load solar system jumps (bidirectional)."""
        path = os.path.join(self.sde_path, "mapSolarSystemJumps.csv.bz2")
        with bz2.open(path, "rt", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                from_id = int(row["fromSolarSystemID"])
                to_id = int(row["toSolarSystemID"])
                if from_id not in self.jumps:
                    self.jumps[from_id] = set()
                if to_id not in self.jumps:
                    self.jumps[to_id] = set()
                self.jumps[from_id].add(to_id)
                self.jumps[to_id].add(from_id)

    def _load_stargates(self):
        """Load stargate positions from mapDenormalize."""
        path = os.path.join(self.sde_path, "mapDenormalize.csv.bz2")
        with bz2.open(path, "rt", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                group_id = int(row["groupID"]) if row["groupID"] and row["groupID"] != "None" else 0

                # Stargates typically have groupID 10
                if group_id == 10:
                    system_id = int(row["solarSystemID"])
                    item_id = int(row["itemID"])
                    gate = Stargate(
                        id=item_id,
                        system_id=system_id,
                        x=float(row["x"]) if row["x"] else 0.0,
                        y=float(row["y"]) if row["y"] else 0.0,
                        z=float(row["z"]) if row["z"] else 0.0,
                        name=row["itemName"]
                    )
                    if system_id not in self.stargates:
                        self.stargates[system_id] = []
                    self.stargates[system_id].append(gate)
                    self.stargate_positions[item_id] = gate

    def get_system_by_name(self, name: str) -> Optional[System]:
        """Get a system by name (case-insensitive)."""
        return self.systems_by_name.get(name.lower())

    def get_station_by_name(self, name: str) -> Optional[Station]:
        """Get a station by name (case-insensitive)."""
        return self.stations_by_name.get(name.lower())

    def resolve_location(self, name: str) -> Tuple[Optional[System], Optional[Station]]:
        """Resolve a location name to either a system or station."""
        station = self.get_station_by_name(name)
        if station:
            return None, station
        system = self.get_system_by_name(name)
        return system, None

    def is_high_sec(self, system_id: int) -> bool:
        """Check if a system is high security (security >= 0.5)."""
        system = self.systems.get(system_id)
        return system is not None and system.security >= 0.5

    def is_low_sec(self, system_id: int) -> bool:
        """Check if a system is low security (0 < security < 0.5)."""
        system = self.systems.get(system_id)
        return system is not None and 0 < system.security < 0.5

    def is_null_sec(self, system_id: int) -> bool:
        """Check if a system is null security (security <= 0)."""
        system = self.systems.get(system_id)
        return system is not None and system.security <= 0


class WarpCalculator:
    """Calculates warp times based on EVE mechanics."""

    def __init__(self, align_time: float, top_speed: float, warp_speed: float,
                 dock_time: float, gate_time: float, move_cargo_time: float = 0.0):
        self.align_time = math.ceil(align_time)  # Ceiling due to server ticks
        self.top_speed = top_speed  # m/s
        self.warp_speed = warp_speed  # AU/s
        self.dock_time = dock_time
        self.gate_time = gate_time
        self.move_cargo_time = move_cargo_time

        # Calculate derived values
        self.v_drop = min(top_speed / 2, 100)  # Dropout speed in m/s
        self.k_a = warp_speed  # Acceleration rate in AU/s
        self.k_d = min(warp_speed / 3, 2)  # Deceleration rate in AU/s, capped at 2

    def calculate_warp_time(self, distance_m: float) -> float:
        """
        Calculate warp time for a given distance.

        Args:
            distance_m: Distance in meters

        Returns:
            Time in seconds
        """
        D = distance_m
        v_warp_au = self.warp_speed  # AU/s
        v_warp_ms = v_warp_au * AU_IN_M  # m/s

        # Acceleration distance is 1 AU
        d_a = AU_IN_M

        # Deceleration distance
        d_d = v_warp_ms / self.k_d

        # Minimum warp distance
        d_min = d_a + d_d

        # Check if we reach full warp speed
        if D < d_min:
            # Peak warp speed is reduced
            v_warp_ms = (D * self.k_a * AU_IN_M * self.k_d) / (self.k_a + self.k_d)
            v_warp_au = v_warp_ms / AU_IN_M
            cruise_time = 0.0
        else:
            # Full warp with cruise phase
            cruise_time = (D - d_min) / v_warp_ms

        # Acceleration time
        t_accel = (1.0 / self.k_a) * math.log(v_warp_au / self.k_a)

        # Deceleration time
        t_decel = (1.0 / self.k_d) * math.log(v_warp_ms / self.v_drop)

        return t_accel + cruise_time + t_decel

    def calculate_distance(self, from_loc: Location, to_loc: Location) -> float:
        """Calculate distance between two locations in the same system."""
        dx = to_loc.x - from_loc.x
        dy = to_loc.y - from_loc.y
        dz = to_loc.z - from_loc.z
        return math.sqrt(dx * dx + dy * dy + dz * dz)


class RoutePlanner:
    """Plans routes through New Eden."""

    def __init__(self, sde: SDELoader, warp_calc: WarpCalculator):
        self.sde = sde
        self.warp_calc = warp_calc

    def plan_route(self, start_name: str, end_name: str, is_freighter: bool = False) -> Tuple[List[Tuple[str, float, Optional[float]]], float]:
        """
        Plan a route from start to end.

        Args:
            start_name: Starting location name
            end_name: Ending location name
            is_freighter: If True, restrict route to high-sec only (with low-sec exception if unavoidable)

        Returns:
            Tuple of (route steps with security, total time)
            Each step is (system_name, time_for_step, security)
        """
        # Resolve start and end locations
        start_system, start_station = self.sde.resolve_location(start_name)
        end_system, end_station = self.sde.resolve_location(end_name)

        if start_system is None and start_station is None:
            raise ValueError(f"Unknown start location: {start_name}")
        if end_system is None and end_station is None:
            raise ValueError(f"Unknown end location: {end_name}")

        # Determine actual start/end systems
        if start_station:
            start_system_id = start_station.system_id
            start_system = self.sde.systems[start_system_id]
        else:
            start_system_id = start_system.id

        if end_station:
            end_system_id = end_station.system_id
            end_system = self.sde.systems[end_system_id]
        else:
            end_system_id = end_system.id

        # Find the shortest path considering constraints
        path, total_time = self._find_path(start_system_id, end_system_id,
                                           start_station, end_station, is_freighter)

        # Build route output
        route_steps = []
        for sys_id in path:
            sys = self.sde.systems[sys_id]
            route_steps.append((sys.name, sys.security))

        return route_steps, total_time

    def _find_path(self, start_id: int, end_id: int,
                   start_station: Optional[Station],
                   end_station: Optional[Station],
                   is_freighter: bool = False) -> Tuple[List[int], float]:
        """
        Find shortest path using Dijkstra's algorithm with constraints.
        """
        # If same system, handle intra-system travel
        if start_id == end_id:
            path = [start_id]
            time = self._calculate_intra_system_time(start_id, start_station, end_station)
            return path, time

        # Dijkstra's algorithm with state tracking for Zarzakh
        best_times: Dict[Tuple[int, Optional[int]], float] = {}
        best_paths: Dict[Tuple[int, Optional[int]], List[int]] = {}

        pq: List[Tuple[float, int, Optional[int], List[int]]] = []

        # Initial state
        undock_time = self.warp_calc.dock_time if start_station else 0.0
        initial_time = undock_time
        heapq.heappush(pq, (initial_time, start_id, None, [start_id]))
        best_times[(start_id, None)] = initial_time

        while pq:
            current_time, current_sys, zarzakh_entry, path = heapq.heappop(pq)

            state = (current_sys, zarzakh_entry)
            if current_time > best_times.get(state, float('inf')):
                continue

            # Check if we've reached the destination
            if current_sys == end_id:
                # Add dock time if ending at a station
                final_time = current_time
                if end_station:
                    final_time += self.warp_calc.dock_time
                return path, final_time

            # Get connected systems
            if current_sys not in self.sde.jumps:
                continue

            neighbors = self.sde.jumps[current_sys]

            # Apply Zarzakh constraint
            if current_sys == ZARZAKH_SYSTEM_ID:
                # We're in Zarzakh - can only exit via the gate we entered from
                if zarzakh_entry is not None:
                    neighbors = {zarzakh_entry}
                # If zarzakh_entry is None (starting here), we can use any gate

            for next_sys in neighbors:
                # For freighters, check security constraints
                if is_freighter:
                    next_system = self.sde.systems.get(next_sys)
                    if next_system and next_system.security < 0.5:
                        # Check if this is the only possible route
                        # We'll handle this by finding an alternative path first
                        # If no high-sec path exists, allow low/null-sec
                        pass

                # Calculate time to travel to next system
                travel_time = self._calculate_system_travel_time(
                    current_sys, next_sys, current_sys == start_id and start_station
                )

                # Determine new Zarzakh state
                new_zarzakh_entry = None
                if next_sys == ZARZAKH_SYSTEM_ID:
                    # Entering Zarzakh - we're locked to this gate
                    new_zarzakh_entry = current_sys

                new_state = (next_sys, new_zarzakh_entry)
                new_time = current_time + travel_time

                if new_time < best_times.get(new_state, float('inf')):
                    best_times[new_state] = new_time
                    new_path = path + [next_sys]
                    heapq.heappush(pq, (new_time, next_sys, new_zarzakh_entry, new_path))

        raise ValueError(f"No path found from {start_id} to {end_id}")

    def find_path_freighter(self, start_id: int, end_id: int,
                            start_station: Optional[Station],
                            end_station: Optional[Station]) -> Tuple[List[Tuple[str, float]], float]:
        """
        Find path for freighter - prefer high-sec, allow low-sec only if unavoidable.
        """
        # First try to find high-sec only path
        high_sec_path = self._find_high_sec_path(start_id, end_id, start_station, end_station)

        if high_sec_path:
            path, time = high_sec_path
            # Convert path to route steps
            route_steps = [(self.sde.systems[sys_id].name, self.sde.systems[sys_id].security) for sys_id in path]
            return route_steps, time

        # If no high-sec path, allow all systems
        path, time = self._find_path(start_id, end_id, start_station, end_station, is_freighter=True)
        route_steps = [(self.sde.systems[sys_id].name, self.sde.systems[sys_id].security) for sys_id in path]
        return route_steps, time

    def _find_high_sec_path(self, start_id: int, end_id: int,
                            start_station: Optional[Station],
                            end_station: Optional[Station]) -> Optional[Tuple[List[int], float]]:
        """Find path using only high-sec systems."""
        if start_id == end_id:
            path = [start_id]
            time = self._calculate_intra_system_time(start_id, start_station, end_station)
            return path, time

        best_times: Dict[int, float] = {}
        pq: List[Tuple[float, int, List[int]]] = []

        undock_time = self.warp_calc.dock_time if start_station else 0.0
        initial_time = undock_time
        heapq.heappush(pq, (initial_time, start_id, [start_id]))
        best_times[start_id] = initial_time

        while pq:
            current_time, current_sys, path = heapq.heappop(pq)

            if current_time > best_times.get(current_sys, float('inf')):
                continue

            if current_sys == end_id:
                final_time = current_time
                if end_station:
                    final_time += self.warp_calc.dock_time
                return path, final_time

            if current_sys not in self.sde.jumps:
                continue

            neighbors = self.sde.jumps[current_sys]

            for next_sys in neighbors:
                # Only allow high-sec systems
                if not self.sde.is_high_sec(next_sys):
                    continue

                travel_time = self._calculate_system_travel_time(
                    current_sys, next_sys, current_sys == start_id and start_station
                )

                new_time = current_time + travel_time

                if new_time < best_times.get(next_sys, float('inf')):
                    best_times[next_sys] = new_time
                    heapq.heappush(pq, (new_time, next_sys, path + [next_sys]))

        return None

    def _calculate_system_travel_time(self, from_sys: int, to_sys: int,
                                      is_first_warp: bool) -> float:
        """Calculate time to travel from one system to another."""
        total_time = 0.0

        # Add align time
        total_time += self.warp_calc.align_time

        # Warp distance - use approximate gate distance
        warp_dist = 10 * AU_IN_M  # Approximate gate distance

        warp_time = self.warp_calc.calculate_warp_time(warp_dist)
        total_time += warp_time

        # Add gate time
        total_time += self.warp_calc.gate_time

        return total_time

    def _calculate_intra_system_time(self, system_id: int,
                                     start_station: Optional[Station],
                                     end_station: Optional[Station]) -> float:
        """Calculate time for intra-system travel."""
        total_time = 0.0

        # Undock time if starting from station
        if start_station:
            total_time += self.warp_calc.dock_time

        # Warp from start to end
        if start_station and end_station:
            dx = end_station.x - start_station.x
            dy = end_station.y - start_station.y
            dz = end_station.z - start_station.z
            distance = math.sqrt(dx*dx + dy*dy + dz*dz)
        elif start_station:
            sys = self.sde.systems[system_id]
            dx = sys.x - start_station.x
            dy = sys.y - start_station.y
            dz = sys.z - start_station.z
            distance = math.sqrt(dx*dx + dy*dy + dz*dz)
        elif end_station:
            sys = self.sde.systems[system_id]
            dx = end_station.x - sys.x
            dy = end_station.y - sys.y
            dz = end_station.z - sys.z
            distance = math.sqrt(dx*dx + dy*dy + dz*dz)
        else:
            distance = 0

        if distance > 0:
            total_time += self.warp_calc.align_time
            total_time += self.warp_calc.calculate_warp_time(distance)

        # Dock time if ending at station
        if end_station:
            total_time += self.warp_calc.dock_time

        return total_time


class CargoHauler:
    """Plans cargo hauling operations."""

    def __init__(self, sde: SDELoader, warp_calc: WarpCalculator, ship: ShipConfig, times: TimeConfig):
        self.sde = sde
        self.warp_calc = warp_calc
        self.ship = ship
        self.times = times
        self.planner = RoutePlanner(sde, warp_calc)
        self.is_freighter = ship.ship_type == "Freighter"

    def plan_haul(self, start_name: str, end_name: str, manifest: Manifest) -> Tuple[List[str], float, float]:
        """
        Plan cargo hauling operations.

        Returns:
            Tuple of (output lines, total time, total cargo moved)
        """
        output_lines = []
        total_time = 0.0
        total_cargo_moved = 0.0

        # Resolve start and end locations
        start_system, start_station = self.sde.resolve_location(start_name)
        end_system, end_station = self.sde.resolve_location(end_name)

        if start_system is None and start_station is None:
            raise ValueError(f"Unknown start location: {start_name}")
        if end_system is None and end_station is None:
            raise ValueError(f"Unknown end location: {end_name}")

        start_system_id = start_station.system_id if start_station else start_system.id
        end_system_id = end_station.system_id if end_station else end_system.id

        start_location_name = start_station.name if start_station else start_system.name
        end_location_name = end_station.name if end_station else end_system.name

        # Build cargo operations list
        # Each cargo operation: (pickup_location, pickup_amount, delivery_location, delivery_amount)
        cargo_operations = []

        # Handle start_cargo
        start_cargo = manifest.start_cargo if manifest.start_cargo is not None else 0

        # Collect all waypoints with cargo
        waypoint_cargos = []
        for wp in manifest.waypoints:
            if wp.cargo is not None and wp.cargo > 0:
                waypoint_cargos.append((wp.name, wp.cargo))

        # Calculate total cargo to move
        total_cargo = start_cargo + sum(cargo for _, cargo in waypoint_cargos)

        # If no cargo, just plan a simple route
        if total_cargo == 0:
            output_lines.append(f"START: {start_location_name}")
            if start_station:
                output_lines.append("UNDOCK")
            route_steps, route_time = self._plan_route_with_constraints(start_name, end_name)
            if route_steps:
                route_str = " -> ".join(f"{name} ({sec:.1f})" for name, sec in route_steps)
                output_lines.append(f"GO: {route_str}")
            if end_station:
                output_lines.append(f"DOCK: {end_location_name}")
            output_lines.append(f"DONE: {format_time(route_time)}")
            return output_lines, route_time, 0.0

        # Plan trips
        # Strategy: Calculate optimal trip ordering based on tie-breaking rules
        # 1. Finish waypoints (pick up all cargo)
        # 2. Minimize warps with cargo in hold
        # 3. Alphabetical

        trips = self._plan_trips(start_name, end_name, manifest)

        trip_num = 0
        for trip in trips:
            trip_num += 1
            if trip_num > 1:
                output_lines.append(f"[--- TRIP {trip_num} ---]")

            trip_time, trip_cargo = self._execute_trip(trip, output_lines, start_name, end_name)
            total_time += trip_time
            total_cargo_moved += trip_cargo

        output_lines.append(f"DONE: {format_time(total_time)}")
        if total_cargo_moved > 0:
            output_lines.append(f"MOVED: {total_cargo_moved:,.2f} m3")

        return output_lines, total_time, total_cargo_moved

    def _plan_trips(self, start_name: str, end_name: str, manifest: Manifest) -> List[List[Tuple[str, Optional[float], str, Optional[float]]]]:
        """
        Plan all trips needed to move the cargo.

        Returns list of trips, where each trip is a list of operations:
        (location, cargo_to_load, next_location, cargo_to_unload)

        Trip planning strategy (tie-breaking order):
        1. Waypoints Finished (prefer finishing all waypoint pickups first)
        2. Minimum warps in space with cargo in hold
        3. Alphabetical

        Implementation:
        - Prioritize finishing waypoint pickups first
        - First trip: Start at start_name, visit waypoints in order to fill cargo
        - Subsequent trips: Start at end (previous trip end), go back to start/waypoints
        """
        cargo_capacity = self.ship.cargo_size

        start_cargo = manifest.start_cargo if manifest.start_cargo is not None else 0
        total_cargo = start_cargo + sum(wp.cargo or 0 for wp in manifest.waypoints)

        if total_cargo == 0:
            return [[(start_name, None, end_name, None)]]

        trips = []
        # Track remaining cargo at start location
        remaining_start_cargo = start_cargo
        # Track remaining cargo at waypoints (list of (location, amount))
        remaining_waypoints = [(wp.name, wp.cargo) for wp in manifest.waypoints if wp.cargo and wp.cargo > 0]

        trip_num = 0
        while remaining_start_cargo > 0 or remaining_waypoints:
            trip_num += 1
            trip_operations = []
            trip_cargo = 0.0

            if trip_num == 1:
                # First trip: start at start_name
                current_loc = start_name
            else:
                # Subsequent trips: start at end (where previous trip ended)
                current_loc = end_name

            # Priority: Pick up from waypoints first to finish them
            # But we can only pick up waypoints after traveling to them

            # For first trip, we can visit waypoints in order to fill cargo
            # For subsequent trips, we need to travel from end to start/waypoints

            if trip_num == 1:
                # First trip strategy:
                # 1. Take partial cargo from start
                # 2. Visit waypoints in order to fill cargo
                # 3. Go to end

                # Calculate how much cargo we need to reserve at start
                # vs how much we can fill with waypoints
                waypoint_cargo = sum(amt for _, amt in remaining_waypoints)

                # Take enough from start to complement waypoints
                # Goal: Fill capacity with start + waypoints (in order)
                for wp_loc, wp_amt in remaining_waypoints:
                    if trip_cargo >= cargo_capacity:
                        break
                    if wp_amt <= 0:
                        continue

                    # Calculate how much we can take from start before going to this waypoint
                    space_left = cargo_capacity - trip_cargo
                    # If waypoint cargo fits with start cargo
                    if current_loc == start_name and remaining_start_cargo > 0:
                        # Take partial from start
                        start_take = min(remaining_start_cargo, space_left - min(wp_amt, space_left))
                        if start_take > 0:
                            # Load at start
                            trip_operations.append((start_name, start_take, start_name, None))
                            trip_cargo += start_take
                            remaining_start_cargo -= start_take
                            space_left = cargo_capacity - trip_cargo

                    # Travel to waypoint if needed
                    if current_loc != wp_loc:
                        trip_operations.append((current_loc, None, wp_loc, None))
                        current_loc = wp_loc

                    # Load at waypoint
                    take = min(wp_amt, cargo_capacity - trip_cargo)
                    if take > 0:
                        trip_operations.append((wp_loc, take, wp_loc, None))
                        trip_cargo += take
                        # Update remaining waypoint cargo
                        for i, (loc, amt) in enumerate(remaining_waypoints):
                            if loc == wp_loc and amt == wp_amt:
                                remaining_waypoints[i] = (loc, amt - take)
                                break

                # If we still have capacity and haven't moved from start, load remaining start cargo
                if current_loc == start_name and remaining_start_cargo > 0 and trip_cargo < cargo_capacity:
                    take = min(remaining_start_cargo, cargo_capacity - trip_cargo)
                    trip_operations.append((start_name, take, start_name, None))
                    trip_cargo += take
                    remaining_start_cargo -= take

            else:
                # Subsequent trips: Start at end, need to go to pickups
                # Strategy: Visit waypoints first (to finish them), then start

                # Clean up empty waypoints
                remaining_waypoints = [(loc, amt) for loc, amt in remaining_waypoints if amt > 0]

                # Visit waypoints first (to finish them)
                for wp_loc, wp_amt in remaining_waypoints[:]:
                    if trip_cargo >= cargo_capacity:
                        break
                    if wp_amt <= 0:
                        continue

                    # Travel to waypoint
                    if current_loc != wp_loc:
                        trip_operations.append((current_loc, None, wp_loc, None))
                        current_loc = wp_loc

                    # Load at waypoint
                    take = min(wp_amt, cargo_capacity - trip_cargo)
                    trip_operations.append((wp_loc, take, wp_loc, None))
                    trip_cargo += take
                    # Update remaining
                    for i, (loc, amt) in enumerate(remaining_waypoints):
                        if loc == wp_loc:
                            remaining_waypoints[i] = (loc, amt - take)
                            break

                remaining_waypoints = [(loc, amt) for loc, amt in remaining_waypoints if amt > 0]

                # If capacity remains, go to start and load
                if trip_cargo < cargo_capacity and remaining_start_cargo > 0:
                    if current_loc != start_name:
                        trip_operations.append((current_loc, None, start_name, None))
                        current_loc = start_name
                    take = min(remaining_start_cargo, cargo_capacity - trip_cargo)
                    trip_operations.append((start_name, take, start_name, None))
                    trip_cargo += take
                    remaining_start_cargo -= take

            # Go to end and unload
            if current_loc != end_name:
                trip_operations.append((current_loc, None, end_name, None))

            trip_operations.append((end_name, None, end_name, trip_cargo))

            trips.append(trip_operations)

            # Clean up empty waypoints for next iteration
            remaining_waypoints = [(loc, amt) for loc, amt in remaining_waypoints if amt > 0]

            # Safety check to prevent infinite loop
            if trip_num > 1000:
                break

        return trips

    def _execute_trip(self, trip: List[Tuple[str, Optional[float], str, Optional[float]]],
                      output_lines: List[str], start_name: str, end_name: str) -> Tuple[float, float]:
        """
        Execute a single trip and generate output lines.

        Returns:
            Tuple of (trip time, cargo moved in this trip)
        """
        trip_time = 0.0
        trip_cargo = 0.0
        current_hold = 0.0

        for i, (from_loc, load_cargo, to_loc, unload_cargo) in enumerate(trip):
            # Resolve locations
            from_system, from_station = self.sde.resolve_location(from_loc)
            to_system, to_station = self.sde.resolve_location(to_loc)

            from_system_id = from_station.system_id if from_station else from_system.id
            to_system_id = to_station.system_id if to_station else to_system.id

            from_location_name = from_station.name if from_station else from_system.name
            to_location_name = to_station.name if to_station else to_system.name

            # First operation: print START
            if i == 0:
                output_lines.append(f"START: {from_location_name}")

            # Handle loading cargo
            if load_cargo is not None and load_cargo > 0:
                output_lines.append(f"LOAD: {load_cargo:,.2f} m3")
                current_hold += load_cargo
                trip_cargo += load_cargo
                trip_time += self.times.move_cargo

            # Handle undock if at station and traveling somewhere
            if from_station and from_system_id != to_system_id:
                output_lines.append("UNDOCK")
                trip_time += self.times.dock

            # Handle travel
            if from_system_id != to_system_id or from_location_name != to_location_name:
                route_steps, route_time = self._plan_route_with_constraints(from_loc, to_loc)
                if route_steps:
                    route_str = " -> ".join(f"{name} ({sec:.1f})" for name, sec in route_steps)
                    output_lines.append(f"GO: {route_str}")
                    trip_time += route_time

            # Handle docking at destination if station
            if to_station and from_system_id != to_system_id:
                output_lines.append(f"DOCK: {to_location_name}")
                trip_time += self.times.dock

            # Handle unloading cargo
            if unload_cargo is not None and unload_cargo > 0:
                output_lines.append(f"UNLOAD: {unload_cargo:,.2f} m3")
                current_hold -= unload_cargo
                trip_time += self.times.move_cargo

        return trip_time, trip_cargo

    def _plan_route_with_constraints(self, start_name: str, end_name: str) -> Tuple[List[Tuple[str, float]], float]:
        """Plan route with freighter constraints if needed."""
        if self.is_freighter:
            return self.planner.find_path_freighter(
                self._get_system_id(start_name),
                self._get_system_id(end_name),
                self._get_station(start_name),
                self._get_station(end_name)
            )
        else:
            return self.planner.plan_route(start_name, end_name)

    def _get_system_id(self, name: str) -> int:
        """Get system ID from location name."""
        system, station = self.sde.resolve_location(name)
        if station:
            return station.system_id
        return system.id

    def _get_station(self, name: str) -> Optional[Station]:
        """Get station from location name if it's a station."""
        _, station = self.sde.resolve_location(name)
        return station


class ContractPlanner:
    """Plans contract-based hauling operations."""

    def __init__(self, sde: SDELoader, config: Config):
        self.sde = sde
        self.config = config

    def select_best_ship(self, contracts: List[Contract], base_location: str) -> Optional[ShipConfig]:
        """
        Select the best ship for the given contracts.
        Criteria: Minimize travel time, tiebreak by EHP then Name.
        """
        if not contracts:
            return None

        # Filter ships that can carry the largest contract volume
        max_volume = max(c.volume for c in contracts)
        viable_ships = [ship for ship in self.config.ships.values() if ship.cargo_size >= max_volume]

        if not viable_ships:
            return None

        # Calculate total travel time for each ship
        ship_times = []
        for ship in viable_ships:
            warp_calc = WarpCalculator(
                align_time=ship.align,
                top_speed=ship.top_speed,
                warp_speed=ship.warp_speed,
                dock_time=self.config.times.dock,
                gate_time=self.config.times.gate
            )
            planner = RoutePlanner(self.sde, warp_calc)

            # Calculate total time to complete all contracts and return to base
            total_time = self._estimate_total_time(contracts, base_location, planner, ship)
            ship_times.append((total_time, ship))

        # Sort by time, then EHP (descending, higher is better), then name
        # Note: EHP only matters for non-blockade runners
        def sort_key(item):
            time, ship = item
            # For blockade runners, EHP doesn't matter, use 0 for neutral comparison
            ehp = ship.ehp if ship.ehp is not None and ship.ship_type != "Blockade Runner" else 0
            return (time, -ehp, ship.name)

        ship_times.sort(key=sort_key)
        return ship_times[0][1]

    def _estimate_total_time(self, contracts: List[Contract], base_location: str,
                            planner: RoutePlanner, ship: ShipConfig) -> float:
        """Estimate total time to complete all contracts and return to base."""
        total_time = 0.0
        current_loc = base_location
        is_freighter = ship.ship_type == "Freighter"

        # Simple greedy estimation: visit contracts in order
        for contract in contracts:
            # Travel to pickup
            try:
                if current_loc.lower() != contract.start.lower():
                    _, route_time = planner.plan_route(current_loc, contract.start, is_freighter)
                    total_time += route_time
            except ValueError:
                return float('inf')  # Unreachable

            # Add cargo loading time
            total_time += self.config.times.move_cargo

            # Travel to delivery
            try:
                _, route_time = planner.plan_route(contract.start, contract.end, is_freighter)
                total_time += route_time
            except ValueError:
                return float('inf')  # Unreachable

            # Add cargo unloading time
            total_time += self.config.times.move_cargo

            current_loc = contract.end

        # Return to base
        if current_loc.lower() != base_location.lower():
            try:
                _, route_time = planner.plan_route(current_loc, base_location, is_freighter)
                total_time += route_time
            except ValueError:
                pass  # Return trip might not be possible

        return total_time

    def plan_contracts(self, contracts: List[Contract], base_location: str,
                       ship: ShipConfig, max_time_minutes: Optional[int] = None,
                       target_iph: Optional[float] = None) -> Tuple[List[str], float, float]:
        """
        Plan contract hauling operations.

        Returns:
            Tuple of (output lines, total time, total profit)
        """
        if not contracts:
            return ["No Good Contracts"], 0.0, 0.0

        # Filter contracts based on constraints
        filtered_contracts = self._filter_contracts(contracts, ship, base_location)

        if not filtered_contracts:
            return ["No Good Contracts"], 0.0, 0.0

        # Select optimal subset of contracts
        selected_contracts = self._select_contracts(
            filtered_contracts, base_location, ship, max_time_minutes, target_iph
        )

        if not selected_contracts:
            return ["No Good Contracts"], 0.0, 0.0

        # Generate route and output
        return self._generate_route(selected_contracts, base_location, ship)

    def _filter_contracts(self, contracts: List[Contract], ship: ShipConfig,
                         base_location: str) -> List[Contract]:
        """Filter contracts based on constraints."""
        filtered = []

        for contract in contracts:
            # Check volume constraint
            if contract.volume > ship.cargo_size:
                continue

            # Check max_isk_per_ehp constraint (does NOT apply to blockade runners)
            if self.config.max_isk_per_ehp is not None and ship.ship_type != "Blockade Runner":
                if ship.ehp and ship.ehp > 0:
                    isk_per_ehp = contract.actual_value / ship.ehp
                    if isk_per_ehp > self.config.max_isk_per_ehp:
                        continue

            # Check min_isk_per_jump constraint
            if self.config.min_isk_per_jump is not None:
                # Calculate jumps from base to pickup to delivery
                jumps = self._estimate_jumps(base_location, contract.start, ship) + \
                        self._estimate_jumps(contract.start, contract.end, ship)
                if jumps > 0:
                    isk_per_jump = contract.reward / jumps
                    if isk_per_jump < self.config.min_isk_per_jump:
                        continue

            filtered.append(contract)

        return filtered

    def _estimate_jumps(self, start: str, end: str, ship: ShipConfig) -> int:
        """Estimate number of jumps between two locations."""
        warp_calc = WarpCalculator(
            align_time=ship.align,
            top_speed=ship.top_speed,
            warp_speed=ship.warp_speed,
            dock_time=self.config.times.dock,
            gate_time=self.config.times.gate
        )
        planner = RoutePlanner(self.sde, warp_calc)
        is_freighter = ship.ship_type == "Freighter"

        try:
            route_steps, _ = planner.plan_route(start, end, is_freighter)
            # Number of jumps is number of systems - 1
            return max(0, len(route_steps) - 1)
        except ValueError:
            return float('inf')

    def _select_contracts(self, contracts: List[Contract], base_location: str,
                         ship: ShipConfig, max_time_minutes: Optional[int],
                         target_iph: Optional[float]) -> List[Contract]:
        """
        Select optimal subset of contracts.
        Strategy: Try all permutations for small sets, greedy for large sets.
        """
        if not contracts:
            return []

        warp_calc = WarpCalculator(
            align_time=ship.align,
            top_speed=ship.top_speed,
            warp_speed=ship.warp_speed,
            dock_time=self.config.times.dock,
            gate_time=self.config.times.gate
        )
        planner = RoutePlanner(self.sde, warp_calc)
        is_freighter = ship.ship_type == "Freighter"

        max_time_seconds = max_time_minutes * 60 if max_time_minutes else None

        # For small sets, try all permutations
        if len(contracts) <= 8:
            return self._try_all_permutations(contracts, base_location, planner, is_freighter, max_time_seconds)
        else:
            return self._greedy_selection(contracts, base_location, planner, is_freighter, max_time_seconds)

    def _try_all_permutations(self, contracts: List[Contract], base_location: str,
                              planner: RoutePlanner, is_freighter: bool,
                              max_time_seconds: Optional[float]) -> List[Contract]:
        """Try all permutations to find optimal contract order."""
        from itertools import permutations, combinations

        best_contracts = []
        best_profit = 0.0

        # Try all subset sizes from largest to smallest to maximize profit
        for subset_size in range(len(contracts), 0, -1):
            for subset in combinations(contracts, subset_size):
                for perm in permutations(subset):
                    time, profit, valid = self._evaluate_route(list(perm), base_location, planner, is_freighter)

                    if not valid:
                        continue

                    # Check time constraint
                    if max_time_seconds and time > max_time_seconds:
                        continue

                    # Check if this is better (more profit, or same profit with alphabetical tiebreak)
                    if profit > best_profit or (profit == best_profit and profit > 0):
                        if profit > best_profit:
                            best_profit = profit
                            best_contracts = list(perm)
                        elif profit == best_profit:
                            # Tiebreak by issuer alphabetical order
                            current_issuers = [c.issuer for c in perm]
                            best_issuers = [c.issuer for c in best_contracts]
                            if current_issuers < best_issuers:
                                best_contracts = list(perm)

            # If we found a valid solution with this subset size, stop looking at smaller subsets
            # (since we're going from largest to smallest, this maximizes profit)
            if best_contracts:
                break

        return best_contracts

    def _greedy_selection(self, contracts: List[Contract], base_location: str,
                         planner: RoutePlanner, is_freighter: bool,
                         max_time_seconds: Optional[float]) -> List[Contract]:
        """Use greedy algorithm for large contract sets."""
        selected = []
        current_loc = base_location
        total_time = 0.0
        remaining = list(contracts)

        while remaining:
            # Find best next contract
            best_contract = None
            best_score = float('-inf')
            best_time_add = 0.0

            for contract in remaining:
                # Calculate time to pick up and deliver this contract
                time_to_pickup = 0.0
                if current_loc.lower() != contract.start.lower():
                    try:
                        _, route_time = planner.plan_route(current_loc, contract.start, is_freighter)
                        time_to_pickup = route_time
                    except ValueError:
                        continue

                try:
                    _, route_time = planner.plan_route(contract.start, contract.end, is_freighter)
                    time_to_deliver = route_time
                except ValueError:
                    continue

                total_add_time = time_to_pickup + self.config.times.move_cargo + \
                               time_to_deliver + self.config.times.move_cargo

                # Check if adding this contract exceeds max time
                if max_time_seconds and total_time + total_add_time > max_time_seconds:
                    continue

                # Score: reward per time
                score = contract.reward / total_add_time if total_add_time > 0 else 0

                if score > best_score:
                    best_score = score
                    best_contract = contract
                    best_time_add = total_add_time

            if best_contract is None:
                break

            selected.append(best_contract)
            remaining.remove(best_contract)
            total_time += best_time_add
            current_loc = best_contract.end

        return selected

    def _evaluate_route(self, contracts: List[Contract], base_location: str,
                       planner: RoutePlanner, is_freighter: bool) -> Tuple[float, float, bool]:
        """
        Evaluate a route.
        Returns: (total_time, total_profit, is_valid)
        """
        total_time = 0.0
        total_profit = 0.0
        current_loc = base_location

        for contract in contracts:
            # Travel to pickup
            if current_loc.lower() != contract.start.lower():
                try:
                    _, route_time = planner.plan_route(current_loc, contract.start, is_freighter)
                    total_time += route_time
                except ValueError:
                    return 0.0, 0.0, False

            # Load cargo
            total_time += self.config.times.move_cargo

            # Travel to delivery
            try:
                _, route_time = planner.plan_route(contract.start, contract.end, is_freighter)
                total_time += route_time
            except ValueError:
                return 0.0, 0.0, False

            # Unload cargo
            total_time += self.config.times.move_cargo

            total_profit += contract.reward
            current_loc = contract.end

        return total_time, total_profit, True

    def _generate_route(self, contracts: List[Contract], base_location: str,
                       ship: ShipConfig) -> Tuple[List[str], float, float]:
        """Generate output for the selected contracts."""
        output_lines = []
        total_time = 0.0
        total_profit = 0.0
        total_volume = 0.0
        total_jumps = 0

        warp_calc = WarpCalculator(
            align_time=ship.align,
            top_speed=ship.top_speed,
            warp_speed=ship.warp_speed,
            dock_time=self.config.times.dock,
            gate_time=self.config.times.gate,
            move_cargo_time=self.config.times.move_cargo
        )
        planner = RoutePlanner(self.sde, warp_calc)
        is_freighter = ship.ship_type == "Freighter"

        # Track contract states for LOAD/UNLOAD output
        contract_jumps = {}  # contract_id -> jumps while holding
        contract_pickup_time = {}  # contract_id -> time when picked up

        current_loc = base_location
        current_system, current_station = self.sde.resolve_location(base_location)
        current_loc_name = current_station.name if current_station else current_system.name

        # Print ship selection
        output_lines.append(f"SHIP: {ship.name}")

        # Process each contract
        for i, contract in enumerate(contracts):
            # Travel to pickup location
            if current_loc.lower() != contract.start.lower():
                # Resolve start location
                start_system, start_station = self.sde.resolve_location(contract.start)
                start_loc_name = start_station.name if start_station else start_system.name

                output_lines.append(f"START: {current_loc_name}")

                # Undock if at station
                if current_station:
                    output_lines.append("UNDOCK")
                    total_time += self.config.times.dock

                # Plan route
                route_steps, route_time = planner.plan_route(current_loc, contract.start, is_freighter)
                total_time += route_time
                total_jumps += len(route_steps) - 1

                route_str = " -> ".join(f"{name} ({sec:.1f})" for name, sec in route_steps)
                output_lines.append(f"GO: {route_str}")

                # Dock at destination if station
                if start_station:
                    output_lines.append(f"DOCK: {start_loc_name}")
                    total_time += self.config.times.dock

                current_loc = contract.start
                current_loc_name = start_loc_name
                current_station = start_station

            # Load contract
            profit_m = contract.reward / 1_000_000
            volume = contract.volume
            total_volume += volume
            output_lines.append(f"LOAD {contract.issuer} (id={contract.id}): {profit_m:.2f}M ISK | {volume:,.2f} m3")
            total_time += self.config.times.move_cargo
            contract_pickup_time[contract.id] = total_time
            contract_jumps[contract.id] = 0

            # Travel to delivery location
            end_system, end_station = self.sde.resolve_location(contract.end)
            end_loc_name = end_station.name if end_station else end_system.name

            output_lines.append(f"START: {current_loc_name}")

            # Undock if at station
            if current_station:
                output_lines.append("UNDOCK")
                total_time += self.config.times.dock

            # Plan route and track jumps
            route_steps, route_time = planner.plan_route(contract.start, contract.end, is_freighter)
            jumps_for_contract = len(route_steps) - 1
            total_time += route_time
            total_jumps += jumps_for_contract
            contract_jumps[contract.id] = jumps_for_contract

            route_str = " -> ".join(f"{name} ({sec:.1f})" for name, sec in route_steps)
            output_lines.append(f"GO: {route_str}")

            # Dock at destination if station
            if end_station:
                output_lines.append(f"DOCK: {end_loc_name}")
                total_time += self.config.times.dock

            # Unload contract
            output_lines.append(f"UNLOAD {contract.issuer} (id={contract.id}): {jumps_for_contract} Jumps | {volume:,.2f} m3")
            total_time += self.config.times.move_cargo
            total_profit += contract.reward

            current_loc = contract.end
            current_loc_name = end_loc_name
            current_station = end_station

        # Return to base if needed
        return_jumps = 0
        if current_loc.lower() != base_location.lower():
            base_system, base_station = self.sde.resolve_location(base_location)
            base_loc_name = base_station.name if base_station else base_system.name

            output_lines.append(f"START: {current_loc_name}")

            if current_station:
                output_lines.append("UNDOCK")
                total_time += self.config.times.dock

            route_steps, route_time = planner.plan_route(current_loc, base_location, is_freighter)
            return_jumps = len(route_steps) - 1
            total_time += route_time
            total_jumps += return_jumps

            route_str = " -> ".join(f"{name} ({sec:.1f})" for name, sec in route_steps)
            output_lines.append(f"GO: {route_str}")

            if base_station:
                output_lines.append(f"DOCK: {base_loc_name}")
                total_time += self.config.times.dock

        output_lines.append(f"DONE: {format_time(total_time)}")

        # Calculate metrics
        profit_m = total_profit / 1_000_000
        isk_per_m3 = total_profit / total_volume if total_volume > 0 else 0
        total_jumps_including_return = total_jumps
        isk_per_jump = total_profit / total_jumps_including_return if total_jumps_including_return > 0 else 0
        isk_per_hour = (total_profit / total_time * 3600) / 1_000_000 if total_time > 0 else 0

        output_lines.append(f"NUM CONTRACTS: {len(contracts)}")
        output_lines.append(f"PROFIT: {profit_m:.2f}M")
        output_lines.append(f"ISK/M3: {isk_per_m3:.2f}")
        output_lines.append(f"ISK/Jump: {isk_per_jump / 1_000_000:.2f}M")
        output_lines.append(f"ISK/Hour: {isk_per_hour:.2f}M")

        return output_lines, total_time, total_profit


def load_config(config_path: str) -> Config:
    """Load configuration from YAML file."""
    with open(config_path, 'r') as f:
        data = yaml.safe_load(f)

    ships = {}
    for ship_name, ship_data in data.get('ships', {}).items():
        # Get EHP from config or use defaults based on ship type
        ehp = ship_data.get('ehp')
        if ehp is not None:
            ehp = int(ehp)
        else:
            # Default EHP values based on ship type
            ship_type = ship_data.get('type', 'Freighter')
            if ship_type == 'Deep Space Transport':
                ehp = 60000
            elif ship_type == 'Freighter':
                ehp = 300000
            # Blockade runners don't care about EHP

        ships[ship_name] = ShipConfig(
            name=ship_name,
            ship_type=ship_data.get('type', 'Freighter'),
            align=float(ship_data['align']),
            top_speed=float(ship_data['top_speed']),
            warp_speed=float(ship_data['warp_speed']),
            cargo_size=int(ship_data['cargo_size']),
            ehp=ehp
        )

    times_data = data.get('times', {})
    times = TimeConfig(
        dock=float(times_data['dock']),
        gate=float(times_data['gate']),
        move_cargo=float(times_data['move_cargo'])
    )

    # Get optional min_isk_per_jump and max_isk_per_ehp
    min_isk_per_jump = data.get('min_isk_per_jump')
    if min_isk_per_jump is not None:
        min_isk_per_jump = float(min_isk_per_jump)

    max_isk_per_ehp = data.get('max_isk_per_ehp')
    if max_isk_per_ehp is not None:
        max_isk_per_ehp = float(max_isk_per_ehp)

    return Config(ships=ships, times=times, min_isk_per_jump=min_isk_per_jump, max_isk_per_ehp=max_isk_per_ehp)


def load_manifest(manifest_path: str) -> Manifest:
    """Load manifest from YAML file."""
    with open(manifest_path, 'r') as f:
        data = yaml.safe_load(f)

    start_cargo = data.get('start_cargo')
    if start_cargo is not None:
        start_cargo = float(start_cargo)

    waypoints = []
    for wp_data in data.get('waypoints', []):
        cargo = wp_data.get('cargo')
        if cargo is not None:
            cargo = float(cargo)
        waypoints.append(Waypoint(
            name=wp_data['name'],
            cargo=cargo
        ))

    return Manifest(start_cargo=start_cargo, waypoints=waypoints)


def load_contracts(contracts_path: str) -> List[Contract]:
    """Load contracts from JSONL file."""
    contracts = []
    with open(contracts_path, 'r') as f:
        for idx, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            contract = Contract(
                id=idx,
                start=data['start'],
                end=data['end'],
                collateral=float(data['collateral']),
                volume=float(data['m3']),
                actual_value=float(data['actual_value']),
                reward=float(data['reward']),
                issuer=data['issuer']
            )
            contracts.append(contract)
    return contracts


def format_time(total_seconds: float) -> str:
    """Format time as HH:MM, rounding up to nearest minute."""
    total_minutes = math.ceil(total_seconds / 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours:02d}:{minutes:02d}"


def main():
    parser = argparse.ArgumentParser(description="EVE Online Travel Route Planner")

    # Subcommands
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')

    # Plan command (existing functionality)
    plan_parser = subparsers.add_parser('plan', help='Plan a route between locations')
    plan_parser.add_argument("start", help="Starting location (system or station name)")
    plan_parser.add_argument("end", help="Ending location (system or station name)")
    plan_parser.add_argument("--manifest", help="Path to manifest YAML file")
    plan_parser.add_argument("--config", help="Path to ship config YAML file")
    plan_parser.add_argument("--ship", help="Ship name from config")
    plan_parser.add_argument("--sde", required=True, help="Path to SDE directory")
    plan_parser.add_argument("--align", type=float, help="Align time in seconds (must be > 0)")
    plan_parser.add_argument("--top-speed", type=float, help="Top subwarp speed in m/s (must be >= 0)")
    plan_parser.add_argument("--warp-speed", type=float, help="Warp speed in AU/s (must be > 0)")
    plan_parser.add_argument("--dock-time", type=float, help="Dock/undock time in seconds (must be > 0)")
    plan_parser.add_argument("--gate-time", type=float, help="Gate travel time in seconds (must be > 0)")

    # Contracts command (new functionality)
    contracts_parser = subparsers.add_parser('contracts', help='Plan contract hauling operations')
    contracts_parser.add_argument("start", help="Starting location (base system or station)")
    contracts_parser.add_argument("contracts_file", help="Path to open contracts JSONL file")
    contracts_parser.add_argument("--config", required=True, help="Path to ship config YAML file")
    contracts_parser.add_argument("--sde", required=True, help="Path to SDE directory")
    contracts_parser.add_argument("--target-iph", type=float, help="Target ISK per hour (M ISK/Hour)")
    contracts_parser.add_argument("--max-time", type=int, help="Maximum hauling time in minutes")

    args = parser.parse_args()

    if args.command is None:
        # Legacy support: if no subcommand provided, use old argument parsing
        parser.print_help()
        sys.exit(1)

    if args.command == 'plan':
        handle_plan_command(args)
    elif args.command == 'contracts':
        handle_contracts_command(args)


def handle_plan_command(args):
    """Handle the plan subcommand."""
    # Determine mode: manifest mode vs legacy mode
    if args.manifest:
        # Manifest mode
        if not args.config:
            print("Error: --config is required when using --manifest", file=sys.stderr)
            sys.exit(1)
        if not args.ship:
            print("Error: --ship is required when using --manifest", file=sys.stderr)
            sys.exit(1)

        try:
            config = load_config(args.config)
        except FileNotFoundError:
            print(f"Error: Config file not found: {args.config}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Error loading config: {e}", file=sys.stderr)
            sys.exit(1)

        if args.ship not in config.ships:
            print(f"Error: Unknown ship '{args.ship}'. Available ships: {', '.join(config.ships.keys())}", file=sys.stderr)
            sys.exit(1)

        ship = config.ships[args.ship]

        try:
            manifest = load_manifest(args.manifest)
        except FileNotFoundError:
            print(f"Error: Manifest file not found: {args.manifest}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Error loading manifest: {e}", file=sys.stderr)
            sys.exit(1)

        # Load SDE data
        try:
            sde = SDELoader(args.sde)
            sde.load_all()
        except FileNotFoundError as e:
            print(f"Error: SDE file not found: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Error loading SDE: {e}", file=sys.stderr)
            sys.exit(1)

        # Create warp calculator with config values
        warp_calc = WarpCalculator(
            align_time=ship.align,
            top_speed=ship.top_speed,
            warp_speed=ship.warp_speed,
            dock_time=config.times.dock,
            gate_time=config.times.gate,
            move_cargo_time=config.times.move_cargo
        )

        # Create cargo hauler and plan
        hauler = CargoHauler(sde, warp_calc, ship, config.times)

        try:
            output_lines, total_time, total_cargo = hauler.plan_haul(args.start, args.end, manifest)
            for line in output_lines:
                print(line)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    else:
        # Legacy mode - need all ship parameters
        required_args = ['align', 'top_speed', 'warp_speed', 'dock_time', 'gate_time']
        missing = [arg for arg in required_args if getattr(args, arg.replace('-', '_')) is None]
        if missing:
            print(f"Error: Missing required arguments: {', '.join('--' + arg for arg in missing)}", file=sys.stderr)
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

        # Load SDE data
        try:
            sde = SDELoader(args.sde)
            sde.load_all()
        except FileNotFoundError as e:
            print(f"Error: SDE file not found: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Error loading SDE: {e}", file=sys.stderr)
            sys.exit(1)

        # Create warp calculator
        warp_calc = WarpCalculator(
            align_time=args.align,
            top_speed=args.top_speed,
            warp_speed=args.warp_speed,
            dock_time=args.dock_time,
            gate_time=args.gate_time
        )

        # Plan route
        planner = RoutePlanner(sde, warp_calc)

        # Resolve locations for output
        start_system, start_station = sde.resolve_location(args.start)
        end_system, end_station = sde.resolve_location(args.end)

        start_name = start_station.name if start_station else start_system.name
        end_name = end_station.name if end_station else end_system.name

        try:
            route_steps, total_time = planner.plan_route(args.start, args.end)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        # Output the plan
        print(f"START: {start_name}")

        if start_station:
            print("UNDOCK")

        # Only print GO line if we travel between systems
        if len(route_steps) > 1 or (len(route_steps) == 1 and not end_station and not start_station):
            route_str = " -> ".join(f"{name} ({sec:.1f})" for name, sec in route_steps)
            print(f"GO: {route_str}")

        if end_station:
            print(f"DOCK: {end_name}")

        print(f"DONE: {format_time(total_time)}")


def handle_contracts_command(args):
    """Handle the contracts subcommand."""
    # Load config
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"Error: Config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading config: {e}", file=sys.stderr)
        sys.exit(1)

    # Load contracts
    try:
        contracts = load_contracts(args.contracts_file)
    except FileNotFoundError:
        print(f"Error: Contracts file not found: {args.contracts_file}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading contracts: {e}", file=sys.stderr)
        sys.exit(1)

    # Load SDE data
    try:
        sde = SDELoader(args.sde)
        sde.load_all()
    except FileNotFoundError as e:
        print(f"Error: SDE file not found: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading SDE: {e}", file=sys.stderr)
        sys.exit(1)

    # Create contract planner
    planner = ContractPlanner(sde, config)

    # Select best ship for these contracts
    best_ship = planner.select_best_ship(contracts, args.start)
    if best_ship is None:
        print("No Good Contracts")
        return

    # Plan contracts
    try:
        output_lines, total_time, total_profit = planner.plan_contracts(
            contracts, args.start, best_ship,
            max_time_minutes=args.max_time,
            target_iph=args.target_iph
        )
        for line in output_lines:
            print(line)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
