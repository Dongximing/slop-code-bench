#!/usr/bin/env python3
"""
Jump Freighter Logistics Planner
Plans JF jumps from start to end station, calculating isotopes needed and fatigue.
"""

import argparse
import csv
import math
import sys
from dataclasses import dataclass
from typing import Optional
import bz2

# Constants
METER_PER_LY = 9_460_730_472_580_800.0  # 1 LY in meters

# Security class for pochven/zarzakh detection
POCHVEN_SECURITY_CLASS = "B"  # Systems with security class B are pochven
ZARAKH_FACTION_ID = 500007  # Faction ID for Zarzakh


@dataclass
class Vector3:
    """3D vector for position calculations."""
    x: float
    y: float
    z: float

    def distance_to(self, other: 'Vector3') -> float:
        """Calculate Euclidean distance."""
        dx = self.x - other.x
        dy = self.y - other.y
        dz = self.z - other.z
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    def distance_to_ly(self, other: 'Vector3') -> float:
        """Calculate distance in light-years."""
        return self.distance_to(other) / METER_PER_LY


@dataclass
class SolarSystem:
    """Represents a solar system from the SDE."""
    system_id: int
    name: str
    position: Vector3
    security: float
    security_class: str
    faction_id: Optional[int]

    def is_high_sec(self) -> bool:
        """Check if system is high security (rounded to 1 decimal)."""
        return round(self.security, 1) >= 0.5

    def is_pochven(self) -> bool:
        """Check if system is pochven (security class B)."""
        return self.security_class == POCHVEN_SECURITY_CLASS

    def is_zarzakh(self) -> bool:
        """Check if system belongs to Zarzakh faction."""
        return self.faction_id == ZARAKH_FACTION_ID

    def is_valid_end(self) -> bool:
        """Check if this system is a valid end destination."""
        return not (self.is_high_sec() or self.is_pochven() or self.is_zarzakh())


@dataclass
class Station:
    """Represents a station from the SDE."""
    station_id: int
    name: str
    solar_system_id: int
    position: Vector3


@dataclass
class JumpResult:
    """Result of a single jump."""
    ly: float
    system: SolarSystem
    station: Optional[Station]
    isotopes: int

    # Timing values (in minutes)
    fatigue_before: float
    cooldown_before: float
    fatigue_after: float
    cooldown_after: float
    wait_time: float


