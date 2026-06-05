#!/usr/bin/env python3
"""
EVE Online Travel Route Planner

Plans routes through New Eden using the EVE Static Data Export (SDE).
"""

import argparse
import bz2
import csv
import heapq
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

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
                type_id = int(row["typeID"]) if row["typeID"] and row["typeID"] != "None" else 0
                # typeID for stargates varies, but groupID is typically used
                # Stargates typically have groupID = 10 for constellation gates
                # or groupID = 57 for region gates, but we need to check the data
                # For this implementation, we'll look for items with "Star" in name
                # or check typical gate type IDs (various values)
                group_id = int(row["groupID"]) if row["groupID"] and row["groupID"] != "None" else 0

                # Stargates typically have groupID 10 (stargate)
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


class WarpCalculator:
    """Calculates warp times based on EVE mechanics."""

    def __init__(self, align_time: float, top_speed: float, warp_speed: float,
                 dock_time: float, gate_time: float):
        self.align_time = math.ceil(align_time)  # Ceiling due to server ticks
        self.top_speed = top_speed  # m/s
        self.warp_speed = warp_speed  # AU/s
        self.dock_time = dock_time
        self.gate_time = gate_time

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

    def plan_route(self, start_name: str, end_name: str) -> Tuple[List[Tuple[str, float, Optional[float]]], float]:
        """
        Plan a route from start to end.

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

        # Find the shortest path considering Zarzakh constraints
        path, total_time = self._find_path(start_system_id, end_system_id,
                                           start_station, end_station)

        # Build route output
        route_steps = []
        for sys_id in path:
            sys = self.sde.systems[sys_id]
            route_steps.append((sys.name, sys.security))

        return route_steps, total_time

    def _find_path(self, start_id: int, end_id: int,
                   start_station: Optional[Station],
                   end_station: Optional[Station) -> Tuple[List[int], float]:
        """
        Find shortest path using Dijkstra's algorithm with Zarzakh constraints.

        The Zarzakh constraint: when entering Zarzakh, you're locked to that gate
        for 6 hours. You can only leave back to where you came from.
        """
        # State: (current_system, came_from_zarzakh_via)
        # came_from_zarzakh_via is None if not in Zarzakh, or the system we entered from
        # If we're in Zarzakh, we can only go back to came_from_zarzakh_via

        # Priority queue: (time, current_system, came_from_zarzakh_via, path)
        # Using a dict to track best times to states
        # State key: (system_id, zarzakh_entry_system or None)

        initial_state = (start_id, None)

        # Calculate initial undock time if starting from station
        undock_time = self.warp_calc.dock_time if start_station else 0.0

        # Get starting position
        if start_station:
            start_pos = Location(
                system_id=start_id,
                station_id=start_station.id,
                x=start_station.x,
                y=start_station.y,
                z=start_station.z
            )
        else:
            # Starting in space - pick lexicographically first gate
            gates = self.sde.stargates.get(start_id, [])
            if gates:
                first_gate = min(gates, key=lambda g: g.name)
                start_pos = Location(
                    system_id=start_id,
                    gate_id=first_gate.id,
                    x=first_gate.x,
                    y=first_gate.y,
                    z=first_gate.z
                )
            else:
                # Use system center if no gates
                sys = self.sde.systems[start_id]
                start_pos = Location(
                    system_id=start_id,
                    x=sys.x,
                    y=sys.y,
                    z=sys.z
                )

        # If same system, handle intra-system travel
        if start_id == end_id:
            path = [start_id]
            time = self._calculate_intra_system_time(start_id, start_station, end_station)
            return path, time

        # Dijkstra's algorithm with state tracking for Zarzakh
        # State: (system_id, zarzakh_entry_point)
        # zarzakh_entry_point is None if not in Zarzakh or didn't just enter Zarzakh

        # For non-Zarzakh systems, state is just (system_id, None)
        # For Zarzakh, state is (ZARZAKH_SYSTEM_ID, entry_system_id)

        best_times: Dict[Tuple[int, Optional[int]], float] = {}
        best_paths: Dict[Tuple[int, Optional[int]], List[int]] = {}

        pq: List[Tuple[float, int, Optional[int], List[int]]] = []

        # Initial state
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

    def _calculate_system_travel_time(self, from_sys: int, to_sys: int,
                                      is_first_warp: bool) -> float:
        """
        Calculate time to travel from one system to another.

        This includes:
        - Align time (before warp)
        - Warp time (from current position to gate)
        - Gate time (jump through)
        """
        total_time = 0.0

        # Add align time
        total_time += self.warp_calc.align_time

        # Calculate warp time from position to gate
        # We need the distance from our current position to the gate

        # Get position in from_sys (we're at a gate after previous jump)
        gates = self.sde.stargates.get(from_sys, [])
        if gates:
            # We're at a gate - use first gate position (approximation)
            # In reality, we'd need to know which specific gate, but for simplicity
            # we use the system center for intra-system warps
            sys_from = self.sde.systems[from_sys]
            # Distance from system center to gate (approximation)
            # Use average gate distance or just assume warp from center
            avg_dist = sum(math.sqrt(g.x**2 + g.y**2 + g.z**2) for g in gates) / len(gates)
            warp_dist = avg_dist  # Simplified - actual would depend on specific gates
        else:
            warp_dist = 1.0 * AU_IN_M  # Default 1 AU if no gates

        # Use system center position for warp distance calculation
        # This is a simplification - actual distances would depend on specific gates
        sys_from = self.sde.systems[from_sys]
        sys_to = self.sde.systems[to_sys]

        # Distance between systems (for inter-system warp)
        # Actually, we warp TO a gate, so we need the gate position in from_sys
        # that connects to to_sys

        # Find the gate in from_sys that connects to to_sys
        gate_to_next = None
        for gate in self.sde.stargates.get(from_sys, []):
            # Gates connect systems - we need to match by jump data
            # The jump data tells us connections, stargate data tells us positions
            pass

        # For now, use a simplified approach:
        # Assume we warp from system center to a gate at ~10 AU distance
        # This is a rough approximation
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
            # Warp from station to station
            dx = end_station.x - start_station.x
            dy = end_station.y - start_station.y
            dz = end_station.z - start_station.z
            distance = math.sqrt(dx*dx + dy*dy + dz*dz)
        elif start_station:
            # Warp from station to system center (or gate)
            sys = self.sde.systems[system_id]
            dx = sys.x - start_station.x
            dy = sys.y - start_station.y
            dz = sys.z - start_station.z
            distance = math.sqrt(dx*dx + dy*dy + dz*dz)
        elif end_station:
            # Warp from system center (or gate) to station
            sys = self.sde.systems[system_id]
            dx = end_station.x - sys.x
            dy = end_station.y - sys.y
            dz = end_station.z - sys.z
            distance = math.sqrt(dx*dx + dy*dy + dz*dz)
        else:
            # No warp needed - just in system
            distance = 0

        if distance > 0:
            total_time += self.warp_calc.align_time
            total_time += self.warp_calc.calculate_warp_time(distance)

        # Dock time if ending at station
        if end_station:
            total_time += self.warp_calc.dock_time

        return total_time


def format_time(total_seconds: float) -> str:
    """Format time as HH:MM, rounding up to nearest minute."""
    total_minutes = math.ceil(total_seconds / 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours:02d}:{minutes:02d}"


def main():
    parser = argparse.ArgumentParser(description="EVE Online Travel Route Planner")
    parser.add_argument("start", help="Starting location (system or station name)")
    parser.add_argument("end", help="Ending location (system or station name)")
    parser.add_argument("--align", type=float, required=True,
                        help="Align time in seconds (must be > 0)")
    parser.add_argument("--top-speed", type=float, required=True,
                        help="Top subwarp speed in m/s (must be >= 0)")
    parser.add_argument("--warp-speed", type=float, required=True,
                        help="Warp speed in AU/s (must be > 0)")
    parser.add_argument("--dock-time", type=float, required=True,
                        help="Dock/undock time in seconds (must be > 0)")
    parser.add_argument("--gate-time", type=float, required=True,
                        help="Gate travel time in seconds (must be > 0)")
    parser.add_argument("--sde", required=True,
                        help="Path to SDE directory")

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
    sde = SDELoader(args.sde)
    sde.load_all()

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
