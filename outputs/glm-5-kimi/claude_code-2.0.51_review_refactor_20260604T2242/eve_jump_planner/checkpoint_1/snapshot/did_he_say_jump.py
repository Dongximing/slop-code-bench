#!/usr/bin/env python3
"""
Jump Freighter Logistics Planner
Plans optimal jump routes from start to end station.
"""

import argparse
import bz2
import csv
import heapq
import math
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# Constants
LY_IN_METERS = 9_460_730_472_580_800.0
MAX_FATIGUE_MINUTES = 300.0  # 5 hours
MAX_COOLDOWN_MINUTES = 30.0  # 30 minutes
DEFAULT_RANGE = 10
DEFAULT_FUEL = 10000
DEFAULT_REDUCTION = 90


@dataclass
class System:
    """Represents a solar system."""
    system_id: int
    name: str
    x: float
    y: float
    z: float
    security: float
    region_id: int
    constellation_id: int


@dataclass
class Station:
    """Represents a station."""
    station_id: int
    name: str
    system_id: int


class SDELoader:
    """Loads and parses EVE Static Data Export."""

    def __init__(self, sde_dir: str):
        self.sde_dir = sde_dir
        self.systems: Dict[int, System] = {}
        self.stations: Dict[int, Station] = {}
        self.station_names: Dict[str, int] = {}
        self.system_names: Dict[str, int] = {}
        self.system_stations: Dict[int, List[Station]] = {}

    def load_all(self):
        """Load all SDE data."""
        self._load_systems()
        self._load_stations()

    def _load_systems(self):
        """Load solar systems from mapSolarSystems.csv.bz2."""
        filepath = os.path.join(self.sde_dir, 'mapSolarSystems.csv.bz2')
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                system_id = int(row['solarSystemID'])
                system = System(
                    system_id=system_id,
                    name=row['solarSystemName'],
                    x=float(row['x']),
                    y=float(row['y']),
                    z=float(row['z']),
                    security=float(row['security']),
                    region_id=int(row['regionID']),
                    constellation_id=int(row['constellationID'])
                )
                self.systems[system_id] = system
                self.system_names[row['solarSystemName'].lower()] = system_id

    def _load_stations(self):
        """Load stations from staStations.csv.bz2."""
        filepath = os.path.join(self.sde_dir, 'staStations.csv.bz2')
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                station_id = int(row['stationID'])
                station = Station(
                    station_id=station_id,
                    name=row['stationName'],
                    system_id=int(row['solarSystemID'])
                )
                self.stations[station_id] = station
                self.station_names[row['stationName'].lower()] = station_id

                # Index by system
                if station.system_id not in self.system_stations:
                    self.system_stations[station.system_id] = []
                self.system_stations[station.system_id].append(station)

    def get_station_by_name(self, name: str) -> Optional[Station]:
        """Get station by name (case-insensitive)."""
        station_id = self.station_names.get(name.lower())
        if station_id:
            return self.stations[station_id]
        return None

    def get_system_by_name(self, name: str) -> Optional[System]:
        """Get system by name (case-insensitive)."""
        system_id = self.system_names.get(name.lower())
        if system_id:
            return self.systems[system_id]
        return None

    def get_system(self, system_id: int) -> Optional[System]:
        """Get system by ID."""
        return self.systems.get(system_id)

    def get_station(self, station_id: int) -> Optional[Station]:
        """Get station by ID."""
        return self.stations.get(station_id)

    def get_stations_in_system(self, system_id: int) -> List[Station]:
        """Get all stations in a system."""
        return self.system_stations.get(system_id, [])


def distance_ly(sys1: System, sys2: System) -> float:
    """Calculate distance between two systems in light years using double precision."""
    dx = sys1.x - sys2.x
    dy = sys1.y - sys2.y
    dz = sys1.z - sys2.z
    distance_meters = math.sqrt(dx*dx + dy*dy + dz*dz)
    return distance_meters / LY_IN_METERS


def is_high_sec(system: System) -> bool:
    """Check if system is high security (>=0.5 with rounding)."""
    return system.security >= 0.45


def is_pochven(system: System) -> bool:
    """Check if system is in Pochven region."""
    return system.region_id == 10001000