class SDELoader:
    """Loads and indexes data from the Static Data Export (SDE)."""

    def __init__(self, sde_path: str):
        self.sde_path = sde_path
        self.stations_by_name: dict[str, Station] = {}
        self.systems_by_id: dict[int, SolarSystem] = {}
        self.systems_by_name: dict[str, SolarSystem] = {}
        self.jumps: set[tuple[int, int]] = set()  # (from_id, to_id)

        self._load_all_data()

    def _load_all_data(self):
        """Load all SDE data files."""
        self._load_solar_systems()
        self._load_stations()
        self._load_jumps()

    def _load_solar_systems(self):
        """Load solar systems from mapSolarSystems.csv.bz2."""
        filepath = f"{self.sde_path}/mapSolarSystems.csv.bz2"
        with bz2.open(filepath, 'rt') as f:
            reader = csv.DictReader(f)
            for row in reader:
                system = SolarSystem(
                    system_id=int(row['solarSystemID']),
                    name=row['solarSystemName'],
                    position=Vector3(
                        x=float(row['x']),
                        y=float(row['y']),
                        z=float(row['z'])
                    ),
                    security=float(row['security']),
                    security_class=row.get('securityClass', ''),
                    faction_id=int(row['factionID']) if row.get('factionID') else None
                )
                self.systems_by_id[system.system_id] = system
                self.systems_by_name[system.name] = system

    def _load_stations(self):
        """Load stations from staStations.csv.bz2."""
        filepath = f"{self.sde_path}/staStations.csv.bz2"
        with bz2.open(filepath, 'rt') as f:
            reader = csv.DictReader(f)
            for row in reader:
                station = Station(
                    station_id=int(row['stationID']),
                    name=row['stationName'],
                    solar_system_id=int(row['solarSystemID']),
                    position=Vector3(
                        x=float(row['x']),
                        y=float(row['y']),
                        z=float(row['z'])
                    )
                )
                self.stations_by_name[station.name] = station

    def _load_jumps(self):
        """Load jump connections from mapSolarSystemJumps.csv.bz2."""
        filepath = f"{self.sde_path}/mapSolarSystemJumps.csv.bz2"
        with bz2.open(filepath, 'rt') as f:
            reader = csv.DictReader(f)
            for row in reader:
                from_id = int(row['fromSolarSystemID'])
                to_id = int(row['toSolarSystemID'])
                self.jumps.add((from_id, to_id))
                self.jumps.add((to_id, from_id))  # Jumps are bidirectional

    def get_station(self, name: str) -> Optional[Station]:
        """Get a station by name."""
        return self.stations_by_name.get(name)

    def get_system_by_station(self, station: Station) -> Optional[SolarSystem]:
        """Get the solar system for a station."""
        return self.systems_by_id.get(station.solar_system_id)

    def get_system_by_name(self, name: str) -> Optional[SolarSystem]:
        """Get a solar system by name."""
        return self.systems_by_name.get(name)

    def can_jump(self, from_system: SolarSystem, to_system: SolarSystem) -> bool:
        """Check if a jump is possible between two systems."""
        return (from_system.system_id, to_system.system_id) in self.jumps

    def get_jumpable_systems(self, system: SolarSystem) -> list[SolarSystem]:
        """Get all systems reachable by a single jump."""
        result = []
        for from_id, to_id in self.jumps:
            if from_id == system.system_id:
                to_system = self.systems_by_id.get(to_id)
                if to_system:
                    result.append(to_system)
        return result


