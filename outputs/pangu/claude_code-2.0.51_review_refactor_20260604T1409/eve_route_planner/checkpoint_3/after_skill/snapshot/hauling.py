#!/usr/bin/env python3
"""
Cargo hauling operations with manifest files and ship information.
"""

import argparse
import csv
import sys
import re
import bz2
import json
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict


def parse_yaml_simple(content: str) -> dict:
    """Parse a simple subset of YAML without external dependencies."""
    result = {}
    lines = content.split('\n')
    i = 0

    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.strip().startswith('#'):
            i += 1
            continue

        # Parse key at current level
        kv_match = re.match(r'^(\S+):\s*(.*)', line)
        if not kv_match:
            i += 1
            continue

        key = kv_match.group(1)
        value = kv_match.group(2).strip()

        # Handle inline empty list/object notation
        if value == '[]':
            result[key] = []
            i += 1
            continue
        elif value == '{}':
            result[key] = {}
            i += 1
            continue

        # If value is empty, it might be a nested object/list
        if value == '':
            # Check if next lines are list items
            has_list = False
            has_object = False

            j = i + 1
            while j < len(lines):
                nested_line = lines[j]
                if not nested_line.strip():
                    j += 1
                    continue

                # Check indentation (2 spaces for nested level)
                if nested_line.startswith('  '):
                    if nested_line.strip().startswith('- '):
                        has_list = True
                    else:
                        has_object = True
                    break
                else:
                    break

            if has_list:
                # Parse list
                result[key] = parse_yaml_list(lines, i + 1)
                # Skip parsed lines
                list_end = find_list_end(lines, i + 1)
                i = list_end
                continue
            elif has_object:
                # Parse nested object
                result[key] = {}
                obj_dict, end_idx = parse_yaml_object(lines, i + 1)
                result[key].update(obj_dict)
                i = end_idx
                continue
            else:
                # Empty list or object
                result[key] = []
                i += 1
                continue

        # Handle simple key-value
        result[key] = parse_yaml_value(value)
        i += 1

    return result


def parse_yaml_list(lines: list, start_idx: int) -> list:
    """Parse a YAML list starting at the given line."""
    result = []
    i = start_idx

    while i < len(lines):
        line = lines[i]

        # Skip empty lines
        if not line.strip():
            i += 1
            continue

        # Check if we've exited the list (less indentation)
        if not line.startswith('  '):
            break

        # Handle list item '- '
        if line.strip().startswith('- '):
            item_content = line.strip()[2:].strip()

            # Check for nested object in list item
            j = i + 1
            has_nested = False
            while j < len(lines) and lines[j].startswith('    '):
                has_nested = True
                j += 1

            if has_nested and ':' in item_content:
                # Parse key-value pair and nested fields
                kv_match = re.match(r'(\w+):\s*(.*)', item_content)
                if kv_match:
                    k = kv_match.group(1)
                    v = kv_match.group(2).strip()
                    item_dict = {k: parse_yaml_value(v) if v else {}}

                    # Parse nested fields
                    nested_obj, end_idx = parse_yaml_object(lines, i + 1, level=4)
                    item_dict.update(nested_obj)
                    result.append(item_dict)
                    i = end_idx
                    continue
            elif item_content:
                # Simple value in list
                result.append(parse_yaml_value(item_content) if parse_yaml_value(item_content) is not None else item_content)

        i += 1

    return result


def find_list_end(lines: list, start_idx: int) -> int:
    """Find the end of a YAML list."""
    i = start_idx
    while i < len(lines):
        if not lines[i].startswith('  ') and lines[i].strip():
            break
        i += 1
    return i


def parse_yaml_object(lines: list, start_idx: int, level: int = 2) -> tuple:
    """Parse a nested YAML object."""
    result = {}
    i = start_idx
    indent = ' ' * level

    while i < len(lines):
        line = lines[i]

        # Check if we've exited the object (less indentation)
        if line.strip() and not line.startswith(indent):
            break

        if not line.strip():
            i += 1
            continue

        # Parse key-value
        # Remove the indentation level
        content = line[len(indent):]
        kv_match = re.match(r'^(\S+):\s*(.*)', content)
        if kv_match:
            key = kv_match.group(1)
            value = kv_match.group(2).strip()

            if value:
                parsed_value = parse_yaml_value(value)
                result[key] = parsed_value if parsed_value is not None else value
            else:
                # Check for nested
                j = i + 1
                if j < len(lines) and lines[j].startswith(indent + '  '):
                    sub_obj, end_idx = parse_yaml_object(lines, i + 1, level + 2)
                    result[key] = sub_obj
                    i = end_idx
                    continue
                else:
                    result[key] = {}
        i += 1

    return result, i


