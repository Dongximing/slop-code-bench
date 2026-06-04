#!/usr/bin/env python3
"""
Jump Freight logistics planner for EVE Online.
Calculates optimal routes considering fatigue, cooldown, and isotopes.
"""

import argparse
import csv
import math
import bz2
from collections import defaultdict
from pathlib import Path as PathLib
from typing import NamedTuple, Optional

# Constants
METER_TO_LY = 1.0 / 9_460_730_472_580_800.0

# Security thresholds
HIGH_SEC_THRESHOLD = 0.5

# Fatigue/Cooldown limits
MAX_FATIGUE_HOURS = 5
MAX_COOLDOWN_MINUTES = 30


class Vector3(NamedTuple):
    x: float
    y: float
    z: float


class SolarSystem(NamedTuple):
    system_id: int
    name: str
    position: Vector3
    security: float


class Station(NamedTuple):
    station_id: int
    system_id: int
    name: str
    security: float


class JumpResult(NamedTuple):
    distance_ly: float
    system: SolarSystem
    isotopes_used: int


class PathStep(NamedTuple):
    station: Station
    jump_result: Optional[JumpResult]


class Path:
    """Represents a complete path from start to end."""

    def __init__(self):
        self.steps: list[PathStep] = []
        self.total_ly: float = 0.0
        self.total_isotopes: int = 0
        self.end_cooldown_min: int = 0
        self.end_fatigue_min: int = 0
        self.total_waiting_min: int = 0
        self.cloak_used: bool = False  # Whether cloak jump has been used

    def add_step(self, station: Station, jump_result: Optional[JumpResult] = None):
        self.steps.append(PathStep(station, jump_result))

    @property
    def jump_count(self) -> int:
        return len([s for s in self.steps if s.jump_result is not None])

    def calculate_end_state(self, reduction: float) -> tuple[int, int]:
        """Calculate final cooldown and fatigue after entire journey."""
        cooldown_min = 0
        fatigue_min = 0

        for step in self.steps:
            if step.jump_result is not None:
                eff_distance = (reduction / 100) * step.jump_result.distance_ly

                if cooldown_min == 0 and fatigue_min == 0:
                    new_fatigue = 10 * (1 + eff_distance)
                    new_cooldown = 1 + eff_distance
                else:
                    new_fatigue = max(fatigue_min, 10) * (1 + eff_distance)
                    new_cooldown = 0.1 * fatigue_min

                # Apply limits
                new_fatigue = min(new_fatigue, MAX_FATIGUE_HOURS * 60)
                new_cooldown = min(new_cooldown, MAX_COOLDOWN_MINUTES)

                cooldown_min = new_cooldown
                fatigue_min = new_fatigue

        return int(math.ceil(cooldown_min)), int(math.ceil(fatigue_min))


def load_solar_systems(sde_path: Path) -> dict[int, SolarSystem]:
    """Load solar systems from SDE."""
    systems = {}
    filepath = sde_path / "mapSolarSystems.csv.bz2"

    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            systems[int(row['solarSystemID'])] = SolarSystem(
                system_id=int(row['solarSystemID']),
                name=row['solarSystemName'],
                position=Vector3(
                    float(row['x']),
                    float(row['y']),
                    float(row['z'])
                ),
                security=float(row['security'])
            )

    return systems


def load_stations(sde_path: Path) -> dict[int, Station]:
    """Load stations from SDE."""
    stations = {}
    filepath = sde_path / "staStations.csv.bz2"

    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            stations[int(row['stationID'])] = Station(
                station_id=int(row['stationID']),
                system_id=int(row['solarSystemID']),
                name=row['stationName'],
                security=float(row['security'])
            )

    return stations


def load_system_jumps(sde_path: Path) -> dict[int, set[int]]:
    """Load connected systems for jump routing."""
    jumps = defaultdict(set)
    filepath = sde_path / "mapSolarSystemJumps.csv.bz2"

    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            from_sys = int(row['fromSolarSystemID'])
            to_sys = int(row['toSolarSystemID'])
            jumps[from_sys].add(to_sys)
            jumps[to_sys].add(from_sys)

    return jumps


