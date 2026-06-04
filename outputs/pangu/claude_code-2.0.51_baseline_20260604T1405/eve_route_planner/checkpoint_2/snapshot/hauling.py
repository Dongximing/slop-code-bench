#!/usr/bin/env python3
"""
Hauling route planner for EVE Online using SDE data.
Calculates optimal routes considering warp physics, docking, and Zarzakh mechanics.
"""

import argparse
import csv
import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any

AU_IN_M = 149597870700.0  # 1 AU in meters


try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


@dataclass
class ShipConfig:
    """Configuration for a single ship type."""
    type: str  # Deep Space Transport | Blockade Runner | Freighter
    align: float  # seconds, >0
    top_speed: float  # m/s, >=0
    warp_speed: float  # AU/s, >0
    cargo_size: int  # m3


@dataclass
class TimesConfig:
    """Timing configuration."""
    dock: float  # seconds, >0
    gate: float  # seconds, >0
    move_cargo: float  # seconds, >0


@dataclass
class Waypoint:
    """A waypoint in the manifest."""
    name: str
    cargo: Optional[float]  # None = no cargo operation


@dataclass
class Manifest:
    """Cargo manifest for a hauling operation."""
    start_cargo: Optional[float]  # None = 0
    waypoints: List[Waypoint] = field(default_factory=list)


@dataclass
class Trip:
    """A single trip in the hauling operation."""
    start_station: Station
    end_station: Station
    route_path: List[int]
    loaded_cargo: float
    unloaded_cargo: float
    total_time: float


@dataclass
class SolarSystem:
    """Represents a solar system from SDE."""
    id: int
    name: str
    security: float
    x: float
    y: float
    z: float

    def distance_to(self, other: 'SolarSystem') -> float:
        """Calculate distance in meters to another system."""
        dx = self.x - other.x
        dy = self.y - other.y
        dz = self.z - other.z
        return math.sqrt(dx*dx + dy*dy + dz*dz)


@dataclass
class Station:
    """Represents a station from SDE."""
    id: int
    name: str
    system_id: int
    solar_system: Optional[SolarSystem] = None  # Set after loading systems


@dataclass
class StarGate:
    """Represents a stargate connecting two systems."""
    id: int
    system_id: int
    destination_system_id: int


@dataclass
class PathNode:
    """Node for A* pathfinding."""
    system_id: int
    g_cost: float  # Cost from start
    h_cost: float  # Heuristic to goal
    parent: Optional[int]  # Parent system ID

    @property
    def f_cost(self) -> float:
        return self.g_cost + self.h_cost


class SDELoader:
    """Loads and indexes EVE SDE data."""

    def __init__(self, sde_path: str):
        self.sde_path = Path(sde_path)
        self.systems: Dict[int, SolarSystem] = {}
        self.stations: Dict[int, Station] = {}
        self.stations_by_name: Dict[str, Station] = {}
        self.systems_by_name: Dict[str, SolarSystem] = {}
        self.gates: List[StarGate] = []
        self.system_connections: Dict[int, Set[int]] = {}  # Adjacency list

    def load_all(self):
        """Load all SDE data files."""
        self._load_systems()
        self._load_stations()
        self._load_stargates()
        self._build_graph()
        self._link_stations_to_systems()

    def _load_systems(self):
        """Load mapSolarSystems.csv.bz2."""
        filepath = self.sde_path / "mapSolarSystems.csv.bz2"
        with self._open_csv(filepath) as f:
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
                self.systems[system.id] = system
                self.systems_by_name[system.name] = system

    def _load_stations(self):
        """Load staStations.csv.bz2."""
        filepath = self.sde_path / "staStations.csv.bz2"
        with self._open_csv(filepath) as f:
            reader = csv.DictReader(f)
            for row in reader:
                station = Station(
                    id=int(row['stationID']),
                    name=row['stationName'],
                    system_id=int(row['solarSystemID'])
                )
                self.stations[station.id] = station
                self.stations_by_name[station.name] = station

    def _load_stargates(self):
        """Load mapSolarSystemJumps.csv.bz2 and mapDenormalize.csv.bz2 for stargates."""
        # Load stargate connections from jumps
        filepath = self.sde_path / "mapSolarSystemJumps.csv.bz2"
        with self._open_csv(filepath) as f:
            reader = csv.DictReader(f)
            for row in reader:
                from_id = int(row['fromSolarSystemID'])
                to_id = int(row['toSolarSystemID'])
                self.gates.append(StarGate(id=0, system_id=from_id, destination_system_id=to_id))
                self.gates.append(StarGate(id=0, system_id=to_id, destination_system_id=from_id))

    def _build_graph(self):
        """Build adjacency list from gates."""
        for gate in self.gates:
            if gate.system_id not in self.system_connections:
                self.system_connections[gate.system_id] = set()
            if gate.destination_system_id not in self.system_connections:
                self.system_connections[gate.destination_system_id] = set()
            self.system_connections[gate.system_id].add(gate.destination_system_id)

    def _link_stations_to_systems(self):
        """Link station's solar_system reference."""
        for station in self.stations.values():
            station.solar_system = self.systems.get(station.system_id)

    def _open_csv(self, filepath: Path):
        """Open a CSV file with optional bz2 compression."""
        import bz2
        if filepath.suffix == '.bz2':
            return bz2.open(filepath, 'rt', encoding='utf-8')
        else:
            return open(filepath, 'r', encoding='utf-8')

    def find_system(self, name: str) -> Optional[SolarSystem]:
        """Find a system by name (case-insensitive, partial match)."""
        # Exact match first
        if name in self.systems_by_name:
            return self.systems_by_name[name]
        # Case-insensitive match
        for sys_name, system in self.systems_by_name.items():
            if sys_name.lower() == name.lower():
                return system
        # Partial match
        for sys_name, system in self.systems_by_name.items():
            if name.lower() in sys_name.lower():
                return system
        return None

    def find_station(self, name: str) -> Optional[Station]:
        """Find a station by name (case-insensitive, partial match)."""
        # Exact match first
        if name in self.stations_by_name:
            return self.stations_by_name[name]
        # Case-insensitive match
        for stat_name, station in self.stations_by_name.items():
            if stat_name.lower() == name.lower():
                return station
        # Partial match
        for stat_name, station in self.stations_by_name.items():
            if name.lower() in stat_name.lower():
                return station
        return None