def parse_yaml_value(value: str):
    """Parse a YAML value string into Python type."""
    if not value:
        return None

    # Handle null values
    if value.lower() in ('null', '~'):
        return None

    # Handle booleans
    if value.lower() == 'true':
        return True
    if value.lower() == 'false':
        return False

    # Handle integers
    if re.match(r'^-?\d+$', value):
        return int(value)

    # Handle floats
    if re.match(r'^-?\d+\.\d*$', value):
        return float(value)

    # Handle strings (may contain quotes)
    if (value.startswith('"') and value.endswith('"')) or \
       (value.startswith("'") and value.endswith("'")):
        return value[1:-1]

    return value


def parse_yaml_file(filepath: str) -> dict:
    """Parse a YAML file."""
    with open(filepath, 'r') as f:
        content = f.read()
    return parse_yaml_simple(content)


@dataclass
class SolarSystem:
    id: int
    name: str
    security: float
    x: float
    y: float
    z: float


@dataclass
class Station:
    id: int
    name: str
    solar_system_id: int
    security: float


@dataclass
class Ship:
    name: str
    ship_type: str
    align: float
    top_speed: float
    warp_speed: float
    cargo_size: int
    ehp: Optional[float] = None


@dataclass
class Config:
    ships: dict
    times: dict
    min_isk_per_jump: Optional[float] = None
    max_isk_per_ehp: Optional[float] = None


@dataclass
class Waypoint:
    name: str
    cargo: Optional[float]


@dataclass
class Manifest:
    start_cargo: Optional[float]
    waypoints: list


@dataclass
class Trip:
    operations: list
    total_cargo: float
    total_time: float


@dataclass
class Contract:
    id: int
    start: str
    end: str
    collateral: float
    m3: float
    actual_value: float
    reward: float
    issuer: str


class SDE:
    """Static Data Export parser for EVE Online."""

    def __init__(self, sde_path: str):
        self.sde_path = sde_path
        self.systems_by_id: dict[int, SolarSystem] = {}
        self.systems_by_name: dict[str, SolarSystem] = {}
        self.stations_by_name: dict[str, Station] = {}
        self.stations_by_system: dict[int, list[Station]] = defaultdict(list)
        self.graph: dict[int, list[int]] = defaultdict(list)
        self.system_security: dict[int, float] = {}

    def load(self):
        """Load all SDE data."""
        self._load_systems()
        self._load_jumps()
        self._load_stations()

    def _load_systems(self):
        """Load solar systems from mapSolarSystems.csv."""
        filepath = f"{self.sde_path}/mapSolarSystems.csv.bz2"
        with bz2.open(filepath, 'rt') as f:
            reader = csv.DictReader(f)
            for row in reader:
                system = SolarSystem(
                    id=int(row['solarSystemID']),
                    name=row['solarSystemName'],
                    security=float(row['security']),
                    x=float(row['x']),
                    y=float(row['y']),
                    z=float(row['z'])
                )
                self.systems_by_id[system.id] = system
                self.systems_by_name[system.name.lower()] = system
                self.system_security[system.id] = system.security

    def _load_jumps(self):
        """Load solar system jumps from mapSolarSystemJumps.csv."""
        filepath = f"{self.sde_path}/mapSolarSystemJumps.csv.bz2"
        with bz2.open(filepath, 'rt') as f:
            reader = csv.DictReader(f)
            for row in reader:
                from_id = int(row['fromSolarSystemID'])
                to_id = int(row['toSolarSystemID'])
                self.graph[from_id].append(to_id)

    def _load_stations(self):
        """Load stations from staStations.csv."""
        filepath = f"{self.sde_path}/staStations.csv.bz2"
        with bz2.open(filepath, 'rt') as f:
            reader = csv.DictReader(f)
            for row in reader:
                station = Station(
                    id=int(row['stationID']),
                    name=row['stationName'],
                    solar_system_id=int(row['solarSystemID']),
                    security=float(row['security'])
                )
                self.stations_by_name[station.name.lower()] = station
                self.stations_by_system[station.solar_system_id].append(station)

    def find_station_by_name(self, name: str) -> Optional[Station]:
        """Find a station by its full name (case-insensitive)."""
        return self.stations_by_name.get(name.lower())

    def find_system_by_name(self, name: str) -> Optional[SolarSystem]:
        """Find a solar system by name (case-insensitive)."""
        return self.systems_by_name.get(name.lower())


