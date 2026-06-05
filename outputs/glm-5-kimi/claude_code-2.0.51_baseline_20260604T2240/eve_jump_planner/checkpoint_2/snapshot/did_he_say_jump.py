#!/usr/bin/env python3
"""
Jump Freighter Logistics Planning Tool

Plans jump freighter runs from start to end station, calculating
isotopes needed and total fatigue.
"""

import argparse
import bz2
import math
import os
import sys
from dataclasses import dataclass
from typing import Optional

import pandas as pd


# Constants
LY_IN_METERS = 9_460_730_472_580_800.0  # 1 LY in meters

# Pochven region ID
POCHVEN_REGION_ID = 10000070

# Zarzakh system ID
ZARZAKH_SYSTEM_ID = 30045460


@dataclass
class System:
    """Represents a solar system."""
    id: int
    name: str
    x: float  # in meters
    y: float
    z: float
    security: float
    region_id: int

    def distance_ly(self, other: 'System') -> float:
        """Calculate distance in light years to another system."""
        dx = self.x - other.x
        dy = self.y - other.y
        dz = self.z - other.z
        dist_meters = math.sqrt(dx * dx + dy * dy + dz * dz)
        return dist_meters / LY_IN_METERS


@dataclass
class Station:
    """Represents a station."""
    id: int
    name: str
    system_id: int
    security: float


@dataclass
class JumpState:
    """Tracks fatigue and cooldown state."""
    fatigue_minutes: float = 0.0
    cooldown_minutes: float = 0.0
    time_waiting_minutes: float = 0.0

    def apply_jump(self, distance_ly: float, reduction: int) -> None:
        """Apply a jump with given distance and reduction."""
        effective_distance = (1 - reduction / 100.0) * distance_ly

        if self.fatigue_minutes == 0 and self.cooldown_minutes == 0:
            # First jump
            self.fatigue_minutes = 10.0 * (1.0 + effective_distance)
            self.cooldown_minutes = 1.0 + effective_distance
        else:
            # Subsequent jumps
            max_fatigue = max(self.fatigue_minutes, 10.0)
            self.fatigue_minutes = max_fatigue * (1.0 + effective_distance)
            self.cooldown_minutes = self.fatigue_minutes / 10.0

        # Cap fatigue at 5 hours (300 minutes) and cooldown at 30 minutes
        self.fatigue_minutes = min(self.fatigue_minutes, 300.0)
        self.cooldown_minutes = min(self.cooldown_minutes, 30.0)

        # Ensure non-negative
        self.fatigue_minutes = max(0.0, self.fatigue_minutes)
        self.cooldown_minutes = max(0.0, self.cooldown_minutes)

    def wait_for_cooldown(self) -> None:
        """Wait out the remaining cooldown."""
        if self.cooldown_minutes > 0:
            self.time_waiting_minutes += self.cooldown_minutes
            # Cooldown resets after waiting
            self.cooldown_minutes = 0.0


def load_systems(sde_dir: str) -> dict[int, System]:
    """Load solar systems from SDE."""
    systems_file = os.path.join(sde_dir, 'mapSolarSystems.csv.bz2')

    with bz2.open(systems_file, 'rt') as f:
        df = pd.read_csv(f)

    systems = {}
    for _, row in df.iterrows():
        systems[row['solarSystemID']] = System(
            id=row['solarSystemID'],
            name=row['solarSystemName'],
            x=float(row['x']),
            y=float(row['y']),
            z=float(row['z']),
            security=row['security'],
            region_id=row['regionID']
        )

    return systems


def load_stations(sde_dir: str) -> dict[str, Station]:
    """Load stations from SDE."""
    stations_file = os.path.join(sde_dir, 'staStations.csv.bz2')

    with bz2.open(stations_file, 'rt') as f:
        df = pd.read_csv(f)

    stations = {}
    for _, row in df.iterrows():
        stations[row['stationName']] = Station(
            id=row['stationID'],
            name=row['stationName'],
            system_id=row['solarSystemID'],
            security=row['security']
        )

    return stations