def is_zarzakh(system: System) -> bool:
    """Check if system is Zarzakh."""
    return system.name.lower() == 'zarzakh'


def is_valid_destination(system: System) -> bool:
    """Check if system is a valid destination (not high sec, pochven, or zarzakh)."""
    return not is_high_sec(system) and not is_pochven(system) and not is_zarzakh(system)


def ceil_to_minutes(value: float) -> int:
    """Ceiling to minutes (round up to nearest minute)."""
    return math.ceil(value)


def ceil_to_thousands(value: float) -> int:
    """Ceiling to thousands (round up to nearest thousand)."""
    return math.ceil(value / 1000.0)


def format_time(minutes: int) -> str:
    """Format minutes as HH:MM with leading zeros."""
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours:02d}:{mins:02d}"


class JumpPlanner:
    """Plans optimal jump freighter routes."""

    def __init__(self, sde: SDELoader, max_range: float = DEFAULT_RANGE,
                 fuel_per_jump: int = DEFAULT_FUEL,
                 reduction: int = DEFAULT_REDUCTION):
        self.sde = sde
        self.max_range = max_range
        self.fuel_per_jump = fuel_per_jump
        # reduction% reduction means we keep (100 - reduction)% of the distance
        # e.g., 90% reduction means effective_distance = 0.1 * d_ly
        self.reduction_factor = (100 - reduction) / 100.0

        # Pre-compute valid systems (those with stations)
        self.valid_systems: List[int] = []
        for sys_id, stations in self.sde.system_stations.items():
            if stations:
                self.valid_systems.append(sys_id)

    def get_best_station(self, system_id: int) -> Optional[Station]:
        """Get the lexicographically first station in a system."""
        stations = self.sde.get_stations_in_system(system_id)
        if not stations:
            return None
        return min(stations, key=lambda s: s.name.lower())

    def find_reachable_systems(self, from_system: System) -> List[Tuple[System, float]]:
        """Find all systems within jump range that have stations."""
        reachable = []
        for sys_id in self.valid_systems:
            if sys_id == from_system.system_id:
                continue
            system = self.sde.get_system(sys_id)
            if not system:
                continue
            dist = distance_ly(from_system, system)
            if 0 < dist <= self.max_range:
                reachable.append((system, dist))
        return reachable

    def calculate_isotopes(self, distance: float) -> int:
        """Calculate isotopes needed for a jump (ceiling to thousands)."""
        return ceil_to_thousands(self.fuel_per_jump * distance)

    def calculate_fatigue_cooldown(self, path: List[Tuple[int, float]]) -> Tuple[float, float, float]:
        """
        Calculate total fatigue, cooldown, and waiting time for a path.

        Fatigue decays while waiting for cooldown.

        Returns: (end_fatigue, end_cooldown, total_time_waiting)
        """
        current_fatigue = 0.0
        current_cooldown = 0.0
        total_time_waiting = 0.0

        for sys_id, dist in path:
            effective_dist = dist * self.reduction_factor

            # If cooldown > 0, we need to wait
            if current_cooldown > 0:
                # Wait time is ceiling of cooldown
                wait_time = ceil_to_minutes(current_cooldown)
                total_time_waiting += wait_time

                # Fatigue decays by the cooldown amount (not the wait time)
                current_fatigue = max(0.0, current_fatigue - current_cooldown)
                current_cooldown = 0.0

            # Calculate new fatigue and cooldown after jump
            if current_fatigue == 0.0:
                # First jump
                new_fatigue = 10.0 * (1.0 + effective_dist)
                new_cooldown = 1.0 + effective_dist
            else:
                # Subsequent jumps
                base_fatigue = max(current_fatigue, 10.0)
                new_fatigue = base_fatigue * (1.0 + effective_dist)
                new_cooldown = new_fatigue / 10.0

            # Cap at maximums
            new_fatigue = min(new_fatigue, MAX_FATIGUE_MINUTES)
            new_cooldown = min(new_cooldown, MAX_COOLDOWN_MINUTES)

            # Cannot be below zero
            new_fatigue = max(new_fatigue, 0.0)
            new_cooldown = max(new_cooldown, 0.0)

            current_fatigue = new_fatigue
            current_cooldown = new_cooldown

        return current_fatigue, current_cooldown, total_time_waiting

    def plan_route(self, start_station: Station, end_station: Station) -> Optional[dict]:
        """
        Plan the optimal route from start to end station.

        Priority order for route selection:
        1. Min Jumps
        2. Min Time Waiting
        3. Total Trip LY
        4. Lexicographic sorting of intermediate system names
        """
        start_system = self.sde.get_system(start_station.system_id)
        end_system = self.sde.get_system(end_station.system_id)

        if not start_system or not end_system:
            return None

        # Check if end system is valid
        if not is_valid_destination(end_system):
            raise ValueError(f"End station '{end_station.name}' is in a high sec, Pochven, or Zarzakh system")

        # BFS to find minimum jump routes
        min_jumps = self._find_min_jumps(start_system.system_id, end_system.system_id)
        if min_jumps is None:
            return None

        # Now find the best route with exactly min_jumps jumps
        result = self._find_best_route_with_jumps(
            start_system.system_id,
            end_system.system_id,
            min_jumps
        )

        if result:
            return self._build_result(result, start_station, end_station)

        return None

    def _find_min_jumps(self, start_id: int, end_id: int) -> Optional[int]:
        """Find minimum number of jumps using BFS."""
        from collections import deque

        visited = {start_id}
        queue = deque([(start_id, 0)])

        while queue:
            sys_id, jumps = queue.popleft()

            if sys_id == end_id:
                return jumps

            system = self.sde.get_system(sys_id)
            if not system:
                continue

            for next_sys_id in self.valid_systems:
                if next_sys_id in visited:
                    continue
                next_sys = self.sde.get_system(next_sys_id)
                if not next_sys:
                    continue
                dist = distance_ly(system, next_sys)
                if 0 < dist <= self.max_range:
                    visited.add(next_sys_id)
                    queue.append((next_sys_id, jumps + 1))

        return None

    def _find_best_route_with_jumps(self, start_id: int, end_id: int,
                                     target_jumps: int) -> Optional[List[Tuple[int, float]]]:
        """Find the best route with exactly target_jumps jumps."""
        # Use Dijkstra-like search with proper priority
        # Priority: (jumps, time_waiting, total_ly_rounded, system_name)

        initial_state = (start_id, 0, [])

        # Priority queue: (jumps, time_waiting, total_ly_rounded, system_name, state)
        queue = [(0, 0.0, 0.0, "", initial_state)]

        # Track best state per (system_id, jumps)
        best_states: Dict[Tuple[int, int], Tuple[float, float]] = {}

        while queue:
            priority_jumps, priority_tw, priority_ly_r, priority_name, state = heapq.heappop(queue)
            current_id, jumps_so_far, path = state

            # Check if reached destination
            if current_id == end_id and jumps_so_far == target_jumps:
                return path

            # Skip if exceeded target jumps
            if jumps_so_far > target_jumps:
                continue

            # Check if we've seen this state with better stats
            state_key = (current_id, jumps_so_far)
            if state_key in best_states:
                best_tw, best_ly = best_states[state_key]
                if best_tw < priority_tw or (best_tw == priority_tw and best_ly <= priority_ly_r):
                    continue

            best_states[state_key] = (priority_tw, priority_ly_r)

            # Expand
            current_system = self.sde.get_system(current_id)
            if not current_system:
                continue

            reachable = self.find_reachable_systems(current_system)

            for next_system, dist in reachable:
                new_jumps = jumps_so_far + 1
                if new_jumps > target_jumps:
                    continue

                new_path = path + [(next_system.system_id, dist)]
                new_total_ly = sum(d for _, d in new_path)
                new_total_ly_rounded = round(new_total_ly, 2)

                # Calculate time waiting for this new path
                _, _, new_time_waiting = self.calculate_fatigue_cooldown(new_path)

                heapq.heappush(queue, (
                    new_jumps,
                    new_time_waiting,
                    new_total_ly_rounded,
                    next_system.name.lower(),
                    (next_system.system_id, new_jumps, new_path)
                ))

        return None

    def _build_result(self, path: List[Tuple[int, float]],
                      start_station: Station, end_station: Station) -> dict:
        """Build the result dictionary from the path."""
        total_ly = sum(dist for _, dist in path)
        total_isotopes = sum(self.calculate_isotopes(dist) for _, dist in path)

        end_fatigue, end_cooldown, time_waiting = self.calculate_fatigue_cooldown(path)

        jumps = []
        for system_id, dist in path:
            station = self.get_best_station(system_id)
            jumps.append((system_id, dist, station.station_id if station else 0))

        return {
            'jumps': jumps,
            'total_ly': total_ly,
            'total_isotopes': total_isotopes,
            'end_fatigue': end_fatigue,
            'end_cooldown': end_cooldown,
            'time_waiting': time_waiting
        }