class JumpPlanner:
   """Plans jump freighter routes with fatigue and cooldown mechanics."""

    def __init__(self, sde: SDELoader, max_range: int, isotopes_per_jump: int, reduction: int):
        self.sde = sde
        self.max_range = max_range
        self.isotopes_per_jump = isotopes_per_jump
        self.reduction = reduction

        # Reduction factor (90% = 0.1)
        self.reduction_factor = reduction / 100.0

    def plan(self, start_station: Station, end_station: Station) -> list[JumpResult]:
        """Plan a route from start to end station."""
        start_system = self.sde.get_system_by_station(start_station)
        if not start_system:
            raise ValueError(f"Unknown system for station: {start_station.name}")

        end_system = self.sde.get_system_by_station(end_station)
        if not end_system:
            raise ValueError(f"Unknown system for station: {end_station.name}")

        if not end_system.is_valid_end():
            raise ValueError(
                f"End station is in an invalid location: "
                f"high sec (>=0.5), pochven, or zarzakh. "
                f"Station: {end_station.name}, System: {end_system.name}, "
                f"Security: {end_system.security}"
            )

        # Find the route using BFS with priority queue
        path = self._find_path(start_system, end_system)
        if not path:
            raise ValueError("No valid path found from start to end station")

        # Calculate jumps with fatigue/cooldown
        return self._calculate_jumps(start_station, end_station, path)

    def _find_path(self, start: SolarSystem, end: SolarSystem) -> list[SolarSystem]:
        """Find optimal path using BFS with priority on multiple criteria."""
        from heapq import heappush, heappop

        # Priority queue: (priority_tuple, system_id, path)
        # Priority tuple: (num_jumps, total_time_waiting, total_ly, system_name)
        pq = []
        heappush(pq, ((0, 0, 0, start.name), start.system_id, [start]))

        visited = {}  # system_id -> best (num_jumps, total_time_waiting, total_ly)

        while pq:
            priority, current_id, path = heappop(pq)
            num_jumps, total_time, total_ly, _ = priority
            current_system = self.sde.systems_by_id[current_id]

            # Skip if we have a better or equal path to this system
            best = visited.get(current_id)
            if best and (best[0] < num_jumps or
                        (best[0] == num_jumps and best[1] <= total_time) or
                        (best[0] == num_jumps and best[1] == total_time and best[2] <= total_ly)):
                continue
            visited[current_id] = (num_jumps, total_time, total_ly)

            # Check if we reached the destination
            if current_id == end.system_id:
                return path

            # Get all valid jump targets
            for next_system in self.sde.get_jumpable_systems(current_system):
                # Calculate distance
                distance_ly = current_system.position.distance_to_ly(next_system.position)

                # Check range limit
                if distance_ly > self.max_range:
                    continue

                # Calculate effective distance and fatigue
                effective_dist = distance_ly * self.reduction_factor
                fatigue_increase = 10 * (1 + effective_dist)
                cooldown_increase = 1 * (1 + effective_dist)

                # Calculate new totals
                new_total_time = total_time + cooldown_increase
                new_total_ly = total_ly + distance_ly

                # Add to priority queue
                new_path = path + [next_system]
                new_priority = (num_jumps + 1, new_total_time, new_total_ly, next_system.name)
                heappush(pq, (new_priority, next_system.system_id, new_path))

        return []  # No path found

    def _calculate_jumps(self, start_station: Station, end_station: Station,
                        path: list[SolarSystem]) -> list[JumpResult]:
        """Calculate jump details including isotopes, fatigue, and cooldown."""
        results = []
        current_fatigue = 0.0  # minutes
        current_cooldown = 0.0  # minutes
        total_wait_time = 0.0

        # Get stations for systems along the path
        stations = {}
        for system in path:
            for station in self.sde.stations_by_name.values():
                if station.solar_system_id == system.system_id:
                    stations[system.system_id] = station
                    break

        # First jump: from start station's system
        from_system = path[0]
        # Find the best station in this system (start station itself)
        from_station = start_station

        for i in range(len(path) - 1):
            from_sys = path[i]
            to_sys = path[i + 1]

            # Calculate jump distance
            distance_ly = from_sys.position.distance_to_ly(to_sys.position)

            # Check if this jump hits the end station
            hits_end = (i + 1 == len(path) - 1 and to_sys.system_id == end_station.solar_system_id)
            to_station = end_station if hits_end else stations.get(to_sys.system_id)

            # Calculate effective distance for fatigue
            effective_dist = distance_ly * self.reduction_factor

            # Calculate isotopes needed (ceiling to thousands)
            isotopes = math.ceil(distance_ly * self.isotopes_per_jump / 1000) * 1000

            # Calculate fatigue and cooldown after jump
            fatigue_before = current_fatigue
            cooldown_before = current_cooldown

            # Handle cooldown - if cooldown > 0, wait
            wait_time = 0.0
            if cooldown_before > 0:
                wait_time = cooldown_before
                total_wait_time += wait_time
                current_cooldown = 0.0

            # Apply fatigue reduction during wait (if any)
            if wait_time > 0 and current_fatigue > 0:
                current_fatigue = max(0.0, current_fatigue - wait_time)

            # Calculate new fatigue and cooldown based on pre-jump values
            if fatigue_before == 0 and cooldown_before == 0:
                new_fatigue = 10 * (1 + effective_dist)
                new_cooldown = 1 + effective_dist
            else:
                new_fatigue = max(fatigue_before, 10) * (1 + effective_dist)
                new_cooldown = new_fatigue / 10

            # Cap values
            new_fatigue = min(new_fatigue, 300)  # 5 hours in minutes
            new_cooldown = min(new_cooldown, 30)  # 30 minutes

            # Ensure values don't drop below zero (they shouldn't based on formulas)
            new_fatigue = max(0, new_fatigue)
            new_cooldown = max(0, new_cooldown)

            jump_result = JumpResult(
                ly=distance_ly,
                system=to_sys,
                station=to_station,
                isotopes=isotopes,
                fatigue_before=fatigue_before,
                cooldown_before=cooldown_before,
                fatigue_after=new_fatigue,
                cooldown_after=new_cooldown,
                wait_time=wait_time
            )
            results.append(jump_result)

            # Update for next iteration
            current_fatigue = new_fatigue
            current_cooldown = new_cooldown

        return results