def load_system_jumps(sde_dir: str) -> dict[int, list[int]]:
    """Load regular jump connections between systems."""
    jumps_file = os.path.join(sde_dir, 'mapSolarSystemJumps.csv.bz2')

    with bz2.open(jumps_file, 'rt') as f:
        df = pd.read_csv(f)

    connections = {}
    for _, row in df.iterrows():
        from_id = row['fromSolarSystemID']
        to_id = row['toSolarSystemID']

        if from_id not in connections:
            connections[from_id] = set()
        connections[from_id].add(to_id)

        if to_id not in connections:
            connections[to_id] = set()
        connections[to_id].add(from_id)

    # Convert sets to lists
    return {k: list(v) for k, v in connections.items()}


def is_high_sec(security: float) -> bool:
    """Check if a security value is high sec (>= 0.5)."""
    return security >= 0.5


def is_pochven(region_id: int) -> bool:
    """Check if a region is Pochven."""
    return region_id == POCHVEN_REGION_ID


def is_zarzakh(system_id: int) -> bool:
    """Check if a system is Zarzakh."""
    return system_id == ZARZAKH_SYSTEM_ID


def can_be_destination(system: System, systems: dict[int, System]) -> bool:
    """Check if a system can be a destination (not high sec, pochven, or zarzakh)."""
    if is_high_sec(system.security):
        return False
    if is_pochven(system.region_id):
        return False
    if is_zarzakh(system.id):
        return False
    return True


def find_stations_in_system(system_id: int, stations: dict[str, Station]) -> list[Station]:
    """Find all stations in a given system."""
    return [s for s in stations.values() if s.system_id == system_id]


def calculate_isotopes(distance_ly: float, fuel_per_jump: int) -> int:
    """Calculate isotopes needed for a jump (ceiling to thousands)."""
    # Isotopes = distance * fuel_per_jump, then ceiling to thousands
    isotopes = distance_ly * fuel_per_jump
    return math.ceil(isotopes / 1000.0) * 1000


def format_time(minutes: float) -> str:
    """Format minutes as HH:MM (ceiling to minutes, with leading zeros)."""
    total_minutes = math.ceil(minutes)
    hours = total_minutes // 60
    mins = total_minutes % 60
    return f"{hours:02d}:{mins:02d}"


def format_isotopes(isotopes: int) -> str:
    """Format isotopes as thousands with K suffix."""
    return f"{isotopes // 1000}K"


@dataclass
class JumpStep:
    """A single jump step."""
    from_system: System
    to_system: System
    distance_ly: float
    isotopes: int
    dock_station: Station
    wait_minutes: float  # Time waiting before this jump
    is_gate_jump: bool = False  # Whether this is a gate jump (cloak mode)
    gate_from_system: Optional[System] = None  # For gate jumps, the system before the gate


