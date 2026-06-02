#!/usr/bin/env python3
"""
Jump Freighter Logistics Planner
Plans optimal jump routes between stations, calculating fuel and fatigue.
"""

import argparse
import bz2
import csv
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Set
import heapq

# Constants
LY_IN_METERS = 9460730472580800.0  # 1 LY in meters
MAX_FATIGUE_MINUTES = 300  # 5 hours
MAX_COOLDOWN_MINUTES = 30  # 30 minutes


@dataclass
class System:
    """Represents a solar system"""
    system_id: int
    name: str
    x: float  # in meters
    y: float
    z: float
    security: float
    region_id: int = 0


@dataclass
class Station:
    """Represents a station"""
    station_id: int
    name: str
    system_id: int
    security: float


@dataclass
class Jump:
    """Represents a single jump"""
    distance_ly: float
    target_system: str
    target_station: str
    isotopes: int
    wait_time: float  # minutes waited before this jump


@dataclass
class JumpPlan:
    """Complete jump plan from start to end"""
    start_station: str
    jumps: List[Jump] = field(default_factory=list)
    total_isotopes: int = 0
    total_ly: float = 0.0
    total_wait_time: float = 0.0
    end_fatigue: float = 0.0
    end_cooldown: float = 0.0


def load_systems(sde_path: str) -> Dict[int, System]:
    """Load solar systems from SDE bzip'ed CSV files"""
    systems = {}

    possible_files = ['mapSolarSystems.csv.bz2', 'mapSolarSystems.csv']

    data = None
    for filename in possible_files:
        filepath = os.path.join(sde_path, filename)
        if os.path.exists(filepath):
            if filename.endswith('.bz2'):
                with bz2.open(filepath, 'rt', encoding='utf-8') as f:
                    data = list(csv.DictReader(f))
            else:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = list(csv.DictReader(f))
            break

    if data is None:
        raise FileNotFoundError(f"Could not find solar systems data in {sde_path}")

    for row in data:
        system_id = int(row['solarSystemID'])
        systems[system_id] = System(
            system_id=system_id,
            name=row['solarSystemName'],
            x=float(row['x']),
            y=float(row['y']),
            z=float(row['z']),
            security=float(row['security']),
            region_id=int(row.get('regionID', 0))
        )

    return systems


def load_stations(sde_path: str, systems: Dict[int, System]) -> Dict[str, Station]:
    """Load stations from SDE bzip'ed CSV files"""
    stations = {}

    possible_files = ['staStations.csv.bz2', 'staStations.csv']

    data = None
    for filename in possible_files:
        filepath = os.path.join(sde_path, filename)
        if os.path.exists(filepath):
            if filename.endswith('.bz2'):
                with bz2.open(filepath, 'rt', encoding='utf-8') as f:
                    data = list(csv.DictReader(f))
            else:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = list(csv.DictReader(f))
            break

    if data is None:
        raise FileNotFoundError(f"Could not find stations data in {sde_path}")

    for row in data:
        system_id = int(row['solarSystemID'])
        if system_id in systems:
            stations[row['stationName']] = Station(
                station_id=int(row['stationID']),
                name=row['stationName'],
                system_id=system_id,
                security=systems[system_id].security
            )

    return stations


def calculate_distance_ly(sys1: System, sys2: System) -> float:
    """Calculate distance between two systems in LY using double precision"""
    dx = sys1.x - sys2.x
    dy = sys1.y - sys2.y
    dz = sys1.z - sys2.z
    distance_meters = math.sqrt(dx * dx + dy * dy + dz * dz)
    return distance_meters / LY_IN_METERS


def is_valid_destination(system: System) -> bool:
    """Check if a system is a valid destination (not high sec, pochven, or zarzakh)"""
    # High sec check (security >= 0.5 with rounding)
    if round(system.security, 1) >= 0.5:
        return False

    # Pochven systems (region ID 10000082) or known pochven system names
    if system.region_id == 10000082:
        return False

    # Zarzakh system
    if system.name.lower() == 'zarzakh':
        return False

    return True


def compute_jump_fatigue(fatigue: float, cooldown: float,
                          effective_distance: float) -> Tuple[float, float]:
    """
    Compute new fatigue and cooldown after a jump, using pre-jump fatigue.

    Returns: (new_fatigue, new_cooldown) in minutes
    """
    if fatigue == 0 and cooldown == 0:
        # First jump case
        new_fatigue = 10.0 * (1.0 + effective_distance)
        new_cooldown = 1.0 + effective_distance
    else:
        # Subsequent jumps
        # Cooldown = base_fatigue / 10 where base = max(pre_jump_fatigue, 10)
        base_fatigue = max(fatigue, 10.0)
        new_fatigue = base_fatigue * (1.0 + effective_distance)
        new_cooldown = base_fatigue / 10.0

    # Cap at maximum values
    new_fatigue = min(new_fatigue, MAX_FATIGUE_MINUTES)
    new_cooldown = min(new_cooldown, MAX_COOLDOWN_MINUTES)

    # Cannot be negative
    new_fatigue = max(new_fatigue, 0.0)
    new_cooldown = max(new_cooldown, 0.0)

    return new_fatigue, new_cooldown