def distance_ly(pos1: Vector3, pos2: Vector3) -> float:
    """Calculate distance in LY between two 3D positions."""
    dx = pos1.x - pos2.x
    dy = pos1.y - pos2.y
    dz = pos1.z - pos2.z

    distance_meters = math.sqrt(dx*dx + dy*dy + dz*dz)
    return distance_meters * METER_TO_LY


def calculate_isotopes(distance_ly: float, reduction: float, isotopes_per_jump: int) -> int:
    """Calculate isotopes needed for a jump, rounded up to thousands."""
    effective_distance = (reduction / 100) * distance_ly
    # Isotopes = distance * isotopes_per_jump / reduction_factor
    # The reduction factor makes jumps cheaper
    raw_isotopes = effective_distance * isotopes_per_jump

    # Round up to nearest thousand
    return int(math.ceil(raw_isotopes / 1000)) * 1000


def format_time(minutes: int) -> str:
    """Format minutes as HH:MM with leading zeros."""
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours:02d}:{mins:02d}"


def format_isotopes(isotopes: int) -> str:
    """Format isotopes with K suffix for thousands."""
    return f"{isotopes // 1000}K"


def find_station_by_name(stations: dict[int, Station], name: str) -> Optional[Station]:
    """Find a station by its exact name or partial match."""
    # Try exact match first
    for station in stations.values():
        if station.name == name:
            return station

    # Try case-insensitive
    name_lower = name.lower()
    for station in stations.values():
        if station.name.lower() == name_lower:
            return station

    # Try partial match
    for station in stations.values():
        if name_lower in station.name.lower():
            return station

    return None


def validate_end_station(station: Station, systems: dict[int, SolarSystem]) -> tuple[bool, str]:
    """Validate that end station meets requirements."""
    system = systems.get(station.system_id)
    if system is None:
        return False, "Could not find system for end station"

    # Check security: must not be in High Sec (>=0.5)
    if system.security >= HIGH_SEC_THRESHOLD:
        return False, f"End station must not be in High Sec (security >= 0.5), found {system.security}"

    # Check pochven/zarzakh (security < 0.0)
    if system.security < 0.0:
        return False, "End station must not be in pochven or zarzakh (negative security)"

    return True, ""