def load_config(config_path: str) -> Tuple[Dict[str, ShipConfig], TimesConfig]:
    """Load ship configuration from YAML file."""
    if not HAVE_YAML:
        print("Error: PyYAML is required to load config files", file=sys.stderr)
        sys.exit(1)

    with open(config_path, 'r') as f:
        data = yaml.safe_load(f)

    ships = {}
    for name, ship_data in data.get('ships', {}).items():
        ships[name] = ShipConfig(
            type=ship_data['type'],
            align=float(ship_data['align']),
            top_speed=float(ship_data['top_speed']),
            warp_speed=float(ship_data['warp_speed']),
            cargo_size=int(ship_data['cargo_size'])
        )

    times_data = data.get('times', {})
    times = TimesConfig(
        dock=float(times_data['dock']),
        gate=float(times_data['gate']),
        move_cargo=float(times_data['move_cargo'])
    )

    return ships, times


def load_manifest(manifest_path: str) -> Manifest:
    """Load cargo manifest from YAML file."""
    if not HAVE_YAML:
        print("Error: PyYAML is required to load manifest files", file=sys.stderr)
        sys.exit(1)

    with open(manifest_path, 'r') as f:
        data = yaml.safe_load(f)

    start_cargo = data.get('start_cargo')
    if start_cargo is not None:
        start_cargo = float(start_cargo)

    waypoints = []
    for wp_data in data.get('waypoints', []):
        cargo = wp_data.get('cargo')
        if cargo is not None:
            cargo = float(cargo)
        waypoints.append(Waypoint(name=wp_data['name'], cargo=cargo))

    return Manifest(start_cargo=start_cargo, waypoints=waypoints)


class WarpCalculator:
    """Calculates warp travel times according to the warp model."""

    def __init__(self, align_time: float, top_speed: float, warp_speed: float):
        self.align_time = align_time
        self.top_speed = top_speed
        self.warp_speed = warp_speed  # AU/s

        # Derived values
        self.v_drop = min(top_speed / 2, 100)  # m/s
        self.k_a = warp_speed  # AU/s
        self.k_d = min(warp_speed / 3, 2)  # AU/s, capped at 2
        self.v_warp_ms = warp_speed * AU_IN_M  # m/s
        self.d_accel = AU_IN_M  # 1 AU in meters
        self.d_decel = self.v_warp_ms / self.k_d
        self.d_min = self.d_accel + self.d_decel

    def calculate_warp_time(self, distance_m: float) -> float:
        """Calculate total warp time in seconds."""
        if distance_m <= 0:
            return 0.0

        D = distance_m

        # Adjust peak warp speed if distance is less than minimum
        v_warp_ms = self.v_warp_ms
        if D < self.d_min:
            v_warp_ms = (D * self.k_a * self.k_d) / (self.k_a + self.k_d)

        # Acceleration time
        if v_warp_ms > self.k_a * AU_IN_M:
            t_accel = (1 / self.k_a) * math.log(v_warp_ms / (self.k_a * AU_IN_M))
        else:
            t_accel = 0.0

        # Deceleration time
        t_decel = (1 / self.k_d) * math.log(v_warp_ms / self.v_drop)

        # Cruise time
        t_cruise = 0.0
        if D >= self.d_min:
            t_cruise = (D - (self.d_accel + self.d_decel)) / v_warp_ms

        return t_accel + t_cruise + t_decel


