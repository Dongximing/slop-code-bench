#!/usr/bin/env python3
"""
Jump Freighter logistics planner for EVE Online.
"""

import argparse
import csv
import bz2
import math
import heapq
from pathlib import Path
from typing import Dict, List, Tuple, Set, Optional
from dataclasses import dataclass
from collections import defaultdict

# Constants
LY_TO_METERS = 9_460_730_472_580_800  # 1 LY in meters
ISOTOPES_PER_JUMP_BASE = 10000
EFFECTIVE_DISTANCE_REDUCTION = 0.1  # 90% reduction
MAX_FATIGUE_MINUTES = 300  # 5 hours
MAX_COOLDOWN_MINUTES = 30    # 30 minutes
BASE_FATIGUE_MULTIPLIER = 10
COOLDOWN_DIVISOR = 10


@dataclass
class Vector3:
    x: float
    y: float
    z: float

    def distance_to(self, other: 'Vector3') -> float:
        dx = self.x - other.x
        dy = self.y - other.y
        dz = self.z - other.z
        return math.sqrt(dx * dx + dy * dy + dz * dz)


@dataclass
class SolarSystem:
    system_id: int
    name: str
    position: Vector3
    security: float
    security_class: Optional[str]


@dataclass
class Station:
    station_id: int
    name: str
    system_id: int
    security: float


@dataclass
class JumpResult:
    distance_ly: float
    isotopes_used: int
    fatigue_time_minutes: float
    cooldown_time_minutes: float
    waiting_time_minutes: float
    total_ly: float
    path: List[Tuple[str, str]]  # List of (jump system, dock station) pairs
    total_isotopes: int
    total_waiting: float