def main():
    parser = argparse.ArgumentParser(description='Jump Freighter Logistics Planner')
    parser.add_argument('--start', required=True, help='Start station name')
    parser.add_argument('--end', required=True, help='End station name')
    parser.add_argument('--sde', required=True, help='Path to SDE directory')
    parser.add_argument('--range', type=int, default=DEFAULT_RANGE,
                       help=f'Max LY range for a jump (5-10), default {DEFAULT_RANGE}')
    parser.add_argument('--fuel', type=int, default=DEFAULT_FUEL,
                       help=f'Isotopes per jump (1-10000), default {DEFAULT_FUEL}')
    parser.add_argument('--reduction', type=int, default=DEFAULT_REDUCTION,
                       help=f'Effective jump distance reduction (0-100), default {DEFAULT_REDUCTION}')

    args = parser.parse_args()

    # Validate arguments
    if args.range < 5 or args.range > 10:
        print(f"Error: --range must be between 5 and 10, got {args.range}", file=sys.stderr)
        sys.exit(1)

    if args.fuel < 1 or args.fuel > 10000:
        print(f"Error: --fuel must be between 1 and 10000, got {args.fuel}", file=sys.stderr)
        sys.exit(1)

    if args.reduction < 0 or args.reduction > 100:
        print(f"Error: --reduction must be between 0 and 100, got {args.reduction}", file=sys.stderr)
        sys.exit(1)

    # Load SDE
    try:
        sde = SDELoader(args.sde)
        sde.load_all()
    except Exception as e:
        print(f"Error loading SDE: {e}", file=sys.stderr)
        sys.exit(1)

    # Find stations
    start_station = sde.get_station_by_name(args.start)
    if not start_station:
        print(f"Error: Start station '{args.start}' not found", file=sys.stderr)
        sys.exit(1)

    end_station = sde.get_station_by_name(args.end)
    if not end_station:
        print(f"Error: End station '{args.end}' not found", file=sys.stderr)
        sys.exit(1)

    # Plan route
    planner = JumpPlanner(sde, args.range, args.fuel, args.reduction)

    try:
        route = planner.plan_route(start_station, end_station)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not route:
        print("Error: No valid route found", file=sys.stderr)
        sys.exit(1)

    # Output result
    print(f"START: {start_station.name}")

    for system_id, distance, station_id in route['jumps']:
        system = sde.get_system(system_id)
        station = sde.get_station(station_id)

        isotopes = planner.calculate_isotopes(distance)

        print("UNDOCK")
        print(f"JUMP {distance:.2f} LY: {system.name} ({isotopes}K isotopes)")
        print(f"DOCK: {station.name}")

    # Summary
    end_cooldown_min = ceil_to_minutes(route['end_cooldown'])
    end_fatigue_min = ceil_to_minutes(route['end_fatigue'])
    time_waiting_min = ceil_to_minutes(route['time_waiting'])

    print("SUMMARY:")
    print(f"  End Cooldown: {format_time(end_cooldown_min)}")
    print(f"  End Fatigue: {format_time(end_fatigue_min)}")
    print(f"  Isotopes Used: {route['total_isotopes']}K")
    print(f"  Time Waiting: {format_time(time_waiting_min)}")
    print(f"  Total LY: {route['total_ly']:.2f}")


if __name__ == '__main__':
    main()