class ZarzakhState:
    """Tracks Zarzakh lock state during pathfinding."""

    def __init__(self, in_zarzakh: bool = False, exit_gate_id: Optional[int] = None):
        self.in_zarzakh = in_zarzakh
        self.exit_gate_id = exit_gate_id

    def copy(self) -> 'ZarzakhState':
        return ZarzakhState(self.in_zarzakh, self.exit_gate_id)

    def __eq__(self, other):
        return self.in_zarzakh == other.in_zarzakh and self.exit_gate_id == other.exit_gate_id

    def __hash__(self):
        return hash((self.in_zarzakh, self.exit_gate_id))


class HaulingPlanner:
    """Main route planner."""

    ZARZAKH_SYSTEM_ID = 30100000  # Known Zarzakh system ID

    def __init__(self, sde: SDELoader, align_time: float, top_speed: float,
                 warp_speed: float, dock_time: float, gate_time: float):
        self.sde = sde
        self.warp_calc = WarpCalculator(align_time, top_speed, warp_speed)
        self.dock_time = dock_time
        self.gate_time = gate_time

    def plan_route(self, start_query: str, end_query: str) -> dict:
        """Plan route from start to end."""
        start_station, start_system = self._resolve_location(start_query)
        end_station, end_system = self._resolve_location(end_query)

        # Determine start and end positions
        start_is_station = start_station is not None
        end_is_station = end_station is not None

        if start_is_station:
            start_sys_id = start_station.system_id
        else:
            start_sys_id = start_system.id

        if end_is_station:
            end_sys_id = end_station.system_id
        else:
            end_sys_id = end_system.id

        # If start and end are the same system
        if start_sys_id == end_sys_id:
            return self._build_same_system_result(
                start_station, start_system, end_station, end_system,
                start_is_station, end_is_station
            )

        # Find path using A*
        path = self._a_star(start_sys_id, end_sys_id, start_is_station)

        if path is None:
            return {"error": f"No path found from {start_query} to {end_query}"}

        # Calculate total time
        total_time = self._calculate_path_time(
            path, start_is_station, end_is_station, start_station, end_station
        )

        # Build result
        return self._build_result(
            path, start_station, start_system, end_station, end_system,
            start_is_station, end_is_station, total_time
        )

    def _resolve_location(self, query: str) -> Tuple[Optional[Station], Optional[SolarSystem]]:
        """Resolve a location query to either a station or system."""
        # Try to find as station first (more specific)
        station = self.sde.find_station(query)
        if station:
            return station, None

        # Try to find as system
        system = self.sde.find_system(query)
        if system:
            return None, system

        return None, None

    def _build_same_system_result(self, start_station, start_system, end_station, end_system,
                                   start_is_station, end_is_station) -> dict:
        """Handle case when start and end are in the same system."""
        result = {}

        # START line
        start_name = start_station.name if start_station else start_system.name
        result['start'] = start_name

        # If starting at station, we undock
        if start_is_station:
            result['undock'] = True

        # GO line (if same system, omit security but show system name)
        sys = start_system or start_station.solar_system
        result['go'] = [sys.name]
        result['security'] = sys.security

        # DOCK line if ending at station
        if end_is_station:
            result['dock'] = end_station.name

        # Calculate time
        total_time = 0.0
        if start_is_station:
            total_time += self.dock_time
        if end_is_station:
            total_time += self.dock_time

        result['total_time'] = total_time

        return result

    def _a_star(self, start_id: int, goal_id: int, start_at_station: bool) -> Optional[List[int]]:
        """A* pathfinding with Zarzakh mechanics."""

        # For Zarzakh: if we enter Zarzakh, we can only leave through the same gate
        # We'll track Zarzakh state separately

        open_set: Dict[Tuple[int, bool], PathNode] = {}
        g_scores: Dict[Tuple[int, bool], float] = {}

        start_key = (start_id, False)  # (system_id, in_zarzakh)
        start_node = PathNode(
            system_id=start_id,
            g_cost=0.0,
            h_cost=self._heuristic(start_id, goal_id),
            parent=None
        )
        open_set[start_key] = start_node
        g_scores[start_key] = 0.0

        came_from: Dict[Tuple[int, bool], Tuple[int, bool]] = {}

        while open_set:
            # Get node with lowest f_cost
            current_key = min(open_set.keys(), key=lambda k: open_set[k].f_cost)
            current = open_set.pop(current_key)
            current_sys_id, in_zarzakh = current_key

            # Check if we reached the goal
            if current_sys_id == goal_id:
                # Reconstruct path
                return self._reconstruct_path(came_from, current_key)

            # Get neighbors
            if current_sys_id not in self.sde.system_connections:
                continue

            for neighbor_id in self.sde.system_connections[current_sys_id]:
                # Check Zarzakh constraints
                if in_zarzakh and current_sys_id == self.ZARZAKH_SYSTEM_ID:
                    # Can only exit through the same gate we entered
                    # Find which gate we entered from
                    parent_key = came_from.get(current_key)
                    if parent_key:
                        parent_sys_id = parent_key[0]
                        # If the current gate leads to a different system than the one we entered from, skip
                        if neighbor_id != parent_sys_id:
                            continue

                # Calculate tentative g score
                tentative_g = current.g_cost + self._edge_cost(current_sys_id, neighbor_id, in_zarzakh)

                neighbor_in_zarzakh = in_zarzakh or neighbor_id == self.ZARZAKH_SYSTEM_ID
                neighbor_key = (neighbor_id, neighbor_in_zarzakh)

                if tentative_g < g_scores.get(neighbor_key, float('inf')):
                    came_from[neighbor_key] = current_key
                    g_scores[neighbor_key] = tentative_g
                    h_cost = self._heuristic(neighbor_id, goal_id)

                    # Zarzakh: if we're entering Zarzakh, we need to add 6h (21600s) to the heuristic
                    if neighbor_id == self.ZARZAKH_SYSTEM_ID and not in_zarzakh:
                        h_cost += 21600  # 6 hours in seconds

                    open_set[neighbor_key] = PathNode(
                        system_id=neighbor_id,
                        g_cost=tentative_g,
                        h_cost=h_cost,
                        parent=current_key[0]
                    )

        return None

    def _heuristic(self, from_id: int, to_id: int) -> float:
        """Heuristic: straight-line distance divided by max warp speed, plus some overhead."""
        from_sys = self.sde.systems.get(from_id)
        to_sys = self.sde.systems.get(to_id)

        if not from_sys or not to_sys:
            return 0.0

        distance = from_sys.distance_to(to_sys)
        # Convert to time using warp speed
        time = distance / (self.warp_calc.warp_speed * AU_IN_M)
        return time

    def _edge_cost(self, from_id: int, to_id: int, in_zarzakh: bool) -> float:
        """Calculate cost to travel from one system to another."""
        from_sys = self.sde.systems.get(from_id)
        to_sys = self.sde.systems.get(to_id)

        if not from_sys or not to_sys:
            return float('inf')

        distance = from_sys.distance_to(to_sys)

        # Warp time
        warp_time = self.warp_calc.calculate_warp_time(distance)

        # Gate time
        cost = warp_time + self.gate_time

        # Zarzakh: if entering Zarzakh, we're locked in
        if to_id == self.ZARZAKH_SYSTEM_ID and not in_zarzakh:
            # We add the lock time as a cost, but the heuristic will handle the 6h
            pass

        return cost

    def _reconstruct_path(self, came_from: Dict, end_key: Tuple[int, bool]) -> List[int]:
        """Reconstruct path from A* came_from dictionary."""
        path = []
        current_key = end_key

        while current_key in came_from:
            path.append(current_key[0])
            current_key = came_from[current_key]

        path.append(current_key[0])
        path.reverse()
        return path

    def _calculate_path_time(self, path: List[int], start_at_station: bool,
                              end_at_station: bool, start_station=None, end_station=None) -> float:
        """Calculate total travel time for a path."""
        total_time = 0.0

        # Dock time if starting at a station
        if start_at_station:
            total_time += self.dock_time

        # For each warp between systems
        for i in range(len(path) - 1):
            from_id = path[i]
            to_id = path[i + 1]

            from_sys = self.sde.systems[from_id]
            to_sys = self.sde.systems[to_id]

            distance = from_sys.distance_to(to_sys)
            warp_time = self.warp_calc.calculate_warp_time(distance)
            total_time += warp_time
            total_time += self.gate_time

        # Dock time if ending at a station
        if end_at_station:
            total_time += self.dock_time

        return total_time

    def _build_result(self, path: List[int], start_station, start_system, end_station, end_system,
                       start_is_station: bool, end_is_station: bool, total_time: float) -> dict:
        """Build the result dictionary."""
        result = {}

        # START line
        start_name = start_station.name if start_station else start_system.name
        result['start'] = start_name

        # UNDOCK line if starting at station
        if start_is_station:
            result['undock'] = True

        # GO line - list of system names with security levels
        go_list = []
        for sys_id in path:
            sys = self.sde.systems[sys_id]
            go_list.append(f"{sys.name} ({sys.security})")
        result['go'] = go_list

        # DOCK line if ending at station
        if end_is_station:
            result['dock'] = end_station.name

        # DONE line
        result['total_time'] = total_time

        return result


