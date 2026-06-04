#!/usr/bin/env python3
"""
Cargo hauling operations with manifest files and ship information.
"""

import argparse
import csv
import sys
import re
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
        parsed_value = parse_yaml_value(value)
        result[key] = parsed_value
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


@dataclass
class Config:
    ships: dict
    times: dict


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
        import bz2
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
        import bz2
        with bz2.open(filepath, 'rt') as f:
            reader = csv.DictReader(f)
            for row in reader:
                from_id = int(row['fromSolarSystemID'])
                to_id = int(row['toSolarSystemID'])
                self.graph[from_id].append(to_id)

    def _load_stations(self):
        """Load stations from staStations.csv."""
        filepath = f"{self.sde_path}/staStations.csv.bz2"
        import bz2
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


def format_time(total_seconds: float) -> str:
    """Format total seconds as HH:MM."""
    minutes = int(total_seconds // 60)
    hours = minutes // 60
    minutes = minutes % 60
    return f"{hours:02d}:{minutes:02d}"


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
            cargo_size=ship_data['cargo_size']
        )

    times = data.get('times', {})
    return Config(ships=ships, times=times)


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


def main():
    parser = argparse.ArgumentParser(description='Cargo hauling operations')
    parser.add_argument('start', help='Start location')
    parser.add_argument('end', help='End location')
    parser.add_argument('--manifest', required=True, help='Path to manifest YAML')
    parser.add_argument('--config', required=True, help='Path to config YAML')
    parser.add_argument('--ship', required=True, help='Ship name from config')
    parser.add_argument('--sde', required=True, help='Path to SDE directory')

    args = parser.parse_args()

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


if __name__ == '__main__':
    main()