def find_optimal_path(
    start_system: System,
    end_system: System,
    systems: dict[int, System],
    stations: dict[str, Station],
    max_range: float,
    fuel_per_jump: int,
    reduction: int,
    system_jumps: dict[int, list[int]] = None,
    use_cloak: bool = False
) -> list[JumpStep]:
    """
    Find the optimal jump path using modified Dijkstra/A* algorithm.
    Optimization criteria (in order):
    1. Min Jumps
    2. Min Time Waiting (ceiled to minutes)
    3. Min Total Trip LY
    4. Lexicographic sorting of systems

    When use_cloak is True, allows one gate jump per undock cycle.
    """

    # Build candidate midpoints - systems with stations that can be destinations
    valid_midpoints = {}
    for sys_id, sys in systems.items():
        sys_stations = find_stations_in_system(sys_id, stations)
        if sys_stations:
            valid_midpoints[sys_id] = sys

    import heapq

    # Priority queue entries: (jumps, ceiled_time_waiting, total_ly, system_name,
    #                          current_system_id, fatigue, cooldown, path, cloak_available)
    # path is list of (from_system_id, to_system_id, distance, isotopes, dock_station_name, wait_before, is_gate, gate_from_id)

    initial_state = (0, 0, 0.0, "", start_system.id, 0.0, 0.0, [], use_cloak)
    pq = [initial_state]

    # Track best known states per (system_id, cloak_available)
    # Key: (system_id, cloak_available), Value: (min_jumps, min_ceiled_wait, min_total_ly, path_systems)
    best = {}

    while pq:
        jumps, ceiled_wait, total_ly, _, current_sys_id, fatigue, cooldown, path, cloak_available = heapq.heappop(pq)

        current_system = systems[current_sys_id]

        # Check if we reached destination
        if current_sys_id == end_system.id:
            # Build final path
            result = []
            state = JumpState()

            for from_id, to_id, dist, iso, dock_name, wait, is_gate, gate_from_id in path:
                # Wait if needed
                if state.cooldown_minutes > 0:
                    state.wait_for_cooldown()

                result.append(JumpStep(
                    from_system=systems[from_id],
                    to_system=systems[to_id],
                    distance_ly=dist,
                    isotopes=iso,
                    dock_station=stations[dock_name],
                    wait_minutes=wait,
                    is_gate_jump=is_gate,
                    gate_from_system=systems[gate_from_id] if gate_from_id else None
                ))

                if not is_gate:
                    state.apply_jump(dist, reduction)

            return result

        # Find possible jumps from current system
        for next_sys_id, next_system in valid_midpoints.items():
            if next_sys_id == current_sys_id:
                continue

            # Check if destination system is valid (not high sec, pochven, zarzakh)
            # unless it's the final destination
            if next_sys_id != end_system.id and not can_be_destination(next_system, systems):
                continue

            # Calculate distance
            distance = current_system.distance_ly(next_system)

            # Check range
            if distance > max_range:
                continue

            # Calculate fatigue/cooldown impact
            new_fatigue = fatigue
            new_cooldown = cooldown
            new_time_waiting = 0.0

            # Need to wait for cooldown if > 0
            wait_before = 0.0
            if new_cooldown > 0:
                wait_before = new_cooldown
                new_time_waiting = wait_before
                new_cooldown = 0.0

            # Apply jump
            effective_distance = (1 - reduction / 100.0) * distance

            if new_fatigue == 0 and new_cooldown == 0:
                new_fatigue = 10.0 * (1.0 + effective_distance)
                new_cooldown = 1.0 + effective_distance
            else:
                max_fatigue = max(new_fatigue, 10.0)
                new_fatigue = max_fatigue * (1.0 + effective_distance)
                new_cooldown = new_fatigue / 10.0

            # Cap
            new_fatigue = min(new_fatigue, 300.0)
            new_cooldown = min(new_cooldown, 30.0)
            new_fatigue = max(0.0, new_fatigue)
            new_cooldown = max(0.0, new_cooldown)

            # Find stations in target system
            target_stations = find_stations_in_system(next_sys_id, stations)
            if not target_stations:
                continue

            # Pick best station lexicographically for tie-breaking
            best_station = min(target_stations, key=lambda s: s.name)

            new_total_ly = total_ly + distance
            new_jumps = jumps + 1

            # Calculate total time waiting for comparison (so far + new)
            total_time_waiting = sum(p[5] for p in path) + new_time_waiting
            new_ceiled_wait = math.ceil(total_time_waiting)

            # Round total LY to 2 decimal places for comparison
            new_rounded_ly = round(new_total_ly, 2)

            # After a jump drive jump, cloak ability resets (must dock first)
            new_cloak_available = False

            # Create state key for deduplication
            state_key = (next_sys_id, new_cloak_available)

            # Check if this is a better path to this state
            if state_key in best:
                best_jumps, best_wait, best_ly, best_systems = best[state_key]
                # Compare in priority order: jumps, ceiled_wait, rounded_ly, lexicographic
                path_systems = tuple(systems[p[1]].name for p in (path + [(current_sys_id, next_sys_id)]))
                if (new_jumps, new_ceiled_wait, new_rounded_ly, path_systems) >= (best_jumps, best_wait, best_ly, best_systems):
                    continue

            path_systems = tuple(systems[p[1]].name for p in (path + [(current_sys_id, next_sys_id)]))
            best[state_key] = (new_jumps, new_ceiled_wait, new_rounded_ly, path_systems)

            # Add to path
            iso = calculate_isotopes(distance, fuel_per_jump)
            new_path = path + [(current_sys_id, next_sys_id, distance, iso, best_station.name, wait_before, False, None)]

            # Add to priority queue
            heapq.heappush(pq, (
                new_jumps,
                new_ceiled_wait,
                new_total_ly,
                next_system.name,
                next_sys_id,
                new_fatigue,
                new_cooldown,
                new_path,
                new_cloak_available
            ))

        # If cloak is available and system_jumps provided, try gate jumps
        if cloak_available and system_jumps and current_sys_id in system_jumps:
            for gate_dest_id in system_jumps[current_sys_id]:
                if gate_dest_id not in systems:
                    continue

                gate_dest_system = systems[gate_dest_id]

                # Calculate new state after gate jump
                # Gate jump doesn't affect fatigue/cooldown
                # After gate jump, cloak is used up for this undock cycle
                new_cloak_available = False

                # From the gate destination, we can jump to any valid midpoint
                for next_sys_id, next_system in valid_midpoints.items():
                    if next_sys_id == gate_dest_id:
                        continue

                    # Check if destination is valid
                    if next_sys_id != end_system.id and not can_be_destination(next_system, systems):
                        continue

                    # Calculate distance from gate destination to next system
                    distance = gate_dest_system.distance_ly(next_system)

                    # Check range - gate jump gives better range (effectively same range check)
                    if distance > max_range:
                        continue

                    # Calculate fatigue/cooldown impact
                    new_fatigue = fatigue
                    new_cooldown = cooldown
                    new_time_waiting = 0.0

                    # Need to wait for cooldown if > 0
                    wait_before = 0.0
                    if new_cooldown > 0:
                        wait_before = new_cooldown
                        new_time_waiting = wait_before
                        new_cooldown = 0.0

                    # Apply jump
                    effective_distance = (1 - reduction / 100.0) * distance

                    if new_fatigue == 0 and new_cooldown == 0:
                        new_fatigue = 10.0 * (1.0 + effective_distance)
                        new_cooldown = 1.0 + effective_distance
                    else:
                        max_fatigue = max(new_fatigue, 10.0)
                        new_fatigue = max_fatigue * (1.0 + effective_distance)
                        new_cooldown = new_fatigue / 10.0

                    # Cap
                    new_fatigue = min(new_fatigue, 300.0)
                    new_cooldown = min(new_cooldown, 30.0)
                    new_fatigue = max(0.0, new_fatigue)
                    new_cooldown = max(0.0, new_cooldown)

                    # Find stations in target system
                    target_stations = find_stations_in_system(next_sys_id, stations)
                    if not target_stations:
                        continue

                    # Pick best station lexicographically
                    best_station = min(target_stations, key=lambda s: s.name)

                    new_total_ly = total_ly + distance
                    new_jumps = jumps + 1

                    # Calculate total time waiting
                    total_time_waiting = sum(p[5] for p in path) + new_time_waiting
                    new_ceiled_wait = math.ceil(total_time_waiting)

                    new_rounded_ly = round(new_total_ly, 2)

                    # After jump drive jump, cloak ability stays unavailable
                    final_cloak_available = False

                    state_key = (next_sys_id, final_cloak_available)

                    if state_key in best:
                        best_jumps, best_wait, best_ly, best_systems = best[state_key]
                        path_systems = tuple(systems[p[1]].name for p in (path + [(current_sys_id, gate_dest_id), (gate_dest_id, next_sys_id)]))
                        if (new_jumps, new_ceiled_wait, new_rounded_ly, path_systems) >= (best_jumps, best_wait, best_ly, best_systems):
                            continue

                    path_systems = tuple(systems[p[1]].name for p in (path + [(current_sys_id, gate_dest_id), (gate_dest_id, next_sys_id)]))
                    best[state_key] = (new_jumps, new_ceiled_wait, new_rounded_ly, path_systems)

                    iso = calculate_isotopes(distance, fuel_per_jump)
                    # Add gate jump step followed by jump drive step
                    new_path = path + [
                        (current_sys_id, gate_dest_id, 0.0, 0, best_station.name, 0.0, True, current_sys_id),  # Gate jump
                        (gate_dest_id, next_sys_id, distance, iso, best_station.name, wait_before, False, None)  # Jump drive jump
                    ]

                    heapq.heappush(pq, (
                        new_jumps,
                        new_ceiled_wait,
                        new_total_ly,
                        next_system.name,
                        next_sys_id,
                        new_fatigue,
                        new_cooldown,
                        new_path,
                        final_cloak_available
                    ))

    return []  # No path found


