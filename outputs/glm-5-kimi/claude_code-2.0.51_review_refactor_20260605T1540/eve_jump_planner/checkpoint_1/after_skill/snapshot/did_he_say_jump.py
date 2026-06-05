#!/usr/bin/env python3
"""
Jump Freighter Logistics Planning Tool

Plans optimal jump routes for Jump Freighters, calculating fuel consumption,
fatigue, and cooldown timers.
"""

import argparse
import bz2
import csv
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict


# Constants
LY_IN_METERS = 9460730472580800.0
DEFAULT_JUMP_RANGE = 10
DEFAULT_FUEL = 10000
DEFAULT_REDUCTION = 90
MAX_FATIGUE_MINUTES = 300
MAX_COOLDOWN_MINUTES = 30


@dataclass
class SolarSystem:
    """Represents a solar system in EVE."""
    system_id: int
    name: str
    x: float
    y: float
    z: float
    security: float
    security_class: str
    region_id: int
    constellation_id: int

    def is_high_sec(self) -> bool:
        return round(self.security, 1) >= 0.5

    def is_pochven(self) -> bool:
        return self.region_id == 10000008

    def is_zarzakh(self) -> bool:
        return self.name.lower() == "zarzakh"

    def is_valid_destination(self) -> bool:
        return not (self.is_high_sec() or self.is_pochven() or self.is_zarzakh())


@dataclass
class Station:
    """Represents a station in EVE."""
    station_id: int
    name: str
    system_id: int
    x: float
    y: float
    z: float


@dataclass
class JumpState:
    """Tracks fatigue and cooldown state."""
    fatigue_minutes: float = 0.0
    cooldown_minutes: float = 0.0
    time_waiting: float = 0.0

    def apply_jump(self, effective_distance: float) -> None:
        if self.fatigue_minutes == 0 and self.cooldown_minutes == 0:
            # First jump or after full recovery
            self.fatigue_minutes = 10 * (1 + effective_distance)
            self.cooldown_minutes = 1 + effective_distance
        else:
            # Subsequent jumps
            base_fatigue = max(self.fatigue_minutes, 10)
            self.fatigue_minutes = base_fatigue * (1 + effective_distance)
            # For subsequent jumps, cooldown is floor(fatigue / 10)
            self.cooldown_minutes = math.floor(self.fatigue_minutes / 10)

        # Apply caps
        self.fatigue_minutes = min(self.fatigue_minutes, MAX_FATIGUE_MINUTES)
        self.cooldown_minutes = min(self.cooldown_minutes, MAX_COOLDOWN_MINUTES)

        # Ensure non-negative
        self.fatigue_minutes = max(self.fatigue_minutes, 0)
        self.cooldown_minutes = max(self.cooldown_minutes, 0)

    def wait_for_cooldown(self) -> None:
        """Wait out the cooldown before next jump."""
        if self.cooldown_minutes > 0:
            # Use exact cooldown time for fatigue decay
            wait_time = self.cooldown_minutes
            # Add to time_waiting (ceiling for display)
            self.time_waiting += wait_time
            # Fatigue decays during the wait
            self.fatigue_minutes = max(0, self.fatigue_minutes - wait_time)
            self.cooldown_minutes = 0

    def decay(self, minutes: float) -> None:
        self.fatigue_minutes = max(0, self.fatigue_minutes - minutes)
        self.cooldown_minutes = max(0, self.cooldown_minutes - minutes)


@dataclass
class JumpLeg:
    """Represents a single jump leg."""
    from_system: str
    to_system: str
    distance_ly: float
    station_name: str
    isotopes_used: int
    fatigue_after: float
    cooldown_after: float


@dataclass
class Route:
    """Represents a complete route with all legs."""
    start_station: str
    legs: List[JumpLeg] = field(default_factory=list)
    total_isotopes: int = 0
    total_ly: float = 0.0
    total_time_waiting: float = 0.0
    end_fatigue: float = 0.0
    end_cooldown: float = 0.0