def find_path(
    start_station: Station,
    end_station: Station,
    systems: dict[int, SolarSystem],
    jumps: dict[int, set[int]],
    stations_dict: dict[int, Station],
    max_range_ly: int,
    reduction: float,
    isotopes_per_jump: int,
    use_cloak: bool = False
) -> Optional[Path]:
    """
    Find a path from start to end using BFS with custom sorting.

    Sorting priority:
    1. Min Jumps
    2. Min Time Waiting
    3. Total Trip LY
    4. Lexicographic sorting of systems
    """

    start_system = systems[start_station.system_id]
    end_system = systems[end_station.system_id]

    # Priority queue entries: (priority_tuple, path)
    # priority_tuple: (num_jumps, total_waiting, total_ly, system_names)
    from heapq import heappush, heappop

    initial_path = Path()
    initial_path.add_step(start_station)

    # Visited: station_id -> (num_jumps, total_waiting, total_ly, system_names)
    visited = {start_station.station_id: (0, 0, 0.0, [])}

    heap = [((0, 0, 0.0, []), initial_path)]

    while heap:
        (num_jumps, total_waiting, total_ly, system_names), path = heappop(heap)

        current_station = path.steps[-1].station
        current_system = systems[current_station.system_id]

        # Check if we found the end station
        if current_station.station_id == end_station.station_id:
            return path

        # Calculate cloak extended range (can be used once after undocking)
        can_use_cloak = use_cloak and len(path.steps) == 1 and not path.cloak_used
        effective_max_range = max_range_ly * 2 if can_use_cloak else max_range_ly

        # Try to find connected systems within range
        for neighbor_id in jumps.get(current_system.system_id, []):
            neighbor_system = systems.get(neighbor_id)
            if neighbor_system is None:
                continue

            dist = distance_ly(current_system.position, neighbor_system.position)

            # Check if within range
            if dist > effective_max_range:
                continue

            # Find stations in this system
            stations_in_system = [
                s for s in stations.values()
                if s.system_id == neighbor_system.system_id
            ]

            for next_station in stations_in_system:
                # Calculate cooldown/fatigue from current path
                cooldown_min = 0
                fatigue_min = 0

                for step in path.steps:
                    if step.jump_result is not None:
                        eff_dist = (reduction / 100) * step.jump_result.distance_ly
                        if cooldown_min == 0 and fatigue_min == 0:
                            new_fatigue = 10 * (1 + eff_dist)
                            new_cooldown = 1 + eff_dist
                        else:
                            new_fatigue = max(fatigue_min, 10) * (1 + eff_dist)
                            new_cooldown = 0.1 * fatigue_min

                        cooldown_min = min(new_cooldown, MAX_COOLDOWN_MINUTES)
                        fatigue_min = min(new_fatigue, MAX_FATIGUE_HOURS * 60)

                # Calculate additional waiting time
                additional_wait = cooldown_min if cooldown_min > 0 and next_station.station_id != end_station.station_id else 0

                new_total_waiting = total_waiting + additional_wait
                new_num_jumps = num_jumps + 1
                new_total_ly = total_ly + dist
                new_system_names = system_names + [neighbor_system.name]

                # Check if this is a better path to this station
                key = next_station.station_id
                existing = visited.get(key)

                if existing is None or (
                    new_num_jumps < existing[0] or
                    (new_num_jumps == existing[0] and new_total_waiting < existing[1]) or
                    (new_num_jumps == existing[0] and new_total_waiting == existing[1] and new_total_ly < existing[2])
                ):
                    visited[key] = (new_num_jumps, new_total_waiting, new_total_ly, new_system_names)

                    new_path = Path()
                    new_path.steps = list(path.steps)
                    new_path.cloak_used = path.cloak_used or can_use_cloak

                    jump_result = JumpResult(
                        distance_ly=dist,
                        system=neighbor_system,
                        isotopes_used=calculate_isotopes(dist, reduction, isotopes_per_jump)
                    )

                    new_path.add_step(next_station, jump_result)
                    new_path.total_ly = new_total_ly

                    priority = (
                        new_num_jumps,
                        new_total_waiting,
                        new_total_ly,
                        tuple(new_system_names)
                    )

                    heappush(heap, (*priority, new_path))

    return None