def format_time(minutes: float) -> str:
    """Format minutes as HH:MM with leading zeros."""
    mins = math.ceil(minutes)
    hours = mins // 60
    mins = mins % 60
    return f"{hours:02d}:{mins:02d}"


def format_isotopes(isotopes: int) -> str:
    """Format isotopes as #K with ceiling to thousands."""
    # isotopes is already ceiling to thousands
    thousands = isotopes // 1000
    return f"{thousands}K"


def main():
    parser = argparse.ArgumentParser(
        description="Jump Freighter Logistics Planner"
    )
    parser.add_argument(
        '--start',
        required=True,
        help="Start station name"
    )
    parser.add_argument(
        '--end',
        required=True,
        help="End station name (must not be in high sec, pochven, or zarzakh)"
    )
    parser.add_argument(
        '--sde',
        required=True,
        help="Path to SDE directory containing bzip'ed CSV files"
    )
    parser.add_argument(
        '--range',
        dest='max_range',
        type=int,
        default=10,
        choices=range(5, 11),
        help="Maximum LY range for a jump (5-10, default: 10)"
    )
    parser.add_argument(
        '--fuel',
        dest='isotopes_per_jump',
        type=int,
        default=10000,
        choices=range(1, 10001),
        help="Isotopes per jump (1-10000, default: 10000)"
    )
    parser.add_argument(
        '--reduction',
        dest='reduction',
        type=int,
        default=90,
        choices=range(0, 101),
        help="Effective jump distance reduction (0-100, default: 90)"
    )

    args = parser.parse_args()

    # Load SDE data
    sde = SDELoader(args.sde)

    # Find start and end stations
    start_station = sde.get_station(args.start)
    if not start_station:
        print(f"Error: Unknown start station: {args.start}", file=sys.stderr)
        sys.exit(1)

    end_station = sde.get_station(args.end)
    if not end_station:
        print(f"Error: Unknown end station: {args.end}", file=sys.stderr)
        sys.exit(1)

    # Create planner and plan route
    planner = JumpPlanner(sde, args.max_range, args.isotopes_per_jump, args.reduction)

    try:
        jumps = planner.plan(start_station, end_station)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Output results
    start_system = sde.get_system_by_station(start_station)

    print(f"START: {args.start}")
    print("UNDOCK")

    total_ly = 0.0
    total_isotopes = 0
    total_wait_time = 0.0

    for i, jump in enumerate(jumps):
        total_ly += jump.ly
        total_isotopes += jump.isotopes
        total_wait_time += jump.wait_time

        # Get station name for docking
        dock_station = jump.station.name if jump.station else jump.system.name

        # System name (just the base name without the station part)
        system_name = jump.system.name

        # Format isotopes
        isotopes_str = format_isotopes(jump.isotopes)

        print(f"JUMP {jump.ly:.2f} LY: {system_name} ({isotopes_str} isotopes)")
        print(f"DOCK: {dock_station}")

        # UNDOCK for all but the last jump
        if i < len(jumps) - 1:
            print("UNDOCK")

    # Calculate final cooldown and fatigue (after all jumps)
    final_cooldown = jumps[-1].cooldown_after if jumps else 0.0
    final_fatigue = jumps[-1].fatigue_after if jumps else 0.0

    print("SUMMARY:")
    print(f"  End Cooldown: {format_time(final_cooldown)}")
    print(f"  End Fatigue: {format_time(final_fatigue)}")
    print(f"  Isotopes Used: {total_isotopes:,K}")
    print(f"  Time Waiting: {format_time(total_wait_time)}")
    print(f"  Total LY: {total_ly:.2f}")


if __name__ == '__main__':
    main()
