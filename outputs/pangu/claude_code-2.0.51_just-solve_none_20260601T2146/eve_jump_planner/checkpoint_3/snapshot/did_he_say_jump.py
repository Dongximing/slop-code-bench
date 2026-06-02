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

    def find_high_sec_entrance(
        self,
        hs_destination: SolarSystem,
        max_extra_gates: int
    ) -> Optional[Tuple[SolarSystem, Station, List[SolarSystem]]]:
        """
        Find the best non-HS entrance system for a HS destination.

        Returns:
            Tuple of (entrance_system, entrance_station, hs_path) or None if not found.
            hs_path is the list of HS systems from entrance to destination.
        """
        # Build adjacency list from jumps
        adj = {}
        for from_id, to_id in self.jumps:
            if from_id not in adj:
                adj[from_id] = []
            if to_id not in adj:
                adj[to_id] = []
            adj[from_id].append(to_id)
            adj[to_id].append(from_id)

        # Get all stations by system
        stations_by_system = {}
        for station in self.stations.values():
            if station.solar_system_id not in stations_by_system:
                stations_by_system[station.solar_system_id] = []
            stations_by_system[station.solar_system_id].append(station)

        # Find all non-HS systems with stations
        non_hs_systems_with_stations = []
        for system in self.systems.values():
            if not system.is_high_sec and system.system_id in stations_by_system:
                non_hs_systems_with_stations.append(system)

        if not non_hs_systems_with_stations:
            return None

        # BFS from destination to find shortest path to non-HS through HS only
        # We want the shortest path that goes through HS to a non-HS system with a station
        visited = set()
        queue = [(hs_destination, [hs_destination])]

        # Store all valid entrance paths
        valid_entrances = []

        while queue:
            current_system, path = queue.pop(0)

            if current_system.system_id in visited:
                continue
            visited.add(current_system.system_id)

            # Check if we reached a non-HS system with a station
            hs_gate_count = len([s for s in path if s.is_high_sec]) - 1  # Exclude destination

            # We want paths that go through at least one HS gate (can't jump directly)
            if hs_gate_count >= 1:
                if not current_system.is_high_sec and current_system.system_id in stations_by_system:
                    # Calculate warp distance for each station to the stargate
                    entrance_station = None
                    min_warp_dist = float('inf')

                    for station in stations_by_system[current_system.system_id]:
                        # Distance from station to the stargate position (same as system position)
                        warp_dist = station.position.distance_to(current_system.position)
                        if warp_dist < min_warp_dist:
                            min_warp_dist = warp_dist
                            entrance_station = station

                    if entrance_station:
                        # Calculate distance from destination to entrance (entrance path)
                        # Path is from destination to entrance
                        valid_entrances.append((
                            current_system,
                            entrance_station,
                            path.copy(),  # path from destination to entrance (HS -> non-HS)
                            hs_gate_count,
                            min_warp_dist
                        ))

            # Continue searching for other entrances (we want the best one based on preferences)
            # But limit search to max_extra_gates for efficiency
            if hs_gate_count >= max_extra_gates:
                continue

            for neighbor_id in adj.get(current_system.system_id, []):
                neighbor = self.systems.get(neighbor_id)
                if neighbor and neighbor.system_id not in visited:
                    new_path = path + [neighbor]
                    new_hs_count = len([s for s in new_path if s.is_high_sec]) - 1

                    # Only explore through HS systems, and only up to max_extra_gates
                    if neighbor.is_high_sec and new_hs_count <= max_extra_gates:
                        queue.append((neighbor, new_path))

        if not valid_entrances:
            return None

        # Select best entrance based on preferences:
        # Min warp distance (closest station to gate)
        # Then min HS gates
        # Then min total path LY (or some other metric)
        valid_entrances.sort(key=lambda x: (x[4], x[3]))  # Min warp distance, then min HS gates

        best_system, best_station, hs_path, hs_gates, warp_dist = valid_entrances[0]

        # Return entrance system, entrance station, and path from entrance to destination
        # hs_path is from destination to entrance, so reverse it
        path_to_destination = list(reversed(hs_path))

        return best_system, best_station, path_to_destination

    def find_route_to_entrance(
        self,
        start_station: Station,
        entrance_station: Station,
        entrance_system: SolarSystem
    ) -> Optional[List[Jump]]:
        """
        Find a route from start station to an entrance station in a non-HS system.
        This is a wrapper that calls find_route with modified behavior.
        """
        return self.find_route(start_station, entrance_station)

    def find_hs_path_jumps(
        self,
        from_system: SolarSystem,
        path: List[SolarSystem],
        end_station: Station
    ) -> Optional[List[Jump]]:
        """
        Create jumps for the HS path from entrance to destination.
        Path is a list of systems [entrance, ..., destination] (all HS except first).
        """
        jumps = []
        current_system = from_system

        # Get stations by system for finding docking stations
        stations_by_system = {}
        for station in self.stations.values():
            if station.solar_system_id not in stations_by_system:
                stations_by_system[station.solar_system_id] = []
            stations_by_system[station.solar_system_id].append(station)

        # Calculate jumps for each step in the path
        for i, next_system in enumerate(path):
            distance = self.calculate_distance_ly(current_system, next_system)
            isotopes = self.calculate_isotopes(distance)

            # Find a station in the next system for docking
            next_station = None
            if next_system.system_id in stations_by_system:
                # Pick the station closest to the stargate position
                closest_station = None
                min_dist = float('inf')
                for station in stations_by_system[next_system.system_id]:
                    warp_dist = station.position.distance_to(next_system.position)
                    if warp_dist < min_dist:
                        min_dist = warp_dist
                        closest_station = station
                next_station = closest_station

            jump = Jump(
                distance_ly=distance,
                destination_system=next_system,
                destination_station=next_station,
                isotopes_used=isotopes
            )
            jumps.append(jump)
            current_system = next_system

        return jumps

    def find_route(
        self,
        start_station: Station,
        end_station: Station,
        max_extra_gates: int = 0
    ) -> Optional[List[Jump]]:
        """
        Find a route from start station to end station using A* search.
        Returns a list of jumps or None if no route found.
        """
        start_system = self.systems.get(start_station.solar_system_id)
        end_system = self.systems.get(end_station.solar_system_id)

        if not start_system or not end_system:
            return None

        # Handle High Security destination
        if end_system.is_high_sec:
            # Find the best non-HS entrance for this HS destination
            entrance_result = self.find_high_sec_entrance(end_system, max_extra_gates)

            if not entrance_result:
                print(f"Error: No valid non-HS entrance found for HS destination {end_station.name}", file=sys.stderr)
                return None

            entrance_system, entrance_station, hs_path = entrance_result

            # Find route to the entrance station (non-HS)
            entrance_jumps = self.find_route_to_entrance(start_station, entrance_station, entrance_system)

            if not entrance_jumps:
                print(f"Error: No route found from {start_station.name} to entrance {entrance_station.name}", file=sys.stderr)
                return None

            # Add HS jumps from entrance to destination
            hs_jumps = self.find_hs_path_jumps(entrance_system, hs_path, end_station)

            if not hs_jumps:
                print(f"Error: No HS path found from {entrance_system.name} to {end_station.name}", file=sys.stderr)
                return None

            # Combine: route to entrance + HS path + final station
            return entrance_jumps + hs_jumps

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
        end_station: Station,
        max_extra_gates: int = 0
    ) -> Tuple[Optional[List[Jump]], int, int, int, float, float]:
        """
        Find a complete route from start station to end station.
        Returns (jumps, total_isotopes, total_waiting_minutes, total_distance_ly, end_fatigue, end_cooldown).
        """
        jumps = self.find_route(start_station, end_station, max_extra_gates)

        if not jumps:
            return None, 0, 0, 0.0, 0.0, 0.0

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
        help='End station name'
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
    parser.add_argument(
        '--max-extra-gates',
        '-gates',
        type=int,
        default=0,
        help='Maximum extra high security systems willing to take for closer station (default: 0)'
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

    result = planner.find_full_route(start_station, end_station, args.max_extra_gates)
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

    # Track if we have a HS destination and need to add GO: print for return trip
    hs_destination = end_station.solar_system
    hs_path_completed = None

    # First, process jumps through non-HS systems
    # We need to identify where HS path starts and handle it separately
    if hs_destination and hs_destination.is_high_sec:
        # This is a HS destination - jumps_found contains two parts:
        # 1. Jumps to the non-HS entrance
        # 2. HS jumps from entrance to destination
        # We need to find where the transition happens

        entrance_system = None
        hs_start_index = None

        for i, jump in enumerate(jumps_found):
            if jump.destination_system.is_high_sec:
                # Found the first HS jump - this is the transition point
                entrance_system = jumps_found[i-1].destination_system if i > 0 else jumps_found[i].destination_system
                hs_start_index = i
                break

        if entrance_system and hs_start_index is not None:
            # Process non-HS jumps first
            non_hs_jumps = jumps_found[:hs_start_index]
            hs_jumps = jumps_found[hs_start_index:]

            # Process non-HS jumps
            for i, jump in enumerate(non_hs_jumps):
                # Find a station in the destination system for "DOCK" entry
                dest_station = None
                for station in stations.values():
                    if station.solar_system_id == jump.destination_system.system_id:
                        dest_station = station
                        break

                jump.destination_station = dest_station

                # Check if we can use cloak jump for this gate
                can_use_cloak = args.cloak and not cloak_used_this_undock and i < len(non_hs_jumps) - 1

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
                    if i < len(non_hs_jumps) - 1:
                        print("UNDOCK")
                    # Move to next system
                    current_system = jump.destination_system
                    continue

                # Normal JUMP output
                print(f"JUMP {jump.distance_ly:.2f} LY: {jump.destination_system.name} ({format_isotopes(jump.isotopes_used)} isotopes)")

                if dest_station:
                    # If this is the final jump, dock at the exact end station
                    # Note: for HS destinations, we don't dock at entrance, we warp to gate
                    if i == len(non_hs_jumps) - 1 and len(hs_jumps) > 0:
                        print(f"DOCK: {dest_station.name}")
                    else:
                        print(f"DOCK: {dest_station.name}")

                # UNDOCK before next jump (except after last in non-HS part)
                if i < len(non_hs_jumps) - 1 or len(hs_jumps) > 0:
                    print("UNDOCK")

                # Move to next system
                current_system = jump.destination_system

            # Now process HS jumps with GO: print
            for i, jump in enumerate(hs_jumps):
                # Use GO: print for HS jumps
                security_from = round(current_system.security, 1)
                security_to = round(jump.destination_system.security, 1)

                # Check if this is the final jump
                is_last = i == len(hs_jumps) - 1

                if is_last:
                    # Final jump to destination - print GO: and then DOCK
                    print(f"GO: {current_system.name} ({security_from}) -> {jump.destination_system.name} ({security_to})")
                    print(f"DOCK: {end_station.name}")
                else:
                    # Intermediate HS jump - print GO:, then DOCK at station, then UNDOCK
                    # Find station in destination system
                    dest_station = None
                    for station in stations.values():
                        if station.solar_system_id == jump.destination_system.system_id:
                            dest_station = station
                            break

                    jump.destination_station = dest_station

                    print(f"GO: {current_system.name} ({security_from}) -> {jump.destination_system.name} ({security_to})")
                    if dest_station:
                        print(f"DOCK: {dest_station.name}")
                    print("UNDOCK")

                current_system = jump.destination_system
        else:
            # No clear division - process normally
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
    else:
        # Non-HS destination - process normally
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