def main():
    parser = argparse.ArgumentParser(
        description='Plan Jump Freighter logistics runs'
    )
    parser.add_argument(
        '--start',
        required=True,
        help='Starting station name'
    )
    parser.add_argument(
        '--end',
        required=True,
        help='Destination station name'
    )
    parser.add_argument(
        '--sde',
        required=True,
        help='Path to SDE directory containing bzip2 CSV files'
    )
    parser.add_argument(
        '--range',
        dest='max_range',
        type=int,
        default=10,
        choices=range(5, 11),
        help='Maximum LY range for a jump (default: 10)'
    )
    parser.add_argument(
        '--fuel',
        type=int,
        default=10000,
        help='Isotopes per jump (default: 10000)'
    )
    parser.add_argument(
        '--reduction',
        type=int,
        default=90,
        choices=range(0, 101),
        help='Effective jump distance reduction percent (default: 90)'
    )
    parser.add_argument(
        '--cloak',
        action='store_true',
        help='Enable cloak gate warping for better ranges (can be used once after undocking)'
    )

    args = parser.parse_args()

    print(f"DEBUG: args.sde type={type(args.sde)}, value={args.sde!r}")
    sde_path = PathLib(args.sde)

    # Validate SDE directory
    if not sde_path.exists():
        print(f"Error: SDE directory '{sde_path}' does not exist")
        return 1

    # Load data
    print("Loading SDE data...", file=__import__('sys').stderr)
    systems = load_solar_systems(sde_path)
    stations = load_stations(sde_path)
    jumps = load_system_jumps(sde_path)

    # Find start station
    start_station = find_station_by_name(stations, args.start)
    if start_station is None:
        print(f"Error: Could not find start station '{args.start}'")
        return 1

    # Find end station
    end_station = find_station_by_name(stations, args.end)
    if end_station is None:
        print(f"Error: Could not find end station '{args.end}'")
        return 1

    # Validate end station
    valid, msg = validate_end_station(end_station, systems)
    if not valid:
        print(f"Error: {msg}")
        return 1

    # Find path
    print("Calculating optimal route...", file=__import__('sys').stderr)
    path = find_path(
        start_station,
        end_station,
        systems,
        jumps,
        args.max_range,
        args.reduction,
        args.fuel,
        args.cloak
    )

    if path is None:
        print("Error: Could not find a valid path")
        return 1

    # Calculate totals
    total_ly = sum(s.jump_result.distance_ly for s in path.steps if s.jump_result)
    total_isotopes = sum(s.jump_result.isotopes_used for s in path.steps if s.jump_result)

    # Calculate waiting time
    total_waiting = 0
    for i, step in enumerate(path.steps):
        if step.jump_result and i < len(path.steps) - 1:
            # Calculate cooldown after this jump
            eff_distance = (args.reduction / 100) * step.jump_result.distance_ly

            cooldown_min = 0
            fatigue_min = 0
            for prev_step in path.steps[:i + 1]:
                if prev_step.jump_result is not None:
                    prev_eff = (args.reduction / 100) * prev_step.jump_result.distance_ly
                    if cooldown_min == 0 and fatigue_min == 0:
                        fatigue_min = 10 * (1 + prev_eff)
                        cooldown_min = 1 + prev_eff
                    else:
                        fatigue_min = max(fatigue_min, 10) * (1 + prev_eff)
                        cooldown_min = 0.1 * fatigue_min

                    cooldown_min = min(cooldown_min, MAX_COOLDOWN_MINUTES)
                    fatigue_min = min(fatigue_min, MAX_FATIGUE_HOURS * 60)

            total_waiting += int(math.ceil(cooldown_min))

    # Calculate end state
    end_cooldown, end_fatigue = path.calculate_end_state(args.reduction)

    # Output
    print(f"START: {start_station.name}")

    for i, step in enumerate(path.steps[1:], 1):  # Skip start station
        prev_station = path.steps[i-1].station
        prev_system = systems[prev_station.system_id]

        if step.jump_result:
            distance_rounded = round(step.jump_result.distance_ly, 2)
            system_name = step.jump_result.system.name
            isotopes_k = format_isotopes(step.jump_result.isotopes_used)

            # Print GO line for gate jump with security rounded to 1 decimal
            prev_sec_rounded = round(prev_system.security, 1)
            curr_sec_rounded = round(step.jump_result.system.security, 1)
            print(f"GO: {prev_system.name} ({prev_sec_rounded}) -> {system_name} ({curr_sec_rounded})")

            print("UNDOCK")
            print(f"JUMP {distance_rounded:.2f} LY: {system_name} ({isotopes_k} isotopes)")

        print(f"DOCK: {step.station.name}")

    print("SUMMARY:")
    print(f"  End Cooldown: {format_time(end_cooldown)}")
    print(f"  End Fatigue: {format_time(end_fatigue)}")
    print(f"  Isotopes Used: {total_isotopes:,} total isotopes")
    print(f"  Time Waiting: {format_time(total_waiting)}")
    print(f"  Total LY: {total_ly:.2f}")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