class SDELoader:
    """Loads and processes EVE Static Data Export."""

    def __init__(self, sde_dir: str):
        self.sde_dir = sde_dir
        self.systems: Dict[int, SolarSystem] = {}
        self.systems_by_name: Dict[str, SolarSystem] = {}
        self.stations: Dict[int, Station] = {}
        self.stations_by_name: Dict[str, Station] = {}
        self.stations_by_system: Dict[int, List[Station]] = defaultdict(list)

    def load(self) -> None:
        self._load_systems()
        self._load_stations()

    def _load_systems(self) -> None:
        systems_file = os.path.join(self.sde_dir, 'mapSolarSystems.csv.bz2')

        with bz2.open(systems_file, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                system = SolarSystem(
                    system_id=int(row['solarSystemID']),
                    name=row['solarSystemName'],
                    x=float(row['x']),
                    y=float(row['y']),
                    z=float(row['z']),
                    security=float(row['security']),
                    security_class=row.get('securityClass', ''),
                    region_id=int(row['regionID']),
                    constellation_id=int(row['constellationID'])
                )
                self.systems[system.system_id] = system
                self.systems_by_name[system.name] = system

    def _load_stations(self) -> None:
        stations_file = os.path.join(self.sde_dir, 'staStations.csv.bz2')

        with bz2.open(stations_file, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                station = Station(
                    station_id=int(row['stationID']),
                    name=row['stationName'],
                    system_id=int(row['solarSystemID']),
                    x=float(row['x']),
                    y=float(row['y']),
                    z=float(row['z'])
                )
                self.stations[station.station_id] = station
                self.stations_by_name[station.name] = station
                self.stations_by_system[station.system_id].append(station)

    def get_system_by_name(self, name: str) -> Optional[SolarSystem]:
        return self.systems_by_name.get(name)

    def get_system_by_id(self, system_id: int) -> Optional[SolarSystem]:
        return self.systems.get(system_id)

    def get_station_by_name(self, name: str) -> Optional[Station]:
        return self.stations_by_name.get(name)

    def get_stations_in_system(self, system_id: int) -> List[Station]:
        return self.stations_by_system.get(system_id, [])


def calculate_distance_ly(sys1: SolarSystem, sys2: SolarSystem) -> float:
    dx = sys2.x - sys1.x
    dy = sys2.y - sys1.y
    dz = sys2.z - sys1.z

    distance_meters = math.sqrt(dx * dx + dy * dy + dz * dz)
    distance_ly = distance_meters / LY_IN_METERS

    return distance_ly


def calculate_isotopes(distance_ly: float, base_fuel: int) -> int:
    isotopes = distance_ly * base_fuel
    isotopes_rounded = math.ceil(isotopes / 1000) * 1000
    return int(isotopes_rounded)


def format_time(minutes: float) -> str:
    total_minutes = math.ceil(minutes)
    hours = total_minutes // 60
    mins = total_minutes % 60
    return f"{hours:02d}:{mins:02d}"


def format_isotopes(isotopes: int) -> str:
    thousands = isotopes // 1000
    return f"{thousands}K"


class JumpFreightPlanner:
    """Plans optimal jump freighter routes."""

    def __init__(self, sde: SDELoader, max_range: float, base_fuel: int, reduction: int):
        self.sde = sde
        self.max_range = max_range
        self.base_fuel = base_fuel
        self.reduction = reduction

    def plan_route(self, start_station_name: str, end_station_name: str) -> Route:
        start_station = self.sde.get_station_by_name(start_station_name)
        if not start_station:
            raise ValueError(f"Start station not found: {start_station_name}")

        end_station = self.sde.get_station_by_name(end_station_name)
        if not end_station:
            raise ValueError(f"End station not found: {end_station_name}")

        start_system = self.sde.get_system_by_id(start_station.system_id)
        end_system = self.sde.get_system_by_id(end_station.system_id)

        if not start_system or not end_system:
            raise ValueError("Could not find systems for stations")

        if not end_system.is_valid_destination():
            raise ValueError(
                f"End station must not be in High Sec, Pochven, or Zarzakh. "
                f"System {end_system.name} security: {end_system.security}"
            )

        return self._find_optimal_route(start_station, end_station, start_system, end_system)

    def _find_optimal_route(
        self,
        start_station: Station,
        end_station: Station,
        start_system: SolarSystem,
        end_system: SolarSystem
    ) -> Route:
        direct_distance = calculate_distance_ly(start_system, end_system)

        if direct_distance <= self.max_range:
            return self._create_single_jump_route(
                start_station, end_station, start_system, end_system
            )

        return self._find_multi_jump_route(
            start_station, end_station, start_system, end_system
        )

    def _create_single_jump_route(
        self,
        start_station: Station,
        end_station: Station,
        start_system: SolarSystem,
        end_system: SolarSystem
    ) -> Route:
        distance_ly = calculate_distance_ly(start_system, end_system)
        isotopes = calculate_isotopes(distance_ly, self.base_fuel)

        state = JumpState()
        effective_distance = distance_ly * (1 - self.reduction / 100.0)
        state.apply_jump(effective_distance)

        leg = JumpLeg(
            from_system=start_system.name,
            to_system=end_system.name,
            distance_ly=distance_ly,
            station_name=end_station.name,
            isotopes_used=isotopes,
            fatigue_after=state.fatigue_minutes,
            cooldown_after=state.cooldown_minutes
        )

        return Route(
            start_station=start_station.name,
            legs=[leg],
            total_isotopes=isotopes,
            total_ly=distance_ly,
            total_time_waiting=state.time_waiting,
            end_fatigue=state.fatigue_minutes,
            end_cooldown=state.cooldown_minutes
        )

    def _find_multi_jump_route(
        self,
        start_station: Station,
        end_station: Station,
        start_system: SolarSystem,
        end_system: SolarSystem
    ) -> Route:
        route = Route(start_station=start_station.name)
        state = JumpState()

        current_system = start_system
        remaining_systems = [end_system]
        remaining_stations = [end_station]

        while remaining_systems:
            target_system = remaining_systems[0]
            target_station = remaining_stations[0]

            direct_distance = calculate_distance_ly(current_system, target_system)

            if direct_distance <= self.max_range:
                isotopes = calculate_isotopes(direct_distance, self.base_fuel)
                effective_distance = direct_distance * (1 - self.reduction / 100.0)

                state.wait_for_cooldown()
                state.apply_jump(effective_distance)

                leg = JumpLeg(
                    from_system=current_system.name,
                    to_system=target_system.name,
                    distance_ly=direct_distance,
                    station_name=target_station.name,
                    isotopes_used=isotopes,
                    fatigue_after=state.fatigue_minutes,
                    cooldown_after=state.cooldown_minutes
                )

                route.legs.append(leg)
                route.total_isotopes += isotopes
                route.total_ly += direct_distance
                route.total_time_waiting = state.time_waiting
                route.end_fatigue = state.fatigue_minutes
                route.end_cooldown = state.cooldown_minutes

                remaining_systems.pop(0)
                remaining_stations.pop(0)
                current_system = target_system
            else:
                midpoint, midpoint_station = self._find_best_midpoint(
                    current_system, target_system, state
                )

                if midpoint is None:
                    raise ValueError(f"Cannot find valid midpoint from {current_system.name} to {target_system.name}")

                remaining_systems.insert(0, midpoint)
                remaining_stations.insert(0, midpoint_station)

        return route

    def _find_best_midpoint(
        self,
        current_system: SolarSystem,
        target_system: SolarSystem,
        state: JumpState
    ) -> Tuple[Optional[SolarSystem], Optional[Station]]:
        candidates = []

        for system in self.sde.systems.values():
            if not system.is_valid_destination():
                continue

            dist_from_current = calculate_distance_ly(current_system, system)
            if dist_from_current > self.max_range or dist_from_current < 0.01:
                continue

            dist_to_target = calculate_distance_ly(system, target_system)

            temp_state = JumpState(
                fatigue_minutes=state.fatigue_minutes,
                cooldown_minutes=state.cooldown_minutes
            )

            temp_state.wait_for_cooldown()

            effective_dist = dist_from_current * (1 - self.reduction / 100.0)
            temp_state.apply_jump(effective_dist)

            temp_state.wait_for_cooldown()
            if dist_to_target <= self.max_range:
                effective_dist_target = dist_to_target * (1 - self.reduction / 100.0)
                temp_state.apply_jump(effective_dist_target)

            total_time_waiting = temp_state.time_waiting
            total_trip_ly = dist_from_current + dist_to_target

            stations = self.sde.get_stations_in_system(system.system_id)
            if not stations:
                continue

            station = stations[0]

            candidates.append({
                'system': system,
                'station': station,
                'jumps_estimate': 1 + (2 if dist_to_target > self.max_range else 1),
                'time_waiting': total_time_waiting,
                'time_waiting_ceiled': math.ceil(total_time_waiting),
                'total_ly': total_trip_ly,
                'dist_from_current': dist_from_current,
                'dist_to_target': dist_to_target
            })

        if not candidates:
            return None, None

        candidates.sort(key=lambda c: (
            c['jumps_estimate'],
            c['time_waiting_ceiled'],
            c['total_ly'],
            c['system'].name
        ))

        best = candidates[0]
        return best['system'], best['station']


def print_route(route: Route) -> None:
    """Print the route in the specified format."""
    print(f"START: {route.start_station}")

    for leg in route.legs:
        print("UNDOCK")
        print(f"JUMP {leg.distance_ly:.2f} LY: {leg.to_system} ({format_isotopes(leg.isotopes_used)} isotopes)")
        print(f"DOCK: {leg.station_name}")

    print("SUMMARY:")
    print(f"  End Cooldown: {format_time(route.end_cooldown)}")
    print(f"  End Fatigue: {format_time(route.end_fatigue)}")
    print(f"  Isotopes Used: {format_isotopes(route.total_isotopes)}")
    print(f"  Time Waiting: {format_time(route.total_time_waiting)}")
    print(f"  Total LY: {route.total_ly:.2f}")


def main():
    parser = argparse.ArgumentParser(
        description='Jump Freighter Logistics Planning Tool'
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
        type=int,
        default=DEFAULT_JUMP_RANGE,
        choices=range(5, 11),
        metavar='[5-10]',
        help=f'Max LY range for a jump (default: {DEFAULT_JUMP_RANGE})'
    )

    parser.add_argument(
        '--fuel',
        type=int,
        default=DEFAULT_FUEL,
        choices=range(1, 10001),
        metavar='[1-10000]',
        help=f'Isotopes per jump (default: {DEFAULT_FUEL})'
    )

    parser.add_argument(
        '--reduction',
        type=int,
        default=DEFAULT_REDUCTION,
        choices=range(0, 101),
        metavar='[0-100]',
        help=f'Effective jump distance reduction %% (default: {DEFAULT_REDUCTION})'
    )

    args = parser.parse_args()

    # Validate SDE directory
    if not os.path.isdir(args.sde):
        print(f"Error: SDE directory not found: {args.sde}", file=sys.stderr)
        sys.exit(1)

    # Load SDE data
    try:
        sde = SDELoader(args.sde)
        sde.load()
    except Exception as e:
        print(f"Error loading SDE data: {e}", file=sys.stderr)
        sys.exit(1)

    # Plan route
    try:
        planner = JumpFreightPlanner(
            sde=sde,
            max_range=args.range,
            base_fuel=args.fuel,
            reduction=args.reduction
        )
        route = planner.plan_route(args.start, args.end)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error planning route: {e}", file=sys.stderr)
        sys.exit(1)

    # Print the route
    print_route(route)


if __name__ == '__main__':
    main()
