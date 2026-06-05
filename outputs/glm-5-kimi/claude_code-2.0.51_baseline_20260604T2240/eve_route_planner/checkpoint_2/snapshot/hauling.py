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


def load_config(config_path: str) -> Config:
    """Load configuration from YAML file."""
    with open(config_path, 'r') as f:
        data = yaml.safe_load(f)

    ships = {}
    for ship_name, ship_data in data.get('ships', {}).items():
        ships[ship_name] = ShipConfig(
            name=ship_name,
            ship_type=ship_data.get('type', 'Freighter'),
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

    return Config(ships=ships, times=times)


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


def format_time(total_seconds: float) -> str:
    """Format time as HH:MM, rounding up to nearest minute."""
    total_minutes = math.ceil(total_seconds / 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours:02d}:{minutes:02d}"


def main():
    parser = argparse.ArgumentParser(description="EVE Online Travel Route Planner")

    # Positional arguments
    parser.add_argument("start", help="Starting location (system or station name)")
    parser.add_argument("end", help="Ending location (system or station name)")

    # Manifest mode arguments
    parser.add_argument("--manifest", help="Path to manifest YAML file")
    parser.add_argument("--config", help="Path to ship config YAML file")
    parser.add_argument("--ship", help="Ship name from config")
    parser.add_argument("--sde", required=True, help="Path to SDE directory")

    # Legacy arguments (for backward compatibility)
    parser.add_argument("--align", type=float, help="Align time in seconds (must be > 0)")
    parser.add_argument("--top-speed", type=float, help="Top subwarp speed in m/s (must be >= 0)")
    parser.add_argument("--warp-speed", type=float, help="Warp speed in AU/s (must be > 0)")
    parser.add_argument("--dock-time", type=float, help="Dock/undock time in seconds (must be > 0)")
    parser.add_argument("--gate-time", type=float, help="Gate travel time in seconds (must be > 0)")

    args = parser.parse_args()

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


if __name__ == "__main__":
    main()