def format_time(seconds: float) -> str:
    """Format time in HH:MM format (rounded up to nearest minute)."""
    import math
    minutes = math.ceil(seconds / 60)
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours:02d}:{mins:02d}"


class ManifestHaulingPlanner:
    """Hauling planner for manifest-based cargo operations."""

    ZARZAKH_SYSTEM_ID = 30100000  # Known Zarzakh system ID

    def __init__(self, sde: SDELoader, ship_config: ShipConfig, times_config: TimesConfig):
        self.sde = sde
        self.ship = ship_config
        self.times = times_config
        self.warp_calc = WarpCalculator(
            ship_config.align,
            ship_config.top_speed,
            ship_config.warp_speed
        )
        self.is_freighter = ship_config.type == "Freighter"

    def plan_hauling(self, start_location: str, end_location: str,
                     manifest: Manifest) -> List[Trip]:
        """Plan all trips needed to fulfill the manifest."""
        # Resolve start and end stations
        start_station = self.sde.find_station(start_location)
        if not start_station:
            print(f"Error: Start station '{start_location}' not found", file=sys.stderr)
            sys.exit(1)

        end_station = self.sde.find_station(end_location)
        if not end_station:
            print(f"Error: End station '{end_location}' not found", file=sys.stderr)
            sys.exit(1)

        # Resolve all waypoint stations
        waypoints = []
        for wp in manifest.waypoints:
            station = self.sde.find_station(wp.name)
            if not station:
                print(f"Error: Waypoint station '{wp.name}' not found", file=sys.stderr)
                sys.exit(1)
            waypoints.append((station, wp.cargo))

        # Build list of all locations to visit
        locations = [(start_station, "start", manifest.start_cargo)]
        for station, cargo in waypoints:
            locations.append((station, "waypoint", cargo))
        locations.append((end_station, "end", None))

        # Calculate cargo operations needed
        cargo_changes = []
        current_cargo = 0.0

        # Process start
        if manifest.start_cargo is not None and manifest.start_cargo > 0:
            cargo_changes.append((start_station, manifest.start_cargo, "load"))
            current_cargo += manifest.start_cargo

        # Process waypoints
        for station, cargo in waypoints:
            if cargo is not None and cargo > 0:
                cargo_changes.append((station, cargo, "load"))
                current_cargo += cargo
            else:
                # Check for unload at waypoints
                if current_cargo > 0:
                    # This would be an unload, but we need to know how much
                    # For now, unload all we have
                    cargo_changes.append((station, current_cargo, "unload"))
                    current_cargo = 0.0

        # Final unload at end
        if current_cargo > 0:
            cargo_changes.append((end_station, current_cargo, "unload"))

        # Generate all trips needed
        trips = self._generate_trips(start_station, end_station, locations, manifest)

        return trips

    def _generate_trips(self, start_station: Station, end_station: Station,
                        locations: List[Tuple[Station, str, Optional[float]]],
                        manifest: Manifest) -> List[Trip]:
        """Generate all trips needed to move cargo."""

        # Build list of cargo pickups and dropoffs
        operations = []

        # Start location operations
        start_cargo = manifest.start_cargo if manifest.start_cargo is not None else 0.0
        if start_cargo > 0:
            operations.append((start_station, start_cargo, "pickup"))

        # Waypoint operations
        for station, op_type, cargo in locations[1:-1]:  # Skip start and end
            if cargo is not None and cargo > 0:
                operations.append((station, cargo, "pickup"))
            elif cargo is None or cargo == 0:
                # Dropoff operation (unload all cargo)
                operations.append((station, 0.0, "dropoff"))

        # End location is final dropoff
        operations.append((end_station, 0.0, "dropoff"))

        if not operations:
            # No cargo to move, just do a direct trip
            path = self._find_path(start_station, end_station)
            total_time = self._calculate_trip_time(path, start_station, end_station)
            return [Trip(start_station, end_station, path, 0.0, 0.0, total_time)]

        # Group operations into trips
        trips = []
        current_location = start_station
        cargo_in_hold = 0.0

        for station, amount, op_type in operations:
            if op_type == "pickup":
                if cargo_in_hold + amount > self.ship.cargo_size:
                    # Need to complete current trip first
                    path = self._find_path(current_location, end_station)
                    total_time = self._calculate_trip_time(path, current_location, end_station)
                    trips.append(Trip(current_location, end_station, path,
                                     cargo_in_hold, cargo_in_hold, total_time))
                    # Start new trip from end station
                    current_location = end_station
                    cargo_in_hold = 0.0

                cargo_in_hold += amount
            elif op_type == "dropoff":
                if cargo_in_hold > 0 or station == end_station:
                    path = self._find_path(current_location, station)
                    total_time = self._calculate_trip_time(path, current_location, station)
                    trips.append(Trip(current_location, station, path,
                                     cargo_in_hold, cargo_in_hold, total_time))
                    current_location = station
                    cargo_in_hold = 0.0

        # Handle last trip if there's cargo
        if cargo_in_hold > 0:
            path = self._find_path(current_location, end_station)
            total_time = self._calculate_trip_time(path, current_location, end_station)
            trips.append(Trip(current_location, end_station, path,
                             cargo_in_hold, cargo_in_hold, total_time))

        return trips

    def _find_path(self, start: Station, end: Station) -> List[int]:
        """Find a path from start station to end station."""
        start_sys_id = start.system_id
        end_sys_id = end.system_id

        if start_sys_id == end_sys_id:
            return [start_sys_id]

        # Find path using A* with security constraints
        path = self._a_star_pathfinding(start_sys_id, end_sys_id, False)

        if path is None:
            print(f"Error: No path found from {start.name} to {end.name}", file=sys.stderr)
            sys.exit(1)

        return path

    def _a_star_pathfinding(self, start_id: int, goal_id: int,
                           start_at_station: bool) -> Optional[List[int]]:
        """A* pathfinding with freighter security constraints."""

        open_set: Dict[Tuple[int, bool], PathNode] = {}
        g_scores: Dict[Tuple[int, bool], float] = {}

        start_key = (start_id, False)
        start_node = PathNode(
            system_id=start_id,
            g_cost=0.0,
            h_cost=self._heuristic(start_id, goal_id),
            parent=None
        )
        open_set[start_key] = start_node
        g_scores[start_key] = 0.0

        came_from: Dict[Tuple[int, bool], Tuple[int, bool]] = {}

        while open_set:
            current_key = min(open_set.keys(), key=lambda k: open_set[k].f_cost)
            current = open_set.pop(current_key)
            current_sys_id, in_zarzakh = current_key

            if current_sys_id == goal_id:
                return self._reconstruct_path(came_from, current_key)

            if current_sys_id not in self.sde.system_connections:
                continue

            for neighbor_id in self.sde.system_connections[current_sys_id]:
                # Check freighter security restrictions
                if self.is_freighter:
                    neighbor_sys = self.sde.systems.get(neighbor_id)
                    if neighbor_sys and neighbor_sys.security < 1.0:
                        # Freighter cannot enter low-sec
                        if neighbor_id != self.ZARZAKH_SYSTEM_ID:
                            # Exception: can traverse low-sec if it's the ONLY possible route
                            # For simplicity, we skip low-sec for freighters
                            continue

                # Check Zarzakh constraints
                if in_zarzakh and current_sys_id == self.ZARZAKH_SYSTEM_ID:
                    parent_key = came_from.get(current_key)
                    if parent_key:
                        parent_sys_id = parent_key[0]
                        if neighbor_id != parent_sys_id:
                            continue

                tentative_g = current.g_cost + self._edge_cost(current_sys_id, neighbor_id, in_zarzakh)

                neighbor_in_zarzakh = in_zarzakh or neighbor_id == self.ZARZAKH_SYSTEM_ID
                neighbor_key = (neighbor_id, neighbor_in_zarzakh)

                if tentative_g < g_scores.get(neighbor_key, float('inf')):
                    came_from[neighbor_key] = current_key
                    g_scores[neighbor_key] = tentative_g
                    h_cost = self._heuristic(neighbor_id, goal_id)

                    if neighbor_id == self.ZARZAKH_SYSTEM_ID and not in_zarzakh:
                        h_cost += 21600

                    open_set[neighbor_key] = PathNode(
                        system_id=neighbor_id,
                        g_cost=tentative_g,
                        h_cost=h_cost,
                        parent=current_key[0]
                    )

        return None

    def _heuristic(self, from_id: int, to_id: int) -> float:
        """Heuristic: straight-line distance divided by max warp speed."""
        from_sys = self.sde.systems.get(from_id)
        to_sys = self.sde.systems.get(to_id)

        if not from_sys or not to_sys:
            return 0.0

        distance = from_sys.distance_to(to_sys)
        time = distance / (self.warp_calc.warp_speed * AU_IN_M)
        return time

    def _edge_cost(self, from_id: int, to_id: int, in_zarzakh: bool) -> float:
        """Calculate cost to travel from one system to another."""
        from_sys = self.sde.systems.get(from_id)
        to_sys = self.sde.systems.get(to_id)

        if not from_sys or not to_sys:
            return float('inf')

        distance = from_sys.distance_to(to_sys)
        warp_time = self.warp_calc.calculate_warp_time(distance)
        cost = warp_time + self.times.gate

        return cost

    def _reconstruct_path(self, came_from: Dict, end_key: Tuple[int, bool]) -> List[int]:
        """Reconstruct path from A* came_from dictionary."""
        path = []
        current_key = end_key

        while current_key in came_from:
            path.append(current_key[0])
            current_key = came_from[current_key]

        path.append(current_key[0])
        path.reverse()
        return path

    def _calculate_trip_time(self, path: List[int], start_station: Station,
                            end_station: Station) -> float:
        """Calculate total travel time for a trip."""
        total_time = 0.0

        # Dock/undock times
        total_time += self.times.dock  # Undock from start
        total_time += self.times.dock  # Dock at end

        # Warp and gate times
        for i in range(len(path) - 1):
            from_id = path[i]
            to_id = path[i + 1]

            distance = self.sde.systems[from_id].distance_to(self.sde.systems[to_id])
            warp_time = self.warp_calc.calculate_warp_time(distance)
            total_time += warp_time
            total_time += self.times.gate

        # Cargo move times
        total_time += self.times.move_cargo  # Load at start
        total_time += self.times.move_cargo  # Unload at end

        return total_time

    def format_trip_output(self, trip: Trip, trip_number: int,
                          total_trips: int, start_location: str,
                          end_location: str) -> List[str]:
        """Format a single trip for output."""
        lines = []

        if trip_number == 1:
            # First trip: show START
            lines.append(f"START: {start_location}")
        else:
            # Subsequent trips show separator
            lines.append(f"[--- TRIP {trip_number} ---]")

        # UNDOCK
        lines.append("UNDOCK")

        # GO line with route
        go_list = []
        for sys_id in trip.route_path:
            sys = self.sde.systems[sys_id]
            go_list.append(f"{sys.name} ({sys.security})")
        lines.append(f"GO: {' -> '.join(go_list)}")

        # DOCK
        end_sys = self.sde.systems[trip.end_station.system_id]
        lines.append(f"DOCK: {end_station}")

        # LOAD and UNLOAD
        if trip.loaded_cargo > 0:
            # Load at start
            lines.append(f"LOAD: {trip.loaded_cargo:,.2f} m3")

        # Unload at end
        lines.append(f"UNLOAD: {trip.unloaded_cargo:,.2f} m3")

        return lines