def calculate_isotopes(distance_ly: float, fuel_per_jump: int) -> int:
    """Calculate isotopes needed (rounded up to thousands)"""
    raw_isotopes = distance_ly * fuel_per_jump
    return math.ceil(raw_isotopes / 1000) * 1000


def find_stations_in_system(system_id: int, stations: Dict[str, Station]) -> List[str]:
    """Find all station names in a given system"""
    return [name for name, station in stations.items() if station.system_id == system_id]


def get_station_in_system(system_id: int, stations: Dict[str, Station],
                          preferred: Optional[str] = None) -> Optional[str]:
    """Get a station name in a system, preferring specific one if available"""
    system_stations = find_stations_in_system(system_id, stations)

    if preferred and preferred in system_stations:
        return preferred

    if system_stations:
        return sorted(system_stations)[0]

    return None


def plan_jumps(start_station: Station, end_station: Station,
               systems: Dict[int, System], stations: Dict[str, Station],
               max_range_ly: float, fuel_per_jump: int,
               reduction_percent: int) -> JumpPlan:
    """
    Plan optimal jump route from start to end station.

    Priority: Min Jumps > Min Time Waiting > Total Trip LY > Lexicographic sorting
    """
    start_system = systems[start_station.system_id]
    end_system = systems[end_station.system_id]

    # Effective distance: 0.1 × d_ly means 90% reduction
    # reduction_factor = (100 - reduction_percent) / 100
    reduction_factor = (100.0 - reduction_percent) / 100.0

    # Build candidate systems: valid destinations or the end system
    candidate_systems = {}
    for sid, s in systems.items():
        if is_valid_destination(s) or s.system_id == end_system.system_id:
            candidate_systems[sid] = s

    # Store paths in a separate dict, keyed by auto-incrementing counter,
    # to avoid having lists in the heap tuple which break tie-breaking
    path_store = {}
    path_counter = [0]

    def store_path(path):
        idx = path_counter[0]
        path_counter[0] += 1
        path_store[idx] = path
        return idx

    # Priority queue entries: (num_jumps, total_wait, total_ly, sys_name, sys_id,
    #                           fatigue, cooldown, path_index)
    initial_idx = store_path([])
    initial_state = (0, 0.0, 0.0, start_system.name, start_system.system_id,
                     0.0, 0.0, initial_idx)

    # visited: system_id -> set of fatigue buckets seen
    visited = defaultdict(set)

    pq = [initial_state]

    while pq:
        num_jumps, total_wait, total_ly, sys_name, sys_id, fatigue, cooldown, path_idx = heapq.heappop(pq)
        path = path_store.pop(path_idx)

        # Check if we reached the destination
        if sys_id == end_system.system_id:
            # Build the jump plan from the path
            plan = JumpPlan(start_station=start_station.name)

            current_fatigue = 0.0
            current_cooldown = 0.0

            for i, (target_sys_id, dist_ly) in enumerate(path):
                target_system = systems[target_sys_id]
                effective_dist = dist_ly * reduction_factor

                # Determine wait needed (cooldown remaining)
                wait_needed = max(0.0, current_cooldown)

                # Fatigue decays during wait
                if wait_needed > 0:
                    current_fatigue = max(0.0, current_fatigue - wait_needed)
                    current_cooldown = max(0.0, current_cooldown - wait_needed)

                # Calculate isotopes
                isotopes = calculate_isotopes(dist_ly, fuel_per_jump)

                # Get station in target system
                if i == len(path) - 1:
                    target_station_name = end_station.name
                else:
                    target_station_name = get_station_in_system(target_sys_id, stations)

                jump = Jump(
                    distance_ly=dist_ly,
                    target_system=target_system.name,
                    target_station=target_station_name,
                    isotopes=isotopes,
                    wait_time=wait_needed
                )
                plan.jumps.append(jump)
                plan.total_isotopes += isotopes
                plan.total_wait_time += wait_needed

                # Compute new fatigue and cooldown using pre-jump values
                current_fatigue, current_cooldown = compute_jump_fatigue(
                    current_fatigue, current_cooldown, effective_dist
                )

            plan.total_ly = total_ly
            plan.end_fatigue = current_fatigue
            plan.end_cooldown = current_cooldown

            return plan

        # State key for visited check
        fatigue_bucket = round(fatigue, 0)
        state_key = fatigue_bucket

        if state_key in visited[sys_id]:
            continue
        visited[sys_id].add(state_key)

        current_system = systems[sys_id]

        # Try all possible jumps
        for target_system in candidate_systems.values():
            if target_system.system_id == sys_id:
                continue

            dist_ly = calculate_distance_ly(current_system, target_system)

            if dist_ly > max_range_ly:
                continue

            effective_dist = dist_ly * reduction_factor

            # At current position, cooldown should be 0 (we wait before moving on)
            # But we stored fatigue/cooldown as-is in state. Need to wait here.
            wait_needed = max(0.0, cooldown)
            # Fatigue decays during wait
            fat_after_wait = max(0.0, fatigue - wait_needed)
            cd_after_wait = 0.0

            # Compute new fatigue/cooldown after this jump
            new_fatigue, new_cooldown = compute_jump_fatigue(
                fat_after_wait, cd_after_wait, effective_dist
            )

            new_jumps = num_jumps + 1
            new_wait = total_wait + wait_needed
            new_ly = total_ly + dist_ly

            new_path_idx = store_path(path + [(target_system.system_id, dist_ly)])

            new_state = (new_jumps, new_wait, new_ly, target_system.name,
                        target_system.system_id, new_fatigue, new_cooldown,
                        new_path_idx)

            heapq.heappush(pq, new_state)

    raise ValueError(f"No valid jump route found from {start_station.name} to {end_station.name}")