class RouteFinder:
    """Find routes between stations considering security constraints."""

    def __init__(self, sde: SDE):
        self.sde = sde

    def find_route(self, start_station: Station, end_station: Station,
                   ship: Ship, allow_low_sec_only: bool = False) -> Optional[list]:
        """Find a route between two stations using BFS."""
        if start_station.solar_system_id == end_station.solar_system_id:
            return [end_station]

        # BFS to find shortest path in terms of jumps
        visited = {start_station.solar_system_id}
        queue = [[start_station]]

        while queue:
            path = queue.pop(0)
            current_system = path[-1].solar_system_id

            for neighbor_id in self.sde.graph[current_system]:
                if neighbor_id == end_station.solar_system_id:
                    full_path = path + [end_station]
                    return self._build_route_output(full_path, ship, allow_low_sec_only)

                if neighbor_id in visited:
                    continue

                visited.add(neighbor_id)
                neighbor_system = self.sde.systems_by_id.get(neighbor_id)
                if neighbor_system:
                    station_list = self.sde.stations_by_system.get(neighbor_id, [])
                    if station_list:
                        # Use the first station in the system
                        queue.append(path + [station_list[0]])

        return None

    def count_jumps(self, start_station_id: int, end_station_id: int) -> Optional[int]:
        """Count minimum jumps between two stations using BFS."""
        if start_station_id == end_station_id:
            return 0

        visited = {start_station_id}
        queue = [(start_station_id, 0)]

        while queue:
            current_id, dist = queue.pop(0)

            for neighbor_id in self.sde.graph[current_id]:
                if neighbor_id == end_station_id:
                    return dist + 1

                if neighbor_id not in visited:
                    visited.add(neighbor_id)
                    queue.append((neighbor_id, dist + 1))

        return None

    def _build_route_output(self, route: list, ship: Ship,
                            allow_low_sec_only: bool) -> list:
        """Build route output with security levels."""
        segments = []
        for i, station in enumerate(route):
            system = self.sde.systems_by_id.get(station.solar_system_id)
            if system:
                security = system.security
                segments.append({
                    'name': system.name,
                    'security': security
                })
        return segments

    def get_route_string(self, route_segments: list) -> str:
        """Convert route segments to output string."""
        return " -> ".join([f"{s['name']} ({s['security']:.1f})" for s in route_segments])


class HaulingPlanner:
    """Plan cargo hauling operations across multiple trips."""

    def __init__(self, sde: SDE, config: Config):
        self.sde = sde
        self.config = config
        self.route_finder = RouteFinder(sde)

    def plan_manifest(self, start_location: str, end_location: str,
                      manifest: Manifest, ship_name: str) -> list[Trip]:
        """Plan all trips needed to fulfill the manifest."""
        ship = self.config.ships.get(ship_name)
        if not ship:
            raise ValueError(f"Ship '{ship_name}' not found in config")

        start_station = self.sde.find_station_by_name(start_location)
        end_station = self.sde.find_station_by_name(end_location)

        if not start_station:
            raise ValueError(f"Start location '{start_location}' not found")
        if not end_station:
            raise ValueError(f"End location '{end_location}' not found")

        # Extract cargo operations
        operations = []
        total_cargo = 0.0

        # Add start cargo
        if manifest.start_cargo and manifest.start_cargo > 0:
            operations.append({
                'type': 'LOAD',
                'location': start_location,
                'amount': manifest.start_cargo
            })
            total_cargo += manifest.start_cargo

        # Add waypoint operations
        for wp in manifest.waypoints:
            if wp.cargo and wp.cargo > 0:
                operations.append({
                    'type': 'LOAD',
                    'location': wp.name,
                    'amount': wp.cargo
                })
                total_cargo += wp.cargo

        if total_cargo == 0:
            return []

        # Calculate needed trips
        trips = []
        remaining = list(operations)
        cargo_per_trip = ship.cargo_size

        # Check for freighter restrictions
        is_freighter = ship.ship_type == "Freighter"

        # For now, all cargo in one or more trips
        if total_cargo <= cargo_per_trip:
            trips.append(self._plan_single_trip(
                start_location, end_location, operations, ship,
                is_freighter, []
            ))
        else:
            # Multiple trips needed
            # Divide operations across trips
            current_cargo = 0
            current_ops = []
            trip_operations = [op.copy() for op in operations if op['amount'] > 0]

            # Sort operations by location to group by route
            # This is a simplified approach - we'll create trips based on cargo capacity
            for op in trip_operations:
                if current_cargo + op['amount'] <= cargo_per_trip:
                    current_cargo += op['amount']
                    current_ops.append(op)
                else:
                    if current_ops:
                        trips.append(self._plan_single_trip(
                            start_location, end_location, current_ops, ship,
                            is_freighter, []
                        ))
                    current_cargo = op['amount']
                    current_ops = [op]

            if current_ops:
                trips.append(self._plan_single_trip(
                    start_location, end_location, current_ops, ship,
                    is_freighter, []
                ))

        return trips

    def _plan_single_trip(self, start_location: str, end_location: str,
                          operations: list, ship: Ship, is_freighter: bool,
                          prev_trip_operations: list) -> Trip:
        """Plan a single trip operation sequence."""
        trip_ops = []
        total_time = 0.0
        dock_time = self.config.times.get('dock', 0)
        gate_time = self.config.times.get('gate', 0)
        move_cargo_time = self.config.times.get('move_cargo', 0)

        start_station = self.sde.find_station_by_name(start_location)
        end_station = self.sde.find_station_by_name(end_location)

        if not start_station or not end_station:
            return Trip(operations=trip_ops, total_cargo=0, total_time=0)

        # Calculate route - simplified: direct route through BFS
        route = self.route_finder.find_route(start_station, end_station, ship,
                                             is_freighter)

        if route is None:
            # Try alternative routing for freighters (allow low-sec if needed)
            route = self.route_finder.find_route(start_station, end_station,
                                                 ship, True)

        route_str = self.route_finder.get_route_string(route) if route else "Unknown"

        # Start with UNDOCK from start
        trip_ops.append({'type': 'START', 'location': start_location})

        # Process operations in order of route
        current_loc = start_location
        remaining_ops = list(operations)
        cargo = 0

        # Add start cargo
        if remaining_ops and remaining_ops[0]['type'] == 'LOAD' and \
           remaining_ops[0]['location'] == start_location:
            load_op = remaining_ops.pop(0)
            trip_ops.append({
                'type': 'LOAD',
                'amount': load_op['amount']
            })
            cargo = load_op['amount']
            total_time += dock_time  # Time to dock/undock for loading
            total_time += move_cargo_time * 1  # Loading operation

        trip_ops.append({'type': 'UNDOCK'})
        total_time += dock_time

        # Add GO segment
        trip_ops.append({
            'type': 'GO',
            'route': route_str
        })
        # Calculate travel time: number of jumps * gate_time
        num_jumps = len(route) - 1 if route else 0
        total_time += num_jumps * gate_time

        # Process waypoints
        for wp_op in remaining_ops:
            wp_loc = wp_op['location']
            wp_station = self.sde.find_station_by_name(wp_loc)

            if wp_station:
                trip_ops.append({'type': 'DOCK', 'location': wp_loc})
                total_time += dock_time

                if wp_op['type'] == 'LOAD':
                    trip_ops.append({
                        'type': 'LOAD',
                        'amount': wp_op['amount']
                    })
                    cargo += wp_op['amount']
                    total_time += move_cargo_time

                trip_ops.append({'type': 'UNDOCK'})
                total_time += dock_time

        # Final segment to end location
        end_system = self.sde.systems_by_id.get(end_station.solar_system_id)
        trip_ops.append({'type': 'DOCK', 'location': end_location})
        total_time += dock_time

        trip_ops.append({
            'type': 'UNLOAD',
            'amount': cargo
        })

        return Trip(operations=trip_ops, total_cargo=cargo, total_time=total_time)


