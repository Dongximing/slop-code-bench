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
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import heapq

# Constants
LY_IN_METERS = 9460730472580800.0
MAX_FATIGUE_MINUTES = 300
MAX_COOLDOWN_MINUTES = 30


@dataclass
class System:
    system_id: int
    name: str
    x: float
    y: float
    z: float
    security: float
    region_id: int = 0


@dataclass
class Station:
    station_id: int
    name: str
    system_id: int
    security: float


@dataclass
class Jump:
    distance_ly: float
    target_system: str
    target_station: str
    isotopes: int
    wait_time: float


@dataclass
class JumpPlan:
    start_station: str
    jumps: List[Jump] = field(default_factory=list)
    total_isotopes: int = 0
    total_ly: float = 0.0
    total_wait_time: float = 0.0
    end_fatigue: float = 0.0
    end_cooldown: float = 0.0


def _load_csv_rows(sde_path: str, basename: str) -> list:
    """Load CSV rows from bz2 or plain CSV, trying both extensions."""
    for ext in ('.csv.bz2', '.csv'):
        filepath = os.path.join(sde_path, basename + ext)
        if not os.path.exists(filepath):
            continue
        opener = bz2.open if ext == '.csv.bz2' else open
        with opener(filepath, 'rt', encoding='utf-8') as f:
            return list(csv.DictReader(f))
    raise FileNotFoundError(f"Could not find {basename} data in {sde_path}")


def load_systems(sde_path: str) -> Dict[int, System]:
    systems = {}
    for row in _load_csv_rows(sde_path, 'mapSolarSystems'):
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
    stations = {}
    for row in _load_csv_rows(sde_path, 'staStations'):
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
    return (round(system.security, 1) < 0.5
            and system.region_id != 10000082
            and system.name.lower() != 'zarzakh')


def compute_jump_fatigue(fatigue: float, cooldown: float,
                          effective_distance: float) -> Tuple[float, float]:
    base_fatigue = max(fatigue, 10.0)
    new_fatigue = min(base_fatigue * (1.0 + effective_distance), MAX_FATIGUE_MINUTES)
    if fatigue == 0 and cooldown == 0:
        new_cooldown = min(1.0 + effective_distance, MAX_COOLDOWN_MINUTES)
    else:
        new_cooldown = min(base_fatigue / 10.0, MAX_COOLDOWN_MINUTES)
    return new_fatigue, new_cooldown


def calculate_isotopes(distance_ly: float, fuel_per_jump: int) -> int:
    """Calculate isotopes needed (rounded up to thousands)"""
    raw_isotopes = distance_ly * fuel_per_jump
    return math.ceil(raw_isotopes / 1000) * 1000


def get_station_in_system(system_id: int, stations: Dict[str, Station],
                          preferred: Optional[str] = None) -> Optional[str]:
    system_stations = [n for n, s in stations.items() if s.system_id == system_id]
    if preferred and preferred in system_stations:
        return preferred
    return sorted(system_stations)[0] if system_stations else None


def _build_plan(path, start_station, end_station, systems, stations,
                reduction_factor, fuel_per_jump, total_ly):
    plan = JumpPlan(start_station=start_station.name)
    fatigue, cooldown = 0.0, 0.0

    for i, (target_sys_id, dist_ly) in enumerate(path):
        target_system = systems[target_sys_id]
        effective_dist = dist_ly * reduction_factor
        wait_needed = max(0.0, cooldown)

        if wait_needed > 0:
            fatigue = max(0.0, fatigue - wait_needed)
            cooldown = 0.0

        isotopes = calculate_isotopes(dist_ly, fuel_per_jump)
        target_station_name = (end_station.name if i == len(path) - 1
                               else get_station_in_system(target_sys_id, stations))

        plan.jumps.append(Jump(
            distance_ly=dist_ly,
            target_system=target_system.name,
            target_station=target_station_name,
            isotopes=isotopes,
            wait_time=wait_needed
        ))
        plan.total_isotopes += isotopes
        plan.total_wait_time += wait_needed
        fatigue, cooldown = compute_jump_fatigue(fatigue, cooldown, effective_dist)

    plan.total_ly = total_ly
    plan.end_fatigue = fatigue
    plan.end_cooldown = cooldown
    return plan


def plan_jumps(start_station: Station, end_station: Station,
               systems: Dict[int, System], stations: Dict[str, Station],
               max_range_ly: float, fuel_per_jump: int,
               reduction_percent: int) -> JumpPlan:
    start_system = systems[start_station.system_id]
    end_system = systems[end_station.system_id]
    reduction_factor = (100.0 - reduction_percent) / 100.0

    candidate_systems = {sid: s for sid, s in systems.items()
                         if is_valid_destination(s) or s.system_id == end_system.system_id}

    # Heap entries: (num_jumps, total_wait, total_ly, sys_name, sys_id,
    #                fatigue, cooldown, tiebreak_counter, path)
    counter = 0
    pq = [(0, 0.0, 0.0, start_system.name, start_system.system_id,
           0.0, 0.0, counter, [])]

    visited = defaultdict(set)

    while pq:
        num_jumps, total_wait, total_ly, sys_name, sys_id, fatigue, cooldown, _, path = heapq.heappop(pq)

        if sys_id == end_system.system_id:
            return _build_plan(path, start_station, end_station, systems,
                               stations, reduction_factor, fuel_per_jump, total_ly)

        fatigue_bucket = round(fatigue, 0)
        if fatigue_bucket in visited[sys_id]:
            continue
        visited[sys_id].add(fatigue_bucket)

        current_system = systems[sys_id]

        for target_system in candidate_systems.values():
            if target_system.system_id == sys_id:
                continue

            dist_ly = calculate_distance_ly(current_system, target_system)
            if dist_ly > max_range_ly:
                continue

            effective_dist = dist_ly * reduction_factor
            wait_needed = max(0.0, cooldown)
            fat_after_wait = max(0.0, fatigue - wait_needed)

            new_fatigue, new_cooldown = compute_jump_fatigue(
                fat_after_wait, 0.0, effective_dist
            )

            counter += 1
            heapq.heappush(pq, (
                num_jumps + 1,
                total_wait + wait_needed,
                total_ly + dist_ly,
                target_system.name,
                target_system.system_id,
                new_fatigue, new_cooldown,
                counter,
                path + [(target_system.system_id, dist_ly)]
            ))

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

    if not (1 <= args.fuel <= 10000):
        print("Error: --fuel must be between 1 and 10000", file=sys.stderr)
        sys.exit(1)

    if not (0 <= args.reduction <= 100):
        print("Error: --reduction must be between 0 and 100", file=sys.stderr)
        sys.exit(1)

    try:
        systems = load_systems(args.sde)
        stations = load_stations(args.sde, systems)

        if args.start not in stations:
            print(f"Error: Start station '{args.start}' not found", file=sys.stderr)
            sys.exit(1)

        if args.end not in stations:
            print(f"Error: End station '{args.end}' not found", file=sys.stderr)
            sys.exit(1)

        start_station = stations[args.start]
        end_station = stations[args.end]

        end_system = systems[end_station.system_id]
        if not is_valid_destination(end_system):
            print(f"Error: End station cannot be in High Sec, Pochven, or Zarzakh", file=sys.stderr)
            sys.exit(1)

        plan = plan_jumps(
            start_station, end_station, systems, stations,
            float(args.range), args.fuel, args.reduction
        )

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

    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
