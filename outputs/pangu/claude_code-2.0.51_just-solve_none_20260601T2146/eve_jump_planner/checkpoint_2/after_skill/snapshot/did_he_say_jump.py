#!/usr/bin/env python3
"""
Jump Freighter logistics planning tool.
Plans jumps from start to end station, calculating isotopes needed and total fatigue.
"""

import argparse
import math
import bz2
import csv
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Set
import heapq

# Constants
METER_PER_LY = 9_460_730_472_580_800.0  # meters per light year
MAX_FATIGUE_HOURS = 5
MAX_COOLDOWN_MINUTES = 30
DEFAULT_RANGE = 10  # LY
DEFAULT_FUEL = 10_000  # isotopes per jump
DEFAULT_REDUCTION = 90  # percent


@dataclass
class Vector3:
    """3D vector for position."""
    x: float
    y: float
    z: float

    def distance_to(self, other: 'Vector3') -> float:
        """Calculate Euclidean distance to another vector."""
        return math.sqrt((self.x - other.x)**2 + (self.y - other.y)**2 + (self.z - other.z)**2)


@dataclass
class SolarSystem:
    """Represents a solar system."""
    system_id: int
    name: str
    position: Vector3
    security: float  # Security status (0.0 to 1.0 for highsec, <0.0 for lowsec/nullsec)
    constellation_id: int
    region_id: int
    security_class: Optional[str] = None  # For pochven/zarzakh detection

    @property
    def is_high_sec(self) -> bool:
        """Check if system is high security (>=0.5 rounded)."""
        return round(self.security, 1) >= 0.5

    def __hash__(self):
        return hash(self.system_id)

    def __eq__(self, other):
        return isinstance(other, SolarSystem) and self.system_id == other.system_id


@dataclass
class Station:
    """Represents a station."""
    station_id: int
    name: str
    position: Vector3
    solar_system_id: int
    solar_system_name: str
    security: float

    def __hash__(self):
        return hash(self.station_id)

    def __eq__(self, other):
        return isinstance(other, Station) and self.station_id == other.station_id


@dataclass
class Jump:
    """Represents a single jump in the route."""
    distance_ly: float
    destination_system: SolarSystem
    destination_station: Station
    isotopes_used: int  # in thousands, ceiling

    def __repr__(self):
        return f"JUMP {self.distance_ly:.2f} LY: {self.destination_system.name} ({self.isotopes_used}K isotopes)"


@dataclass
class RouteState:
    """State for A* pathfinding."""
    current_system: SolarSystem
    jumps: int
    time_waiting_minutes: float  # Total time waiting for cooldown
    fatigue_minutes: float  # Current fatigue time
    cooldown_minutes: float  # Current cooldown time
    total_distance_ly: float
    total_isotopes: int  # in thousands
    path: List[Jump]

    def __lt__(self, other):
        # Priority: Min Jumps > Min Time Waiting > Total Trip LY > Lexicographic
        if self.jumps != other.jumps:
            return self.jumps < other.jumps
        if self.time_waiting_minutes != other.time_waiting_minutes:
            return self.time_waiting_minutes < other.time_waiting_minutes
        if self.total_distance_ly != other.total_distance_ly:
            return self.total_distance_ly < other.total_distance_ly
        # Lexicographic sorting of systems would require comparing the path
        # For simplicity, compare based on current system name
        return self.current_system.name < other.current_system.name