class ContractsPlanner:
    """Plan contract hauling operations."""

    def __init__(self, sde: SDE, config: Config):
        self.sde = sde
        self.config = config
        self.route_finder = RouteFinder(sde)

    def select_best_ship(self, trips: list) -> tuple[Ship, float]:
        """
        Select the best ship for the given trips.
        Minimize travel time. Tiebreaker: EHP then Name.
        Returns (ship, total_time).
        """
        best_ship = None
        best_time = float('inf')

        for ship in self.config.ships.values():
            total_time = self._calculate_total_time(trips, ship)

            if total_time is None:
                continue

            if total_time < best_time:
                best_time = total_time
                best_ship = ship
            elif total_time == best_time and best_ship:
                # Tiebreaker: higher EHP first, then name
                ship_ehp = ship.ehp if ship.ehp is not None else 0
                best_ehp = best_ship.ehp if best_ship.ehp is not None else 0
                if ship_ehp > best_ehp:
                    best_ship = ship
                    best_time = total_time
                elif ship_ehp == best_ehp and ship.name < best_ship.name:
                    best_ship = ship
                    best_time = total_time

        return best_ship, best_time

    def _calculate_total_time(self, trips: list, ship: Ship) -> Optional[float]:
        """Calculate total time for trips with a given ship."""
        dock_time = self.config.times.get('dock', 10.0)
        gate_time = self.config.times.get('gate', 30.0)
        move_cargo_time = self.config.times.get('move_cargo', 60.0)

        total_time = 0.0
        current_location = None

        for trip in trips:
            for stop in trip['stops']:
                loc = stop['location']
                if current_location is not None and loc != current_location:
                    # Travel time between consecutive stops
                    jumps = self.count_jumps(current_location, loc)
                    if jumps is None:
                        return None
                    total_time += jumps * gate_time

                # Dock/undock and cargo operations
                if stop['type'] == 'LOAD':
                    total_time += dock_time  # dock
                    total_time += move_cargo_time  # load
                    total_time += dock_time  # undock
                elif stop['type'] == 'UNLOAD':
                    total_time += dock_time  # dock
                    total_time += move_cargo_time  # unload
                    total_time += dock_time  # undock

                current_location = loc

            # Return to base after trip
            if trip['return']:
                jumps = self.count_jumps(current_location, trip['base'])
                if jumps is None:
                    return None
                total_time += jumps * gate_time

        return total_time

    def count_jumps(self, start: str, end: str) -> Optional[int]:
        """Count minimum jumps between two locations."""
        start_station = self.sde.find_station_by_name(start)
        end_station = self.sde.find_station_by_name(end)

        if not start_station or not end_station:
            return None

        return self.route_finder.count_jumps(
            start_station.solar_system_id,
            end_station.solar_system_id
        )

    def plan_contracts(self, base: str, contracts: list[Contract],
                       ship_name: str, max_time: Optional[int] = None) -> tuple[list, Ship]:
        """
        Plan the optimal set of contracts to fulfill.
        Returns (trips, ship) where trips is a list of trip dictionaries.
        """
        ship = self.config.ships.get(ship_name)
        if not ship:
            # We need to find the best ship first
            # Generate all possible trips with all ships
            all_trips = []
            for s in self.config.ships.values():
                trips = self._generate_trips(base, contracts, s, max_time)
                if trips:
                    ship_trips = (s, trips)
                    all_trips.append(ship_trips)

            if not all_trips:
                return [], None

            # Select ship with minimum time
            best_ship = None
            best_time = float('inf')
            best_trips = None

            for s, trips in all_trips:
                time = self._calculate_trips_time(trips, s)
                if time is not None and time < best_time:
                    best_time = time
                    best_ship = s
                    best_trips = trips
                elif time == best_time and best_ship:
                    ship_ehp = s.ehp if s.ehp is not None else 0
                    best_ehp = best_ship.ehp if best_ship.ehp is not None else 0
                    if ship_ehp > best_ehp:
                        best_ship = s
                        best_trips = trips
                        best_time = time
                    elif ship_ehp == best_ehp and s.name < best_ship.name:
                        best_ship = s
                        best_trips = trips
                        best_time = time

            return best_trips, best_ship
        else:
            trips = self._generate_trips(base, contracts, ship, max_time)
            return trips, ship

    def _calculate_trips_time(self, trips: list, ship: Ship) -> Optional[float]:
        """Calculate total time for a list of trips."""
        dock_time = self.config.times.get('dock', 10.0)
        gate_time = self.config.times.get('gate', 30.0)
        move_cargo_time = self.config.times.get('move_cargo', 60.0)

        total_time = 0.0

        for trip in trips:
            prev_loc = trip['base']

            for stop in trip['stops']:
                loc = stop['location']

                # Travel to this stop
                jumps = self.count_jumps(prev_loc, loc)
                if jumps is None:
                    return None
                total_time += jumps * gate_time

                # Cargo operation time
                if stop['type'] == 'LOAD':
                    total_time += dock_time + move_cargo_time + dock_time
                elif stop['type'] == 'UNLOAD':
                    total_time += dock_time + move_cargo_time + dock_time

                prev_loc = loc

            # Return to base
            if trip.get('return', True):
                jumps = self.count_jumps(prev_loc, trip['base'])
                if jumps is None:
                    return None
                total_time += jumps * gate_time

        return total_time

    def _filter_contracts(self, base: str, contracts: list[Contract]) -> list:
        """
        Filter contracts based on constraints.
        Returns list of valid contracts with additional computed fields.
        """
        filtered = []

        for contract in contracts:
            # Check if start and end locations exist
            start_station = self.sde.find_station_by_name(contract.start)
            end_station = self.sde.find_station_by_name(contract.end)

            if not start_station or not end_station:
                continue

            # Calculate jumps for the contract
            jumps_to_start = self.count_jumps(base, contract.start)
            jumps_to_end = self.count_jumps(base, contract.end)
            jumps_start_to_end = self.count_jumps(contract.start, contract.end)

            if jumps_to_start is None or jumps_to_end is None or jumps_start_to_end is None:
                continue

            # Total jumps for this contract (round trip through locations)
            # We need to pick up from start and deliver to end
            total_jumps = jumps_to_start + jumps_start_to_end + jumps_to_end

            # Calculate isk per jump for this contract
            if total_jumps > 0:
                isk_per_jump = contract.reward / total_jumps
            else:
                isk_per_jump = contract.reward

            # Apply min_isk_per_jump constraint
            if self.config.min_isk_per_jump is not None:
                if isk_per_jump < self.config.min_isk_per_jump:
                    continue

            # Get ship type for EHP check
            # For now, store the contract with computed values
            contract_info = {
                'contract': contract,
                'jumps_to_start': jumps_to_start,
                'jumps_to_end': jumps_to_end,
                'jumps_start_to_end': jumps_start_to_end,
                'total_jumps': total_jumps,
                'isk_per_jump': isk_per_jump
            }
            filtered.append(contract_info)

        return filtered

    def _generate_trips(self, base: str, contracts: list[Contract],
                        ship: Ship, max_time: Optional[int] = None) -> list:
        """
        Generate optimal trips for contracts.
        Uses a greedy approach with sorting by profitability.
        """
        # Filter contracts
        valid_contracts = self._filter_contracts(base, contracts)

        if not valid_contracts:
            return []

        # Apply max_isk_per_ehp constraint (skip for blockade runners)
        if self.config.max_isk_per_ehp is not None and ship.ship_type != "Blockade Runner":
            filtered = []
            for c in valid_contracts:
                contract = c['contract']
                # Calculate isk per m3 per ehp
                if ship.ehp and ship.ehp > 0 and contract.m3 > 0:
                    isk_per_m3 = contract.reward / contract.m3
                    isk_per_m3_per_ehp = isk_per_m3 / ship.ehp
                    if isk_per_m3_per_ehp <= self.config.max_isk_per_ehp:
                        filtered.append(c)
                else:
                    filtered.append(c)
            valid_contracts = filtered

        if not valid_contracts:
            return []

        # Sort contracts by profitability (descending), then by issuer (ascending)
        valid_contracts.sort(key=lambda c: (
            -c['contract'].reward,
            c['contract'].issuer
        ))

        # Group by route to minimize travel
        # Strategy: greedily add contracts while respecting cargo capacity
        cargo_capacity = ship.cargo_size
        trips = []
        used_contracts = set()

        # Build routes that cluster deliveries
        remaining = list(valid_contracts)

        while remaining:
            # Start a new trip from base
            trip_stops = []
            trip_contracts = []
            current_cargo = 0
            current_loc = base
            visited_locations = set()

            # Greedy route building
            added_in_this_trip = True
            while added_in_this_trip and remaining:
                added_in_this_trip = False

                for c in list(remaining):
                    contract = c['contract']
                    contract_id = contract.id

                    if contract_id in used_contracts:
                        continue

                    # Check cargo capacity
                    if current_cargo + contract.m3 > cargo_capacity:
                        continue

                    # Get locations
                    start_station = self.sde.find_station_by_name(contract.start)
                    end_station = self.sde.find_station_by_name(contract.end)

                    if not start_station or not end_station:
                        continue

                    # Check if we can reasonably reach the start location
                    jumps_to_start = self.count_jumps(current_loc, contract.start)
                    if jumps_to_start is None:
                        continue

                    # Add this contract
                    # We need to go pickup (LOAD at start), then deliver (UNLOAD at end)
                    trip_stops.append({
                        'type': 'LOAD',
                        'location': contract.start,
                        'contract': contract
                    })
                    trip_stops.append({
                        'type': 'UNLOAD',
                        'location': contract.end,
                        'contract': contract
                    })

                    current_cargo += contract.m3
                    current_loc = contract.end
                    used_contracts.add(contract_id)
                    remaining.remove(c)
                    trip_contracts.append(contract)
                    added_in_this_trip = True

            if trip_stops:
                # Remove consecutive duplicate locations
                cleaned_stops = []
                prev_loc = base
                for stop in trip_stops:
                    if stop['location'] != prev_loc or stop['type'] == 'LOAD':
                        cleaned_stops.append(stop)
                        prev_loc = stop['location']

                trip = {
                    'base': base,
                    'stops': cleaned_stops,
                    'contracts': trip_contracts
                }
                trips.append(trip)

        # If we have max_time constraint, remove trips that exceed time
        if max_time is not None and trips:
            dock_time = self.config.times.get('dock', 10.0)
            gate_time = self.config.times.get('gate', 30.0)
            move_cargo_time = self.config.times.get('move_cargo', 60.0)

            # Calculate time per trip and filter
            valid_trips = []
            for trip in trips:
                trip_time = self._calculate_trips_time([trip], ship)
                if trip_time is not None and trip_time <= max_time:
                    valid_trips.append(trip)

            if valid_trips:
                # Try to add more contracts within remaining time
                # For now, use filtered trips
                trips = valid_trips

        return trips