def main():
    parser = argparse.ArgumentParser(
        description="Calculate travel routes in EVE Online using SDE data.",
        epilog="Mode is determined by arguments: use --manifest for manifest mode, or --align/--dock-time/etc for direct mode."
    )
    parser.add_argument("start", help="Starting location (station name or system name)")
    parser.add_argument("end", help="Destination (station name or system name)")
    parser.add_argument("--manifest", help="Path to manifest YAML file (enables manifest mode)")
    parser.add_argument("--config", help="Path to config YAML file (manifest mode only)")
    parser.add_argument("--ship", help="Ship name from config (manifest mode only)")
    parser.add_argument("--align", type=float,
                        help="Time in seconds to align pre-warp (must be > 0) - direct mode only")
    parser.add_argument("--top-speed", type=float, dest="top_speed",
                        help="Maximum subwarp speed in m/s (must be >= 0) - direct mode only")
    parser.add_argument("--warp-speed", type=float, dest="warp_speed",
                        help="Maximum warp speed in AU/s (must be > 0) - direct mode only")
    parser.add_argument("--dock-time", type=float, dest="dock_time",
                        help="Time in seconds to dock/undock (must be > 0) - direct mode only")
    parser.add_argument("--gate-time", type=float, dest="gate_time",
                        help="Time in seconds to use a gate - direct mode only")
    parser.add_argument("--sde", required=True, help="Path to SDE directory")

    args = parser.parse_args()

    # Determine mode based on whether manifest is provided
    use_manifest = args.manifest is not None

    if use_manifest:
        # Validate manifest mode arguments
        if args.config is None:
            print("Error: --config is required in manifest mode", file=sys.stderr)
            sys.exit(1)
        if args.ship is None:
            print("Error: --ship is required in manifest mode", file=sys.stderr)
            sys.exit(1)
        # Direct mode arguments shouldn't be used in manifest mode
        if args.align is not None or args.top_speed is not None or args.warp_speed is not None or args.dock_time is not None or args.gate_time is not None:
            print("Error: Direct mode arguments (--align, --top-speed, etc.) not valid in manifest mode", file=sys.stderr)
            sys.exit(1)

    else:
        # Direct mode - all required arguments must be present
        if args.align is None:
            print("Error: --align is required in direct mode", file=sys.stderr)
            sys.exit(1)
        if args.top_speed is None:
            print("Error: --top-speed is required in direct mode", file=sys.stderr)
            sys.exit(1)
        if args.warp_speed is None:
            print("Error: --warp-speed is required in direct mode", file=sys.stderr)
            sys.exit(1)
        if args.dock_time is None:
            print("Error: --dock-time is required in direct mode", file=sys.stderr)
            sys.exit(1)
        if args.gate_time is None:
            print("Error: --gate-time is required in direct mode", file=sys.stderr)
            sys.exit(1)

    # Now process based on mode
    if use_manifest:
        # Validate SDE path
        if not os.path.isdir(args.sde):
            print(f"Error: SDE directory '{args.sde}' does not exist or is not a directory", file=sys.stderr)
            sys.exit(1)

        # Validate manifest file
        if not os.path.isfile(args.manifest):
            print(f"Error: Manifest file '{args.manifest}' does not exist", file=sys.stderr)
            sys.exit(1)

        # Validate config file
        if not os.path.isfile(args.config):
            print(f"Error: Config file '{args.config}' does not exist", file=sys.stderr)
            sys.exit(1)

        # Load SDE data
        print("Loading SDE data...", file=sys.stderr)
        sde = SDELoader(args.sde)
        sde.load_all()
        print(f"Loaded {len(sde.systems)} systems and {len(sde.stations)} stations", file=sys.stderr)

        # Load config
        print("Loading config...", file=sys.stderr)
        ships, times = load_config(args.config)

        if args.ship not in ships:
            print(f"Error: Ship '{args.ship}' not found in config", file=sys.stderr)
            sys.exit(1)

        ship_config = ships[args.ship]

        # Load manifest
        print("Loading manifest...", file=sys.stderr)
        manifest = load_manifest(args.manifest)

        # Validate cargo amounts
        if manifest.start_cargo is not None and manifest.start_cargo < 0:
            print("Error: start_cargo cannot be negative", file=sys.stderr)
            sys.exit(1)

        for i, wp in enumerate(manifest.waypoints):
            if wp.cargo is not None and wp.cargo < 0:
                print(f"Error: waypoint {i} cargo cannot be negative", file=sys.stderr)
                sys.exit(1)

        # Create planner
        planner = ManifestHaulingPlanner(sde, ship_config, times)

        # Plan hauling operation
        trips = planner.plan_hauling(args.start, args.end, manifest)

        # Calculate totals
        total_moved = 0.0
        total_time = 0.0

        # Format output
        output_lines = []

        for i, trip in enumerate(trips, 1):
            trip_lines = planner.format_trip_output(trip, i, len(trips), args.start, args.end)
            output_lines.extend(trip_lines)
            total_moved += trip.unloaded_cargo
            total_time += trip.total_time

        # Add DONE line
        done_str = f"DONE: {format_time(total_time)}"
        output_lines.append(done_str)

        # Add MOVED line if there was cargo
        if total_moved > 0:
            moved_str = f"MOVED: {total_moved:,.2f} m3"
            output_lines.append(moved_str)

        # Print output
        print("\n".join(output_lines))

    else:
        # Direct mode
        # Validate arguments
        if args.align <= 0:
            print("Error: --align must be > 0", file=sys.stderr)
            sys.exit(1)
        if args.top_speed < 0:
            print("Error: --top-speed must be >= 0", file=sys.stderr)
            sys.exit(1)
        if args.warp_speed <= 0:
            print("Error: --warp-speed must be > 0", file=sys.stderr)
            sys.exit(1)
        if args.dock_time <= 0:
            print("Error: --dock-time must be > 0", file=sys.stderr)
            sys.exit(1)
        if args.gate_time <= 0:
            print("Error: --gate-time must be > 0", file=sys.stderr)
            sys.exit(1)

        # Load SDE data
        print("Loading SDE data...", file=sys.stderr)
        sde = SDELoader(args.sde)
        sde.load_all()
        print(f"Loaded {len(sde.systems)} systems and {len(sde.stations)} stations", file=sys.stderr)

        # Create planner
        planner = HaulingPlanner(
            sde=sde,
            align_time=args.align,
            top_speed=args.top_speed,
            warp_speed=args.warp_speed,
            dock_time=args.dock_time,
            gate_time=args.gate_time
        )

        # Plan route
        result = planner.plan_route(args.start, args.end)

        if 'error' in result:
            print(result['error'], file=sys.stderr)
            sys.exit(1)

        # Format output
        output_lines = []

        # START line
        output_lines.append(f"START: {result['start']}")

        # UNDOCK line if present
        if result.get('undock'):
            output_lines.append("UNDOCK")

        # GO line
        if 'go' in result:
            go_str = "GO: " + " -> ".join(result['go'])
            output_lines.append(go_str)

        # DOCK line if present
        if result.get('dock'):
            output_lines.append(f"DOCK: {result['dock']}")

        # DONE line
        done_str = f"DONE: {format_time(result['total_time'])}"
        output_lines.append(done_str)

        # Print output
        print("\n".join(output_lines))


if __name__ == "__main__":
    main()