def load_systems(sde_path: str) -> Dict[int, SolarSystem]:
    """Load solar systems from mapSolarSystems.csv.bz2."""
    systems = {}
    sde_path = sde_path.rstrip('/')
    filename = f"{sde_path}/mapSolarSystems.csv.bz2"

    with bz2.open(filename, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            system_id = int(row['solarSystemID'])
            systems[system_id] = SolarSystem(
                system_id=system_id,
                name=row['solarSystemName'],
                position=Vector3(
                    float(row['x']),
                    float(row['y']),
                    float(row['z'])
                ),
                security=float(row['security']),
                constellation_id=int(row['constellationID']),
                region_id=int(row['regionID']),
                security_class=row.get('securityClass')
            )
    return systems


def load_stations(sde_path: str, systems: Dict[int, SolarSystem]) -> Dict[int, Station]:
    """Load stations from staStations.csv.bz2."""
    stations = {}
    sde_path = sde_path.rstrip('/')
    filename = f"{sde_path}/staStations.csv.bz2"

    with bz2.open(filename, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            station_id = int(row['stationID'])
            system_id = int(row['solarSystemID'])
            system = systems.get(system_id)
            if system:
                stations[station_id] = Station(
                    station_id=station_id,
                    name=row['stationName'],
                    position=Vector3(
                        float(row['x']),
                        float(row['y']),
                        float(row['z'])
                    ),
                    solar_system_id=system_id,
                    solar_system_name=system.name,
                    security=float(row['security'])
                )
    return stations


def load_jumps(sde_path: str) -> List[Tuple[int, int]]:
    """Load jump connections from mapSolarSystemJumps.csv.bz2."""
    jumps = []
    sde_path = sde_path.rstrip('/')
    filename = f"{sde_path}/mapSolarSystemJumps.csv.bz2"

    with bz2.open(filename, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            from_id = int(row['fromSolarSystemID'])
            to_id = int(row['toSolarSystemID'])
            jumps.append((from_id, to_id))
    return jumps


class JumpFreighterPlanner:
    """Plans jump freighter logistics runs."""

    def __init__(
        self,
        systems: Dict[int, SolarSystem],
        stations: Dict[int, Station],
        jumps: List[Tuple[int, int]],
        max_range: int = DEFAULT_RANGE,
        isotopes_per_jump: int = DEFAULT_FUEL,
        reduction: int = DEFAULT_REDUCTION
    ):
        self.systems = systems
        self.stations = stations
        self.stations_by_name = {s.name.lower(): s for s in stations.values()}
        self.jumps = jumps
        self.max_range = max_range
        self.isotopes_per_jump = isotopes_per_jump
        self.reduction = reduction / 100.0  # Convert to fraction

    def calculate_distance_ly(self, system1: SolarSystem, system2: SolarSystem) -> float:
        """Calculate distance between two systems in light years."""
        distance_meters = system1.position.distance_to(system2.position)
        return distance_meters / METER_PER_LY

    def get_effective_distance(self, distance_ly: float) -> float:
        """Calculate effective distance with reduction applied."""
        return 0.1 * distance_ly * self.reduction

    def calculate_isotopes(self, distance_ly: float) -> int:
        """Calculate isotopes needed for a jump (ceiling to thousands)."""
        raw_isotopes = distance_ly * self.isotopes_per_jump
        # Ceiling to thousands
        return math.ceil(raw_isotopes / 1000) * 1000 // 1000

    def calculate_fatigue_cooldown(
        self,
        prev_fatigue_minutes: float,
        prev_cooldown_minutes: float,
        distance_ly: float
    ) -> Tuple[float, float]:
        """
        Calculate new fatigue and cooldown after a jump.

        Args:
            prev_fatigue_minutes: Previous fatigue time in minutes (float)
            prev_cooldown_minutes: Previous cooldown time in minutes (float)
            distance_ly: Distance of the jump in light years

        Returns:
            Tuple of (new_fatigue, new_cooldown) in minutes (floats)
        """
        effective_distance = self.get_effective_distance(distance_ly)

        if prev_fatigue_minutes == 0 and prev_cooldown_minutes == 0:
            # First jump
            new_fatigue = 10 * (1 + effective_distance)
            new_cooldown = 1 + effective_distance
        else:
            # Subsequent jumps
            new_fatigue = max(prev_fatigue_minutes, 10) * (1 + effective_distance)
            new_cooldown = new_fatigue / 10

        # Apply caps
        new_fatigue = min(new_fatigue, MAX_FATIGUE_HOURS * 60)
        new_cooldown = min(new_cooldown, MAX_COOLDOWN_MINUTES)

        # Ensure they don't drop below zero (shouldn't happen with current logic, but being safe)
        new_fatigue = max(0, new_fatigue)
        new_cooldown = max(0, new_cooldown)

        return new_fatigue, new_cooldown

    @staticmethod
    def is_end_system_valid(system: SolarSystem) -> bool:
        """Check if a system is valid as an end system."""
        return not system.is_high_sec

    def find_route(
        self,
        start_station: Station,
        end_station: Station
    ) -> Optional[List[Jump]]:
        """
        Find a route from start station to end station using A* search.
        Returns a list of jumps or None if no route found.
        """
        start_system = self.systems.get(start_station.solar_system_id)
        end_system = self.systems.get(end_station.solar_system_id)

        if not start_system or not end_system:
            return None

        # Validate end station
        if not self.is_end_system_valid(end_system):
            print(f"Error: End station {end_station.name} is in invalid security status", file=sys.stderr)
            return None

        # A* search
        # Priority queue: (priority, state)
        # Priority based on: Min Jumps > Min Time Waiting > Total Trip LY > Lexicographic
        open_set = []
        closed_set = set()

        initial_state = RouteState(
            current_system=start_system,
            jumps=0,
            time_waiting_minutes=0,
            fatigue_minutes=0,
            cooldown_minutes=0,
            total_distance_ly=0.0,
            total_isotopes=0,
            path=[]
        )

        heapq.heappush(open_set, (0, initial_state))

        # Best found state for each system
        best_for_system: Dict[int, RouteState] = {}

        max_iterations = 1000000  # Prevent infinite loops
        iterations = 0

        while open_set and iterations < max_iterations:
            iterations += 1
            _, current_state = heapq.heappop(open_set)

            # Check if we reached the destination
            if current_state.current_system.system_id == end_system.system_id:
                return current_state.path

            # Skip if we've seen this system with a better state
            system_id = current_state.current_system.system_id
            if system_id in closed_set:
                continue
            closed_set.add(system_id)

            best_for_system[system_id] = current_state

            # Explore ALL systems within range (not just connected ones)
            for neighbor_system in self.systems.values():
                if neighbor_system.system_id in closed_set:
                    continue

                # Calculate distance to neighbor
                distance = self.calculate_distance_ly(
                    current_state.current_system,
                    neighbor_system
                )

                # Check range constraint
                if distance > self.max_range:
                    continue

                # Calculate time waiting if we have cooldown
                time_waiting = current_state.time_waiting_minutes
                if current_state.cooldown_minutes > 0:
                    time_waiting += current_state.cooldown_minutes

                # Calculate fatigue and cooldown for next jump
                new_fatigue, new_cooldown = self.calculate_fatigue_cooldown(
                    current_state.fatigue_minutes,
                    current_state.cooldown_minutes,
                    distance
                )

                # Calculate isotopes needed
                isotopes = self.calculate_isotopes(distance)

                # Create the jump
                jump = Jump(
                    distance_ly=distance,
                    destination_system=neighbor_system,
                    destination_station=None,  # Will fill in later
                    isotopes_used=isotopes
                )

                new_path = current_state.path + [jump]

                new_state = RouteState(
                    current_system=neighbor_system,
                    jumps=current_state.jumps + 1,
                    time_waiting_minutes=time_waiting,
                    fatigue_minutes=new_fatigue,
                    cooldown_minutes=new_cooldown,
                    total_distance_ly=current_state.total_distance_ly + distance,
                    total_isotopes=current_state.total_isotopes + isotopes,
                    path=new_path
                )

                priority = (
                    new_state.jumps,
                    new_state.time_waiting_minutes,
                    new_state.total_distance_ly,
                    new_state.current_system.name
                )
                heapq.heappush(open_set, (priority, new_state))

        return None

    def find_full_route(
        self,
        start_station: Station,
        end_station: Station
    ) -> Tuple[Optional[List[Jump]], int, int, int, float]:
        """
        Find a complete route from start station to end station.
        Returns (jumps, total_isotopes, total_waiting_minutes, total_distance_ly).
        """
        jumps = self.find_route(start_station, end_station)

        if not jumps:
            return None, 0, 0, 0.0

        # Calculate totals
        total_isotopes = sum(j.isotopes_used for j in jumps)
        total_distance = sum(j.distance_ly for j in jumps)

        # Calculate total waiting time
        # Waiting happens after each jump if cooldown > 0 before next jump
        total_waiting = 0
        fatigue = 0
        cooldown = 0

        for i, jump in enumerate(jumps):
            if i > 0:  # Check waiting after previous jump
                if cooldown > 0:
                    total_waiting += cooldown

            # Update fatigue and cooldown
            fatigue, cooldown = self.calculate_fatigue_cooldown(
                fatigue,
                cooldown,
                jump.distance_ly
            )

        return jumps, total_isotopes, total_waiting, total_distance, fatigue, cooldown


def format_time(minutes: float) -> str:
    """Format minutes as HH:MM with leading zeros."""
    minutes = int(minutes)
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours:02d}:{mins:02d}"


def format_isotopes(isotopes_k: int) -> str:
    """Format isotopes with K suffix."""
    return f"{isotopes_k}K"


def find_station_by_name(stations_by_name: Dict[str, Station], name: str) -> Optional[Station]:
    """Find a station by name (case-insensitive)."""
    name_lower = name.lower().strip()

    # Try exact match first
    if name_lower in stations_by_name:
        return stations_by_name[name_lower]

    # Try partial match
    for station_name, station in stations_by_name.items():
        if name_lower in station_name or station_name in name_lower:
            return station

    return None


def main():
    parser = argparse.ArgumentParser(
        description="Jump Freighter logistics planning tool"
    )
    parser.add_argument(
        '--start',
        required=True,
        help='Start station name'
    )
    parser.add_argument(
        '--end',
        required=True,
        help='End station name (must not be in High Sec, pochven, or zarzakh)'
    )
    parser.add_argument(
        '--sde',
        required=True,
        help='Path to SDE directory'
    )
    parser.add_argument(
        '--range',
        dest='max_range',
        type=int,
        default=DEFAULT_RANGE,
        choices=range(5, 11),
        help=f'Max LY range for a jump (int in [5,10], defaults to {DEFAULT_RANGE})'
    )
    parser.add_argument(
        '--fuel',
        type=int,
        default=DEFAULT_FUEL,
        choices=range(1, 10001),
        help=f'Isotopes per jump (int in [1,10000], default is {DEFAULT_FUEL})'
    )
    parser.add_argument(
        '--reduction',
        type=int,
        default=DEFAULT_REDUCTION,
        choices=range(0, 101),
        help=f'Effective jump distance reduction (int in [0,100], default is {DEFAULT_REDUCTION})'
    )
    parser.add_argument(
        '--cloak',
        action='store_true',
        help='Enable cloak gate jump behavior for better ranges (can be used once per undock session)'
    )

    args = parser.parse_args()

    # Load SDE data
    print("Loading SDE data...", file=sys.stderr)
    systems = load_systems(args.sde)
    stations = load_stations(args.sde, systems)
    jumps = load_jumps(args.sde)
    print(f"Loaded {len(systems)} systems, {len(stations)} stations, {len(jumps)} jump connections", file=sys.stderr)

    # Find start and end stations
    start_station = find_station_by_name(stations_by_name={s.name.lower(): s for s in stations.values()}, name=args.start)
    end_station = find_station_by_name(stations_by_name={s.name.lower(): s for s in stations.values()}, name=args.end)

    if not start_station:
        print(f"Error: Start station '{args.start}' not found", file=sys.stderr)
        sys.exit(1)

    if not end_station:
        print(f"Error: End station '{args.end}' not found", file=sys.stderr)
        sys.exit(1)

    # Create planner and find route
    planner = JumpFreighterPlanner(
        systems=systems,
        stations=stations,
        jumps=jumps,
        max_range=args.max_range,
        isotopes_per_jump=args.fuel,
        reduction=args.reduction
    )

    result = planner.find_full_route(start_station, end_station)
    jumps_found, total_isotopes, total_waiting, total_distance, end_fatigue, end_cooldown = result

    if jumps_found is None:
        print(f"Error: No route found from '{args.start}' to '{args.end}'", file=sys.stderr)
        sys.exit(1)

    # Output the route
    print(f"START: {start_station.name}")
    print("UNDOCK")

    # Track current station and system for the route
    current_system = systems[start_station.solar_system_id]

    # Track if cloak has been used in this undock session
    cloak_used_this_undock = False

    for i, jump in enumerate(jumps_found):
        # Find a station in the destination system for "DOCK" entry
        dest_station = None
        for station in stations.values():
            if station.solar_system_id == jump.destination_system.system_id:
                dest_station = station
                break

        jump.destination_station = dest_station

        # Check if we can use cloak jump for this gate
        can_use_cloak = args.cloak and not cloak_used_this_undock and i < len(jumps_found) - 1

        if can_use_cloak:
            # Use cloak gate jump - prints GO: line with rounded security
            security_from = round(current_system.security, 1)
            security_to = round(jump.destination_system.security, 1)
            print(f"GO: {current_system.name} ({security_from}) -> {jump.destination_system.name} ({security_to})")
            cloak_used_this_undock = True
            # Also need to dock at the destination
            if dest_station:
                print(f"DOCK: {dest_station.name}")
            # UNDOCK before next jump (if not last)
            if i < len(jumps_found) - 1:
                print("UNDOCK")
            # Move to next system
            current_system = jump.destination_system
            continue

        # Normal JUMP output
        print(f"JUMP {jump.distance_ly:.2f} LY: {jump.destination_system.name} ({format_isotopes(jump.isotopes_used)} isotopes)")

        if dest_station:
            # If this is the final jump, dock at the exact end station
            if i == len(jumps_found) - 1:
                print(f"DOCK: {end_station.name}")
            else:
                print(f"DOCK: {dest_station.name}")

        # UNDOCK before next jump (except after last)
        if i < len(jumps_found) - 1:
            print("UNDOCK")

        # Move to next system
        current_system = jump.destination_system

    print("SUMMARY:")
    print(f"  End Cooldown: {format_time(end_cooldown)}")
    print(f"  End Fatigue: {format_time(end_fatigue)}")
    print(f"  Isotopes Used: {total_isotopes:,}K")
    print(f"  Time Waiting: {format_time(total_waiting)}")
    print(f"  Total LY: {total_distance:.2f}")


if __name__ == '__main__':
    main()