def parse_config(config_path: str) -> Config:
    """Parse config YAML file."""
    data = parse_yaml_file(config_path)

    ships = {}
    ships_data = data.get('ships', {})
    for name, ship_data in ships_data.items():
        ships[name] = Ship(
            name=name,
            ship_type=ship_data['type'],
            align=ship_data['align'],
            top_speed=ship_data['top_speed'],
            warp_speed=ship_data['warp_speed'],
            cargo_size=ship_data['cargo_size'],
            ehp=ship_data.get('ehp')  # Optional EHP field
        )

    times = data.get('times', {})
    min_isk_per_jump = data.get('min_isk_per_jump')
    max_isk_per_ehp = data.get('max_isk_per_ehp')
    return Config(
        ships=ships,
        times=times,
        min_isk_per_jump=min_isk_per_jump,
        max_isk_per_ehp=max_isk_per_ehp
    )


def parse_manifest(manifest_path: str) -> Manifest:
    """Parse manifest YAML file."""
    data = parse_yaml_file(manifest_path)

    start_cargo = data.get('start_cargo')
    waypoints = []
    for wp_data in data.get('waypoints', []):
        waypoints.append(Waypoint(
            name=wp_data['name'],
            cargo=wp_data.get('cargo')
        ))

    return Manifest(start_cargo=start_cargo, waypoints=waypoints)