def main():
    parser = argparse.ArgumentParser(
        description='Plan Jump Freighter logistics runs'
    )
    parser.add_argument('--start', required=True, help='Start station name')
    parser.add_argument('--end', required=True, help='End station name')
    parser.add_argument('--sde', required=True, help='Path to SDE directory')
    parser.add_argument('--range', type=int, default=10, choices=[5, 10],
                        help='Max LY range for a jump (default: 10)')
    parser.add_argument('--fuel', type=int, default=10000,
                        choices=range(1, 10001),
                        help='Isotopes per jump (default: 10000)')
    parser.add_argument('--reduction', type=int, default=90,
                        choices=range(0, 101),
                        help='Effective jump distance reduction %% (default: 90)')
    parser.add_argument('--cloak', action='store_true',
                        help='Enable cloak mode: allows one gate jump per undock cycle')

    args = parser.parse_args()

    # Load data
    try:
        systems = load_systems(args.sde)
        stations = load_stations(args.sde)
        system_jumps = load_system_jumps(args.sde) if args.cloak else None
    except Exception as e:
        print(f"Error loading SDE data: {e}", file=sys.stderr)
        sys.exit(1)

    # Validate start station
    if args.start not in stations:
        print(f"Error: Start station '{args.start}' not found", file=sys.stderr)
        sys.exit(1)

    start_station = stations[args.start]
    start_system = systems[start_station.system_id]

    # Validate end station
    if args.end not in stations:
        print(f"Error: End station '{args.end}' not found", file=sys.stderr)
        sys.exit(1)

    end_station = stations[args.end]
    end_system = systems[end_station.system_id]

    # Check if end station is in valid location
    if not can_be_destination(end_system, systems):
        print(f"Error: End station cannot be in High Sec, Pochven, or Zarzakh", file=sys.stderr)
        sys.exit(1)

    # Find optimal path
    path = find_optimal_path(
        start_system,
        end_system,
        systems,
        stations,
        float(args.range),
        args.fuel,
        args.reduction,
        system_jumps=system_jumps,
        use_cloak=args.cloak
    )

    if not path:
        print(f"Error: No valid path found from '{args.start}' to '{args.end}'", file=sys.stderr)
        sys.exit(1)

    # Calculate totals
    total_isotopes = sum(step.isotopes for step in path if not step.is_gate_jump)
    total_ly = sum(step.distance_ly for step in path)

    # Calculate final fatigue state
    state = JumpState()
    for step in path:
        if state.cooldown_minutes > 0 and not step.is_gate_jump:
            state.wait_for_cooldown()
        if not step.is_gate_jump:
            state.apply_jump(step.distance_ly, args.reduction)

    # Output results
    print(f"START: {args.start}")

    for step in path:
        if step.is_gate_jump:
            # Gate jump output format
            from_sys = step.gate_from_system
            to_sys = step.to_system
            # Round security to 1 decimal place
            from_sec = round(from_sys.security, 1)
            to_sec = round(to_sys.security, 1)
            print(f"GO: {from_sys.name} ({from_sec}) -> {to_sys.name} ({to_sec})")
        else:
            print("UNDOCK")
            print(f"JUMP {step.distance_ly:.2f} LY: {step.to_system.name} ({format_isotopes(step.isotopes)} isotopes)")
            print(f"DOCK: {step.dock_station.name}")

    print("SUMMARY:")
    print(f"  End Cooldown: {format_time(state.cooldown_minutes)}")
    print(f"  End Fatigue: {format_time(state.fatigue_minutes)}")
    print(f"  Isotopes Used: {format_isotopes(total_isotopes)}")
    print(f"  Time Waiting: {format_time(state.time_waiting_minutes)}")
    print(f"  Total LY: {total_ly:.2f}")


if __name__ == '__main__':
    main()