def load_csv_bz2(filepath: Path) -> List[Dict]:
    """Load a bzip2 compressed CSV file."""
    with bz2.open(filepath, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return list(reader)


def load_sde_data(sde_path: Path) -> Tuple[Dict[int, SolarSystem], Dict[str, Station], Dict[int, List[int]]]:
    """Load all SDE data and return systems, stations, and jump graph."""
    # Load solar systems
    systems_file = sde_path / 'mapSolarSystems.csv.bz2'
    systems = {}
    for row in load_csv_bz2(systems_file):
        sys_id = int(row['solarSystemID'])
        pos = Vector3(
            float(row['x']),
            float(row['y']),
            float(row['z'])
        )
        systems[sys_id] = SolarSystem(
            system_id=sys_id,
            name=row['solarSystemName'],
            position=pos,
            security=float(row['security']),
            security_class=row.get('securityClass', 'None') or 'None'
        )

    # Load stations
    stations_file = sde_path / 'staStations.csv.bz2'
    stations_by_name = {}
    stations_by_system = defaultdict(list)
    for row in load_csv_bz2(stations_file):
        station = Station(
            station_id=int(row['stationID']),
            name=row['stationName'],
            system_id=int(row['solarSystemID']),
            security=float(row['security'])
        )
        stations_by_name[station.name] = station
        stations_by_system[station.system_id].append(station)

    # Load wormhole jumps
    jumps_file = sde_path / 'mapSolarSystemJumps.csv.bz2'
    graph = defaultdict(set)
    for row in load_csv_bz2(jumps_file):
        from_sys = int(row['fromSolarSystemID'])
        to_sys = int(row['toSolarSystemID'])
        graph[from_sys].add(to_sys)
        graph[to_sys].add(from_sys)

    return systems, stations_by_name, stations_by_system, graph


def meters_to_ly(meters: float) -> float:
    """Convert meters to light years."""
    return meters / LY_TO_METERS


def get_system_distance(sys1: SolarSystem, sys2: SolarSystem) -> float:
    """Get distance between two systems in LY."""
    return meters_to_ly(sys1.position.distance_to(sys2.position))


def is_valid_end_station(station: Station, systems: Dict[int, SolarSystem]) -> bool:
    """Check if a station is valid for --end destination."""
    # Must not be in High Sec (>= 0.5 with rounding)
    if station.security >= 0.5:
        return False

    # Check system security class and security
    system = systems.get(station.system_id)
    if not system:
        return False

    # Check for zarzakh region (security < 0)
    if system.security < 0:
        # Check if this is in the Zarzakh region specifically
        # Zarzakh has very low security values
        return False

    # Pochven systems are identified by:
    # - securityClass that is NOT 'None' AND is a faction ID (like 500003, 500004, etc.)
    # NOT simple letter codes like 'A', 'B', 'C', 'D', 'E' etc.
    if system.security_class and system.security_class != 'None':
        # Check if security_class is a faction ID (5-digit numbers) or region ID
        # Simple letters (A-E) indicate regional classification, not pochven
        try:
            class_num = int(system.security_class)
            # Large numbers (like 50000x) are faction/region IDs
            if class_num >= 50000:
                return False  # This is pochven
        except ValueError:
            # Not a number - it's a letter code (A, B, C, D, E, etc.)
            # These indicate regional classification, not pochven
            # Allow them as they are just regional designations
            pass

    return True


def find_station_by_name(name: str, stations_by_name: Dict[str, Station], systems: Dict[int, SolarSystem]) -> Optional[Station]:
    """Find a station by full name or partial name match."""
    # Exact match
    if name in stations_by_name:
        return stations_by_name[name]

    # Partial match (case-insensitive)
    name_lower = name.lower()
    exact_match = None
    exact_match_name = None
    for station_name, station in stations_by_name.items():
        if name_lower in station_name.lower():
            if exact_match is None:
                exact_match = station
                exact_match_name = station_name
            else:
                # Multiple matches - prefer more specific (longer name)
                if len(station_name) > len(exact_match_name):
                    exact_match = station
                    exact_match_name = station_name

    return exact_match


def calculate_isotopes(distance_ly: float, isotopes_per_jump: int) -> int:
    """Calculate isotopes needed, rounded up to nearest 1000."""
    effective_distance = EFFECTIVE_DISTANCE_REDUCTION * distance_ly
    raw_isotopes = isotopes_per_jump * (1 + effective_distance)
    return math.ceil(raw_isotopes / 1000) * 1000


def calculate_fatigue_cooldown(
    distance_ly: float,
    current_fatigue: float,
    current_cooldown: float
) -> Tuple[float, float, float]:
    effective_distance = EFFECTIVE_DISTANCE_REDUCTION * distance_ly

    waiting_time = 0.0
    if current_cooldown > 0:
        waiting_time = current_cooldown

    if current_fatigue == 0 and current_cooldown == 0:
        new_fatigue = BASE_FATIGUE_MULTIPLIER * (1 + effective_distance)
    else:
        new_fatigue = max(current_fatigue, BASE_FATIGUE_MULTIPLIER) * (1 + effective_distance)

    new_cooldown = current_fatigue / COOLDOWN_DIVISOR

    new_fatigue = min(new_fatigue, MAX_FATIGUE_MINUTES)
    new_cooldown = min(new_cooldown, MAX_COOLDOWN_MINUTES)

    new_fatigue = max(new_fatigue, 0)
    new_cooldown = max(new_cooldown, 0)

    return new_fatigue, new_cooldown, waiting_time


def find_path(
    start_station: Station,
    end_station: Station,
    systems: Dict[int, SolarSystem],
    stations_by_system: Dict[int, List[Station]],
    graph: Dict[int, Set[int]],
    max_jump_range: int,
    isotopes_per_jump: int
) -> Optional[JumpResult]:
    """Find the optimal path from start to end using A* with custom metrics."""

    start_system_id = start_station.system_id
    end_system_id = end_station.system_id

    # If start and end are in the same system, we're done
    if start_system_id == end_system_id:
        return JumpResult(
            distance_ly=0.0,
            isotopes_used=0,
            fatigue_time_minutes=0.0,
            cooldown_time_minutes=0.0,
            waiting_time_minutes=0.0,
            total_ly=0.0,
            path=[(systems[end_system_id].name, end_station.name)],
            total_isotopes=0,
            total_waiting=0.0
        )

    # Use modified Dijkstra/A* with priority on:
    # 1. Min Jumps
    # 2. Min Time Waiting
    # 3. Total Trip LY
    # 4. Lexicographic sorting of systems

    # Priority queue: (priority_tuple, system_id, fatigue, cooldown, waiting, path)
    # Priority tuple: (num_jumps, total_waiting, total_ly, last_system_name)
    pq = []

    # State: (system_id, fatigue_minutes, cooldown_minutes, waiting_minutes, total_ly, path_list, num_jumps)
    # For start, we haven't jumped yet
    heapq.heappush(pq, (
        (0, 0.0, 0.0, systems[start_system_id].name),  # Priority
        start_system_id,  # Current system
        0.0,  # Fatigue
        0.0,  # Cooldown
        0.0,  # Waiting time
        0.0,  # Total LY
        [],  # Path
        0   # Number of jumps
    ))

    # Best known states: system_id -> (num_jumps, total_waiting, total_ly)
    best_known = defaultdict(lambda: (float('inf'), float('inf'), float('inf')))

    while pq:
        priority_tuple, current_sys_id, fatigue, cooldown, waiting, total_ly, path, num_jumps = heapq.heappop(pq)
        current_system = systems[current_sys_id]

        # Check if we reached the target
        if current_sys_id == end_system_id:
            # Build the final path with docking info
            final_path = []
            prev_sys = start_system_id
            for jump_sys_id in path:
                jump_system = systems[jump_sys_id]
                # Find stations in the jumped-to system
                jump_stations = stations_by_system.get(jump_sys_id, [])
                dock_station = jump_stations[0] if jump_stations else None

                if dock_station:
                    final_path.append((jump_system.name, dock_station.name))
                else:
                    final_path.append((jump_system.name, None))
                prev_sys = jump_sys_id

            # Add end station only if it's a different system from the last one in path
            if not path or path[-1] != end_system_id:
                final_path.append((systems[end_system_id].name, end_station.name))
            else:
                # Replace the last entry's station name with the end station
                if final_path:
                    final_path[-1] = (final_path[-1][0], end_station.name)

            # Calculate total isotopes
            total_isotopes = 0
            for jump_sys_id in path + [end_system_id]:
                if path and jump_sys_id == path[0]:
                    dist = get_system_distance(systems[start_system_id], systems[jump_sys_id])
                else:
                    # Find previous system in path
                    idx = path.index(jump_sys_id) if jump_sys_id in path else -1
                    if idx > 0:
                        prev_id = path[idx - 1]
                        dist = get_system_distance(systems[prev_id], systems[jump_sys_id])
                    elif jump_sys_id == end_system_id and path:
                        prev_id = path[-1]
                        dist = get_system_distance(systems[prev_id], systems[jump_sys_id])
                    else:
                        dist = 0
                total_isotopes += calculate_isotopes(dist, isotopes_per_jump)

            return JumpResult(
                distance_ly=0.0,  # Will be updated
                isotopes_used=0,
                fatigue_time_minutes=fatigue,
                cooldown_time_minutes=cooldown,
                waiting_time_minutes=waiting,
                total_ly=total_ly,
                path=final_path,
                total_isotopes=total_isotopes,
                total_waiting=waiting
            )

        # Explore neighbors
        for neighbor_id in graph.get(current_sys_id, []):
            neighbor_system = systems.get(neighbor_id)
            if not neighbor_system:
                continue

            # Check jump range
            dist = get_system_distance(current_system, neighbor_system)
            if dist > max_jump_range:
                continue

            # Calculate new state after jump
            new_fatigue, new_cooldown, jump_waiting = calculate_fatigue_cooldown(
                dist, fatigue, cooldown
            )
            new_waiting = waiting + jump_waiting
            new_total_ly = total_ly + dist
            new_path = path + [neighbor_id]
            new_num_jumps = num_jumps + 1

            # Check if this is better than what we know
            best_jumps, best_waiting, best_ly = best_known[neighbor_id]

            new_priority = (new_num_jumps, new_waiting, new_total_ly, neighbor_system.name)

            # Only continue if better
            if (new_num_jumps < best_jumps or
                (new_num_jumps == best_jumps and new_waiting < best_waiting) or
                (new_num_jumps == best_jumps and new_waiting == best_waiting and new_total_ly < best_ly)):

                best_known[neighbor_id] = (new_num_jumps, new_waiting, new_total_ly)
                heapq.heappush(pq, (
                    new_priority,
                    neighbor_id, new_fatigue, new_cooldown, new_waiting, new_total_ly, new_path, new_num_jumps
                ))

    return None


def format_time(minutes: float) -> str:
    """Format minutes as HH:MM with leading zeros."""
    total_minutes = math.ceil(minutes)
    hours = int(total_minutes // 60)
    mins = int(total_minutes % 60)
    return f"{hours:02d}:{mins:02d}"


def format_isotopes(isotopes: int) -> str:
    """Format isotopes as #K (thousands)."""
    thousands = isotopes // 1000
    return f"{thousands}K"


def main():
    parser = argparse.ArgumentParser(
        description='Plan Jump Freighter logistics runs.',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--start', required=True, help='Start station name')
    parser.add_argument('--end', required=True, help='End station name (must be in low-sec/Null-sec)')
    parser.add_argument('--sde', required=True, help='Path to SDE directory')
    parser.add_argument('--range', dest='max_range', type=int, default=10,
                        choices=range(5, 11), help='Max LY range for a jump [5-10], default 10')
    parser.add_argument('--fuel', type=int, default=10000,
                        choices=range(1, 10001), help='Isotopes per jump [1-10000], default 10000')
    parser.add_argument('--reduction', type=int, default=90,
                        choices=range(0, 101), help='Effective jump distance reduction [0-100], default 90')
    parser.add_argument('--cloak', action='store_true',
                        help='Enable cloak-assisted gate jumping (safe method usable anywhere)')

    args = parser.parse_args()

    sde_path = Path(args.sde)
    if not sde_path.exists():
        print(f"Error: SDE directory '{args.sde}' does not exist")
        return 1

    # Override global reduction if specified
    global EFFECTIVE_DISTANCE_REDUCTION
    EFFECTIVE_DISTANCE_REDUCTION = 1 - (args.reduction / 100)

    # Load data
    print("Loading SDE data...")
    systems, stations_by_name, stations_by_system, graph = load_sde_data(sde_path)
    print(f"Loaded {len(systems)} systems and {len(stations_by_name)} stations")

    # Find stations
    start_station = find_station_by_name(args.start, stations_by_name, systems)
    if not start_station:
        print(f"Error: Start station '{args.start}' not found")
        return 1

    end_station = find_station_by_name(args.end, stations_by_name, systems)
    if not end_station:
        print(f"Error: End station '{args.end}' not found")
        return 1

    # Validate end station
    if not is_valid_end_station(end_station, systems):
        print(f"Error: End station '{args.end}' is not valid (must be in low-sec/Null-sec, not pochven/zarzakh)")
        return 1

    # Find path
    print("Calculating optimal path...")
    result = find_path(
        start_station, end_station,
        systems, stations_by_system, graph,
        args.max_range, args.fuel
    )

    if not result:
        print("Error: No valid path found")
        return 1

    # Calculate actual distances for output
    total_ly = 0.0
    total_isotopes_used = 0
    prev_system_id = start_station.system_id

    output_lines = []
    output_lines.append(f"START: {start_station.name}")
    output_lines.append("UNDOCK")

    # Process each jump
    for jump_system_name, dock_station_name in result.path:
        # Find the system ID
        jump_system_id = None
        for sys_id, sys in systems.items():
            if sys.name == jump_system_name:
                jump_system_id = sys_id
                break

        if jump_system_id and prev_system_id:
            # Print GO: line when cloak flag is enabled
            if args.cloak:
                prev_system = systems.get(prev_system_id)
                next_system = systems.get(jump_system_id)
                if prev_system and next_system:
                    # Round security to 1 decimal place
                    prev_sec = round(prev_system.security, 1)
                    next_sec = round(next_system.security, 1)
                    output_lines.append(f"GO: {prev_system.name} ({prev_sec}) -> {next_system.name} ({next_sec})")

            dist = get_system_distance(
                systems[prev_system_id],
                systems[jump_system_id]
            )
            total_ly += dist

            isotopes = calculate_isotopes(dist, args.fuel)
            total_isotopes_used += isotopes

            output_lines.append(f"JUMP {dist:.2f} LY: {jump_system_name} ({format_isotopes(isotopes)} isotopes)")

            if dock_station_name:
                output_lines.append(f"DOCK: {dock_station_name}")
                # UNDOCK for next jump if not the last
                if jump_system_name != result.path[-1][0] if result.path else True:
                    output_lines.append("UNDOCK")

        prev_system_id = jump_system_id if jump_system_id else prev_system_id

    # Summary
    output_lines.append("SUMMARY:")
    output_lines.append(f"  End Cooldown: {format_time(result.cooldown_time_minutes)}")
    output_lines.append(f"  End Fatigue: {format_time(result.fatigue_time_minutes)}")
    output_lines.append(f"  Isotopes Used: {total_isotopes_used:,} isotopes")
    output_lines.append(f"  Time Waiting: {format_time(result.total_waiting)}")
    output_lines.append(f"  Total LY: {total_ly:.2f}")

    print('\n'.join(output_lines))
    return 0


if __name__ == '__main__':
    exit(main())