def parse_contracts(filepath: str) -> list[Contract]:
    """Parse contracts from JSONL file."""
    contracts = []
    with open(filepath, 'r') as f:
        for idx, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            contract = Contract(
                id=idx,
                start=data.get('start', ''),
                end=data.get('end', ''),
                collateral=data.get('collateral', 0),
                m3=data.get('m3', 0),
                actual_value=data.get('actual_value', 0),
                reward=data.get('reward', 0),
                issuer=data.get('issuer', '')
            )
            contracts.append(contract)
    return contracts


def format_operation(op: dict) -> str:
    """Format a single operation for output."""
    op_type = op['type']

    if op_type == 'START':
        return f"START: {op['location']}"
    elif op_type == 'LOAD':
        return f"LOAD: {op['amount']:,.2f} m3"
    elif op_type == 'UNLOAD':
        return f"UNLOAD: {op['amount']:,.2f} m3"
    elif op_type == 'UNDOCK':
        return "UNDOCK"
    elif op_type == 'DOCK':
        return f"DOCK: {op['location']}"
    elif op_type == 'GO':
        return f"GO: {op['route']}"
    else:
        return str(op)


def format_time(total_seconds: float) -> str:
    """Format total seconds as HH:MM."""
    minutes = int(total_seconds // 60)
    hours = minutes // 60
    minutes = minutes % 60
    return f"{hours:02d}:{minutes:02d}"


def format_isk(value: float) -> str:
    """Format ISK value in millions with bankers rounding."""
    # Bankers rounding: round to nearest, ties to even
    # Python's round() does bankers rounding for .5 cases
    value_m = value / 1_000_000
    return f"{value_m:,.2f}M"


def format_number(value: float) -> str:
    """Format number with thousands separator and 2 decimal places."""
    return f"{value:,.2f}"


def format_contracts_output(base: str, trips: list, ship: Ship,
                            planner: ContractsPlanner, sde: SDE) -> list:
    """Format output for contracts command."""
    output_lines = []

    if not ship:
        output_lines.append("No Good Contracts")
        return output_lines

    # SHIP line
    output_lines.append(f"SHIP: {ship.name}")

    dock_time = planner.config.times.get('dock', 10.0)
    gate_time = planner.config.times.get('gate', 30.0)
    move_cargo_time = planner.config.times.get('move_cargo', 60.0)

    total_profit = 0.0
    total_m3 = 0.0
    total_jumps = 0
    total_time = 0.0

    # Track contract jumps while in possession
    contract_jumps = {}

    current_loc = base

    for trip_idx, trip in enumerate(trips):
        if trip_idx > 0:
            output_lines.append("")

        trip_start_loc = trip['base']

        # Process each stop in the trip
        for stop in trip['stops']:
            loc = stop['location']

            # Move to location (if not already there)
            if loc != current_loc:
                jumps = planner.count_jumps(current_loc, loc)
                if jumps is not None:
                    total_jumps += jumps
                    total_time += jumps * gate_time

                current_loc = loc

            # Dock
            total_time += dock_time

            if stop['type'] == 'LOAD':
                contract = stop['contract']
                # LOAD line
                output_lines.append(
                    f"LOAD {contract.issuer} (id={contract.id}): "
                    f"{format_isk(contract.reward)} ISK | "
                    f"{format_number(contract.m3)} m3"
                )
                total_profit += contract.reward
                total_m3 += contract.m3
                # Track jumps for this contract
                contract_jumps[contract.id] = 0

                # Load operation time
                total_time += move_cargo_time

            elif stop['type'] == 'UNLOAD':
                contract = stop['contract']
                # UNLOAD line with jumps taken while holding contract
                jumps_held = contract_jumps.pop(contract.id, 0)
                output_lines.append(
                    f"UNLOAD {contract.issuer} (id={contract.id}): "
                    f"{jumps_held} Jumps | "
                    f"{format_number(contract.m3)} m3"
                )
                # Unload operation time
                total_time += move_cargo_time

            # Undock
            total_time += dock_time

        # Return to base
        return_jumps = planner.count_jumps(current_loc, trip['base'])
        if return_jumps is not None:
            total_jumps += return_jumps
            total_time += return_jumps * gate_time
            # Add jumps to all contracts still being held
            for cid in contract_jumps:
                contract_jumps[cid] += return_jumps
            current_loc = trip['base']

    # Calculate summary statistics
    num_contracts = len(trips[0]['contracts']) if trips else 0
    if len(trips) > 1:
        for trip in trips[1:]:
            num_contracts += len(trip['contracts'])

    isk_per_m3 = total_profit / total_m3 if total_m3 > 0 else 0
    isk_per_jump = total_profit / total_jumps if total_jumps > 0 else total_profit

    # Calculate ISK per hour
    isk_per_hour = total_profit
    if total_time > 0:
        # Convert time to hours
        hours = total_time / 3600
        isk_per_hour = total_profit / hours

    # Output summary
    output_lines.append("")
    output_lines.append(f"NUM CONTRACTS: {num_contracts}")
    output_lines.append(f"PROFIT: {format_isk(total_profit)}")
    output_lines.append(f"ISK/M3: {format_isk(isk_per_m3)}")
    output_lines.append(f"ISK/Jump: {format_isk(isk_per_jump)}")
    output_lines.append(f"ISK/Hour: {format_isk(isk_per_hour)}")

    return output_lines


def main():
    parser = argparse.ArgumentParser(description='Cargo hauling operations')
    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # Plan command
    plan_parser = subparsers.add_parser('plan', help='Plan hauling with manifest')
    plan_parser.add_argument('start', help='Start location')
    plan_parser.add_argument('end', help='End location')
    plan_parser.add_argument('--manifest', required=True, help='Path to manifest YAML')
    plan_parser.add_argument('--config', required=True, help='Path to config YAML')
    plan_parser.add_argument('--ship', required=True, help='Ship name from config')
    plan_parser.add_argument('--sde', required=True, help='Path to SDE directory')

    # Contracts command
    contracts_parser = subparsers.add_parser('contracts', help='Plan hauling with contracts')
    contracts_parser.add_argument('start', help='Start system (base)')
    contracts_parser.add_argument('contracts_file', help='Path to contracts JSONL file')
    contracts_parser.add_argument('--config', required=True, help='Path to config YAML')
    contracts_parser.add_argument('--sde', required=True, help='Path to SDE directory')
    contracts_parser.add_argument('--target-iph', type=float, help='Target ISK per hour')
    contracts_parser.add_argument('--max-time', type=int, help='Maximum hauling time in minutes')

    args = parser.parse_args()

    if args.command == 'plan':
        # Load config and manifest
        config = parse_config(args.config)
        manifest = parse_manifest(args.manifest)

        # Load SDE
        sde = SDE(args.sde)
        sde.load()

        # Plan hauling operations
        planner = HaulingPlanner(sde, config)

        # Calculate total cargo
        total_moved = 0.0
        if manifest.start_cargo:
            total_moved += manifest.start_cargo
        for wp in manifest.waypoints:
            if wp.cargo:
                total_moved += wp.cargo

        # Plan trips
        trips = planner.plan_manifest(args.start, args.end, manifest, args.ship)

        # Calculate total time
        total_time = sum(trip.total_time for trip in trips)

        # Output results
        output_lines = []

        if not trips:
            # No cargo to move
            output_lines.append(f"START: {args.start}")
            output_lines.append(f"DONE: 00:00")
        else:
            # First trip
            trip = trips[0]
            for op in trip.operations:
                output_lines.append(format_operation(op))

            # Additional trips
            for i, trip in enumerate(trips[1:], 2):
                output_lines.append(f"[--- TRIP {i} ---]")
                for op in trip.operations:
                    output_lines.append(format_operation(op))

            output_lines.append(f"DONE: {format_time(total_time)}")
            if total_moved > 0:
                output_lines.append(f"MOVED: {total_moved:,.2f} m3")

        # Print output
        for line in output_lines:
            print(line)

    elif args.command == 'contracts':
        # Load config and contracts
        config = parse_config(args.config)
        contracts = parse_contracts(args.contracts_file)

        # Load SDE
        sde = SDE(args.sde)
        sde.load()

        # Create planner
        planner = ContractsPlanner(sde, config)

        # Convert max_time from minutes to seconds
        max_time_seconds = None
        if args.max_time is not None:
            max_time_seconds = args.max_time * 60

        # Plan contracts - automatically select best ship
        trips, ship = planner.plan_contracts(args.start, contracts, '', max_time_seconds)

        # Format and print output
        output_lines = format_contracts_output(args.start, trips, ship, planner, sde)
        for line in output_lines:
            print(line)

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