def format_time(minutes: float) -> str:
    """Format minutes as HH:MM (ceiling to minutes, with leading zeros)"""
    total_minutes = math.ceil(minutes)
    hours = total_minutes // 60
    mins = total_minutes % 60
    return f"{hours:02d}:{mins:02d}"


def format_isotopes(isotopes: int) -> str:
    """Format isotopes with K suffix"""
    thousands = isotopes // 1000
    return f"{thousands}K"


def main():
    parser = argparse.ArgumentParser(description='Jump Freighter Logistics Planner')
    parser.add_argument('--start', required=True, help='Starting station name')
    parser.add_argument('--end', required=True, help='Destination station name')
    parser.add_argument('--sde', required=True, help='Path to SDE directory')
    parser.add_argument('--range', type=int, default=10, choices=range(5, 11),
                        help='Max LY range for a jump (default: 10)')
    parser.add_argument('--fuel', type=int, default=10000,
                        help='Isotopes per jump (default: 10000)')
    parser.add_argument('--reduction', type=int, default=90,
                        help='Jump distance reduction percent (default: 90)')

    args = parser.parse_args()

    # Validate fuel range
    if not (1 <= args.fuel <= 10000):
        print("Error: --fuel must be between 1 and 10000", file=sys.stderr)
        sys.exit(1)

    # Validate reduction range
    if not (0 <= args.reduction <= 100):
        print("Error: --reduction must be between 0 and 100", file=sys.stderr)
        sys.exit(1)

    try:
        # Load SDE data
        systems = load_systems(args.sde)
        stations = load_stations(args.sde, systems)

        # Find start and end stations
        if args.start not in stations:
            print(f"Error: Start station '{args.start}' not found", file=sys.stderr)
            sys.exit(1)

        if args.end not in stations:
            print(f"Error: End station '{args.end}' not found", file=sys.stderr)
            sys.exit(1)

        start_station = stations[args.start]
        end_station = stations[args.end]

        # Validate end station is not in high sec, pochven, or zarzakh
        end_system = systems[end_station.system_id]
        if not is_valid_destination(end_system):
            print(f"Error: End station cannot be in High Sec, Pochven, or Zarzakh", file=sys.stderr)
            sys.exit(1)

        # Plan the jumps
        plan = plan_jumps(
            start_station, end_station, systems, stations,
            float(args.range), args.fuel, args.reduction
        )

        # Output the plan
        print(f"START: {plan.start_station}")

        for jump in plan.jumps:
            print("UNDOCK")
            print(f"JUMP {jump.distance_ly:.2f} LY: {jump.target_system} ({format_isotopes(jump.isotopes)} isotopes)")
            print(f"DOCK: {jump.target_station}")

        print("SUMMARY:")
        print(f"  End Cooldown: {format_time(plan.end_cooldown)}")
        print(f"  End Fatigue: {format_time(plan.end_fatigue)}")
        print(f"  Isotopes Used: {format_isotopes(plan.total_isotopes)}")
        print(f"  Time Waiting: {format_time(plan.total_wait_time)}")
        print(f"  Total LY: {plan.total_ly:.2f}")

    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
