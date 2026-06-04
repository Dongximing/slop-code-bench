#!/usr/bin/env python3
"""
Hauling route planner for New Eden (EVE Online) with manifest support.
"""

import argparse
import csv
import bz2
import math
import sys
from collections import defaultdict
from typing import Dict, List, Tuple, Set, Optional, Any

try:
    import yaml
except ImportError:
    yaml = None

AU_IN_M = 149597870700.0  # 1 AU in meters


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan cargo hauling routes with manifest support",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("start", help="Starting location")
    parser.add_argument("end", help="Destination")
    parser.add_argument("--manifest", required=True, help="Path to manifest YAML file")
    parser.add_argument("--config", required=True, help="Path to config YAML file")
    parser.add_argument("--ship", required=True, help="Ship name from config")
    parser.add_argument("--sde", required=True, help="Path to SDE directory")
    return parser.parse_args()


def load_yaml_file(path: str) -> Dict:
    """Load a YAML file and return its contents."""
    if yaml is None:
        raise ImportError("PyYAML is required. Install with: pip install PyYAML")
    with open(path, 'r') as f:
        return yaml.safe_load(f)


def load_sde_data(sde_path: str) -> Tuple[Dict, Dict, Dict]:
    """Load all SDE CSV.bz2 files into data structures.

    Returns:
        systems: dict name -> {id, security, x, y, z}
        jumps: list of (system_id1, system_id2) tuples
        stations: dict name -> {id, system_id}
    """
    systems = {}
    with bz2.open(f"{sde_path}/mapSolarSystems.csv.bz2", "rt") as f:
        reader = csv.DictReader(f)
        for row in reader:
            systems[row["solarSystemName"]] = {
                "id": int(row["solarSystemID"]),
                "security": float(row["security"]),
                "x": float(row["x"]),
                "y": float(row["y"]),
                "z": float(row["z"])
            }

    jumps = []
    system_id_to_name = {s["id"]: name for name, s in systems.items()}
    with bz2.open(f"{sde_path}/mapSolarSystemJumps.csv.bz2", "rt") as f:
        reader = csv.DictReader(f)
        for row in reader:
            from_id = int(row["fromSolarSystemID"])
            to_id = int(row["toSolarSystemID"])
            jumps.append((from_id, to_id))

    stations = {}
    with bz2.open(f"{sde_path}/staStations.csv.bz2", "rt") as f:
        reader = csv.DictReader(f)
        for row in reader:
            station_name = row["stationName"]
            stations[station_name] = {
                "id": int(row["stationID"]),
                "system_id": int(row["solarSystemID"])
            }

    return systems, jumps, stations


def build_system_graph(jumps: List[Tuple]) -> Dict[int, Set[int]]:
    """Build adjacency list for system graph using system IDs."""
    graph = defaultdict(set)
    for from_id, to_id in jumps:
        graph[from_id].add(to_id)
        graph[to_id].add(from_id)
    return graph


def get_system_for_location(name: str, systems: Dict, stations: Dict) -> Tuple[str, int]:
    """Get the system name and ID for a location (station or system)."""
    if name in stations:
        station = stations[name]
        # Find system name
        for sys_name, sys_data in systems.items():
            if sys_data["id"] == station["system_id"]:
                return sys_name, station["system_id"]
    elif name in systems:
        return name, systems[name]["id"]
    raise ValueError(f"Location '{name}' not found")


def is_station_location(name: str, stations: Dict) -> bool:
    """Check if location is a station."""
    return name in stations


def calculate_system_distance(sys1: Dict, sys2: Dict) -> float:
    """Calculate 3D distance between two systems in meters."""
    dx = sys1["x"] - sys2["x"]
    dy = sys1["y"] - sys2["y"]
    dz = sys1["z"] - sys2["z"]
    return math.sqrt(dx*dx + dy*dy + dz*dz)


def find_path_dijkstra(start_system_id: int, end_system_id: int,
                       systems: Dict, graph: Dict,
                       prohibit_low_sec: bool = False) -> List[int]:
    """Find shortest path using Dijkstra with optional low-sec prohibition."""
    import heapq

    pq = [(0, start_system_id, [start_system_id])]
    visited = set()

    while pq:
        dist, current, path_so_far = heapq.heappop(pq)

        if current in visited:
            continue
        visited.add(current)

        if current == end_system_id:
            return path_so_far

        for neighbor in graph.get(current, set()):
            if neighbor in visited:
                continue

            # Check security constraint
            if prohibit_low_sec:
                neighbor_name = None
                for name, data in systems.items():
                    if data["id"] == neighbor:
                        neighbor_name = name
                        break
                if neighbor_name and systems[neighbor_name]["security"] < 0.45:
                    continue

            sys1_name = None
            sys2_name = None
            for name, data in systems.items():
                if data["id"] == current:
                    sys1_name = name
                if data["id"] == neighbor:
                    sys2_name = name

            if sys1_name and sys2_name:
                new_dist = dist + calculate_system_distance(
                    systems[sys1_name], systems[sys2_name]
                )
                heapq.heappush(pq, (new_dist, neighbor, path_so_far + [neighbor]))

    raise ValueError("No path found")


def find_freighter_path(start_system_id: int, end_system_id: int,
                        systems: Dict, graph: Dict) -> List[int]:
    """Find path for freighter - try high-sec only first, then fall back to any path."""
    try:
        return find_path_dijkstra(start_system_id, end_system_id, systems, graph, True)
    except ValueError:
        # Fall back to any route if high-sec only fails
        return find_path_dijkstra(start_system_id, end_system_id, systems, graph, False)


class Ship:
    """Represents a ship configuration."""

    def __init__(self, ship_type: str, align: float, top_speed: float,
                 warp_speed: float, cargo_size: int):
        self.type = ship_type
        self.align = align
        self.top_speed = top_speed
        self.warp_speed = warp_speed
        self.cargo_size = cargo_size


class Trip:
    """Represents a single trip with cargo operations."""

    def __init__(self):
        self.waypoints: List[Dict] = []  # Each with 'type', 'name', 'cargo', 'action'
        # action: 'load', 'unload'
        self.cargo_in_hold = 0.0
        self.total_time = 0.0


class RouteSegment:
    """Represents a route segment between two waypoints."""

    def __init__(self, from_location: str, to_location: str, is_station_from: bool,
                 is_station_to: bool, path: List[str]):
        self.from_location = from_location
        self.to_location = to_location
        self.is_station_from = is_station_from
        self.is_station_to = is_station_to
        self.path = path  # List of system names


def calculate_route_time(segment: RouteSegment, ship: Ship, dock_time: float,
                         gate_time: float, systems: Dict) -> float:
    """Calculate total time for a route segment."""
    total_time = 0.0

    # Undock if starting at station
    if segment.is_station_from:
        total_time += dock_time

    # Align
    total_time += ship.align

    # Warp and gate traversal
    for i in range(len(segment.path) - 1):
        sys1 = systems[segment.path[i]]
        sys2 = systems[segment.path[i + 1]]
        dist = calculate_system_distance(sys1, sys2)

        # Warp time calculation
        warp_speed_ms = ship.warp_speed * AU_IN_M
        v_drop = min(ship.top_speed / 2, 100)
        k_a = ship.warp_speed
        k_d = min(ship.warp_speed / 3, 2.0)

        d_a = AU_IN_M
        d_d = warp_speed_ms / k_d
        d_min = d_a + d_d

        if dist < d_min:
            v_warp_ms = dist * k_a * k_d / (k_a + k_d)
        else:
            v_warp_ms = warp_speed_ms

        v_warp_au_s = v_warp_ms / AU_IN_M
        t_accel = (1 / k_a) * math.log(v_warp_au_s / k_a)
        t_decel = (1 / k_d) * math.log(v_warp_au_s / (v_drop / AU_IN_M))
        total_time += t_accel + t_decel

        if dist >= d_min:
            t_cruise = (dist - (d_a + d_d)) / v_warp_ms
            total_time += t_cruise

        # Gate time
        total_time += gate_time

    # Dock if ending at station
    if segment.is_station_to:
        total_time += dock_time

    return total_time


def plan_triangle(start_loc: str, end_loc: str, manifest: Dict, ship: Ship,
                  config: Dict, systems: Dict, stations: Dict, graph: Dict) -> List[Trip]:
    """Plan all necessary trips to fulfill the manifest."""

    dock_time = config["times"]["dock"]
    gate_time = config["times"]["gate"]
    move_cargo_time = config["times"]["move_cargo"]

    start_cargo = manifest.get("start_cargo")
    if start_cargo is None:
        start_cargo = 0.0

    waypoints = manifest.get("waypoints", [])

    # Get system for start and end
    start_system_name, start_system_id = get_system_for_location(start_loc, systems, stations)
    end_system_name, end_system_id = get_system_for_location(end_loc, systems, stations)
    start_is_station = is_station_location(start_loc, stations)
    end_is_station = is_station_location(end_loc, stations)

    # Build list of all cargo operations
    # Format: (location, cargo_delta, is_station)
    # cargo_delta > 0 means load, < 0 means unload
    operations: List[Tuple[str, float, bool]] = []

    if start_cargo > 0:
        operations.append((start_loc, start_cargo, start_is_station))

    for wp in waypoints:
        cargo = wp.get("cargo")
        if cargo is not None and cargo != 0:
            loc = wp["name"]
            is_station = is_station_location(loc, stations)
            operations.append((loc, cargo, is_station))

    # For simplicity, if no operations, just return a single trip
    if not operations and start_cargo == 0:
        trip = Trip()
        trip.waypoints = []
        return [trip]

    # Determine if freighter restrictions apply
    is_freighter = ship.type == "Freighter"

    # Build a plan: we need to visit all locations with cargo operations
    # and deliver to the end

    trips = []

    # Current implementation: simple greedy approach
    # 1. Start at start location with initial cargo
    # 2. Visit waypoints in order, loading/unloading
    # 3. End at destination

    # Calculate all system-to-system paths
    # First, get all unique systems involved
    all_systems = set([start_system_name, end_system_name])
    for op in operations:
        op_loc = op[0]
        sys_name, _ = get_system_for_location(op_loc, systems, stations)
        all_systems.add(sys_name)

    # For each trip, we load cargo and deliver
    remaining_to_deliver = start_cargo
    cargo_at_start = start_cargo

    # If we have waypoints, we need to process them
    # For simplicity, process sequentially
    cargo_in_hold = 0.0
    current_loc = start_loc
    current_is_station = start_is_station
    current_system = start_system_name

    # Build sequence of stops
    stops: List[Dict] = []

    # First stop: start with cargo if any
    if start_cargo > 0:
        stops.append({
            "type": "station" if start_is_station else "system",
            "name": start_loc,
            "system": start_system_name,
            "action": "load",
            "cargo": start_cargo
        })
        cargo_in_hold = start_cargo

    # Process waypoints
    for wp in waypoints:
        cargo = wp.get("cargo")
        if cargo is not None and cargo != 0:
            loc = wp["name"]
            is_station = is_station_location(loc, stations)
            sys_name, _ = get_system_for_location(loc, systems, stations)

            if cargo > 0:
                # Loading at waypoint
                stops.append({
                    "type": "station" if is_station else "system",
                    "name": loc,
                    "system": sys_name,
                    "action": "load",
                    "cargo": cargo
                })
                cargo_in_hold += cargo
            else:
                # Unloading
                cargo_abs = abs(cargo)
                stops.append({
                    "type": "station" if is_station else "system",
                    "name": loc,
                    "system": sys_name,
                    "action": "unload",
                    "cargo": cargo_abs
                })
                cargo_in_hold -= cargo_abs

    # Final stop: unload at end
    if cargo_in_hold > 0:
        stops.append({
            "type": "station" if end_is_station else "system",
            "name": end_loc,
            "system": end_system_name,
            "action": "unload",
            "cargo": cargo_in_hold
        })

    if not stops:
        # No cargo operations
        trip = Trip()
        trips.append(trip)
        return trips

    # Now we need to figure out trips based on cargo capacity
    # A trip consists of: start -> load(s) -> end (unload)

    # Calculate total cargo to move
    total_cargo_to_move = sum(s["cargo"] for s in stops if s["action"] == "load")

    if total_cargo_to_move == 0:
        trip = Trip()
        trips.append(trip)
        return trips

    # Number of full trips needed
    max_capacity = ship.cargo_size
    trips_needed = math.ceil(total_cargo_to_move / max_capacity)

    # Distribute cargo across trips
    # For now, simple approach: each trip carries up to max_capacity
    # Load sources: start (if start_cargo > 0) and waypoints with positive cargo
    # Unload targets: waypoints with negative cargo and end

    load_sources = [(s["name"], s["system"], s["cargo"]) for s in stops if s["action"] == "load"]
    unload_targets = [(s["name"], s["system"], s["cargo"]) for s in stops if s["action"] == "unload"]

    # Build trips
    for trip_num in range(trips_needed):
        trip = Trip()

        # Determine what cargo this trip carries
        trip_cargo = 0.0
        trip_operations = []

        # Allocate cargo from sources
        for src_name, src_system, src_cargo in load_sources:
            remaining = src_cargo
            # This is simplified - in real implementation would track per-source
            # For now, just fill the trip
            if trip_cargo < max_capacity:
                can_take = min(src_cargo, max_capacity - trip_cargo)
                if can_take > 0:
                    trip_operations.append({
                        "location": src_name,
                        "system": src_system,
                        "type": "station" if is_station_location(src_name, stations) else "system",
                        "action": "load",
                        "cargo": can_take,
                        "source_remaining": src_cargo - can_take  # Simplified
                    })
                    trip_cargo += can_take

        # Build trip path
        # Simplified: go from start, visit all load locations, then all unload locations, end
        trip.waypoints = trip_operations
        trips.append(trip)

    return trips


class ManifestRoutePlanner:
    """Main class for planning manifest-based routes."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.systems, self.jumps, self.stations = load_sde_data(args.sde)
        self.graph = build_system_graph(self.jumps)

        # Load config
        self.config = load_yaml_file(args.config)
        self.manifest = load_yaml_file(args.manifest)

        # Get ship config
        if args.ship not in self.config.get("ships", {}):
            raise ValueError(f"Ship '{args.ship}' not found in config")
        ship_data = self.config["ships"][args.ship]

        self.ship = Ship(
            ship_type=ship_data["type"],
            align=ship_data["align"],
            top_speed=ship_data["top_speed"],
            warp_speed=ship_data["warp_speed"],
            cargo_size=ship_data["cargo_size"]
        )

        self.dock_time = self.config["times"]["dock"]
        self.gate_time = self.config["times"]["gate"]
        self.move_cargo_time = self.config["times"]["move_cargo"]

    def get_system_name(self, location: str) -> str:
        """Get the system name for a location."""
        if location in self.stations:
            station = self.stations[location]
            for sys_name, sys_data in self.systems.items():
                if sys_data["id"] == station["system_id"]:
                    return sys_name
        elif location in self.systems:
            return location
        raise ValueError(f"Location '{location}' not found")

    def get_path_with_security(self, from_sys: str, to_sys: str,
                               is_freighter: bool = False) -> Tuple[List[str], bool]:
        """Get path between two systems, optionally avoiding low-sec for freighters."""
        from_id = self.systems[from_sys]["id"]
        to_id = self.systems[to_sys]["id"]

        if is_freighter:
            # Try high-sec only
            try:
                path = find_freighter_path(from_id, to_id, self.systems, self.graph)
                went_low_sec = False
            except ValueError:
                # Fallback to any route
                path = find_path_dijkstra(from_id, to_id, self.systems, self.graph, False)
                went_low_sec = True
        else:
            path = find_path_dijkstra(from_id, to_id, self.systems, self.graph, False)
            went_low_sec = any(self.systems[self.systems_name_from_id(sid)]["security"] < 0.45
                               for sid in path[1:])

        return [self.systems_name_from_id(sid) for sid in path], went_low_sec

    def systems_name_from_id(self, sys_id: int) -> str:
        """Get system name from ID."""
        for name, data in self.systems.items():
            if data["id"] == sys_id:
                return name
        return ""

    def build_route_segment(self, from_loc: str, to_loc: str) -> RouteSegment:
        """Build a route segment between two locations."""
        from_sys = self.get_system_name(from_loc)
        to_sys = self.get_system_name(to_loc)

        from_is_station = is_station_location(from_loc, self.stations)
        to_is_station = is_station_location(to_loc, self.stations)

        path, _ = self.get_path_with_security(from_sys, to_sys,
                                               is_freighter=self.ship.type == "Freighter")

        return RouteSegment(from_loc, to_loc, from_is_station, to_is_station, path)

    def calculate_segment_time(self, segment: RouteSegment) -> float:
        """Calculate time for a route segment."""
        return calculate_route_time(segment, self.ship, self.dock_time,
                                    self.gate_time, self.systems)

    def plan_all_trips(self) -> List[List[Dict]]:
        """Plan all necessary trips. Returns list of trips, each being list of operations."""
        start_loc = self.args.start
        end_loc = self.args.end
        manifest = self.manifest

        start_cargo = manifest.get("start_cargo")
        if start_cargo is None:
            start_cargo = 0.0

        waypoints = manifest.get("waypoints", [])

        # Build list of all cargo operations
        operations: List[Dict] = []

        if start_cargo > 0:
            operations.append({
                "location": start_loc,
                "type": "station" if is_station_location(start_loc, self.stations) else "system",
                "action": "load",
                "cargo": start_cargo
            })

        for wp in waypoints:
            cargo = wp.get("cargo")
            if cargo is not None and cargo != 0:
                loc = wp["name"]
                action = "load" if cargo > 0 else "unload"
                operations.append({
                    "location": loc,
                    "type": "station" if is_station_location(loc, self.stations) else "system",
                    "action": action,
                    "cargo": abs(cargo) if cargo < 0 else cargo
                })

        # Add final unload at end if there's cargo
        total_load = sum(op["cargo"] for op in operations if op["action"] == "load")
        if total_load > 0:
            operations.append({
                "location": end_loc,
                "type": "station" if is_station_location(end_loc, self.stations) else "system",
                "action": "unload",
                "cargo": total_load
            })

        if not operations:
            return [[]]

        # Calculate total cargo and trips needed
        max_capacity = self.ship.cargo_size

        # Separate load and unload operations
        load_ops = [op for op in operations if op["action"] == "load"]
        unload_ops = [op for op in operations if op["action"] == "unload" and op["location"] != end_loc]

        total_cargo = sum(op["cargo"] for op in load_ops)
        trips_needed = max(1, math.ceil(total_cargo / max_capacity))

        trips = []
        remaining_load = total_cargo

        for trip_idx in range(trips_needed):
            trip_ops = []
            trip_load = 0.0

            # For this trip, determine which loads we take
            trip_loads = []
            for op in load_ops:
                if trip_load < max_capacity:
                    # Simplified: assume all cargo sources are at start
                    # In real implementation, would need to visit each source
                    can_take = min(op["cargo"], max_capacity - trip_load)
                    if can_take > 0:
                        trip_loads.append({
                            "location": op["location"],
                            "cargo": can_take,
                            "type": op["type"]
                        })
                        trip_load += can_take

            # Build trip operations
            # First, any initial load at start
            if trip_loads:
                first_load = trip_loads[0]
                trip_ops.append({
                    "location": first_load["location"],
                    "cargo": first_load["cargo"],
                    "type": first_load["type"],
                    "action": "load"
                })

            # For simplicity, assume single destination
            # In real implementation, would need to handle multiple waypoints
            trip_ops.append({
                "location": end_loc,
                "cargo": trip_load,
                "type": "station" if is_station_location(end_loc, self.stations) else "system",
                "action": "unload"
            })

            trips.append(trip_ops)

        return trips if trips else [[]]

    def _format_route_segment(self, sys_list):
        """Format a list of system names into a route string with security levels."""
        parts = []
        for s in sys_list:
            sec = self.systems[s]["security"]
            parts.append(f"{s} ({sec:.1f})")
        return " -> ".join(parts)

    def generate_output(self) -> str:
        """Generate the formatted output for manifest mode."""
        start_loc = self.args.start
        end_loc = self.args.end
        manifest = self.manifest

        start_cargo = manifest.get("start_cargo")
        if start_cargo is None:
            start_cargo = 0.0

        waypoints = manifest.get("waypoints", [])

        # Get systems
        start_sys = self.get_system_name(start_loc)
        end_sys = self.get_system_name(end_loc)

        # Build list of all cargo operations in order: start -> waypoints -> end
        # Format: (location, cargo_delta, is_station)
        # cargo_delta > 0 = load, < 0 = unload, 0 = no operation
        operations = []

        if start_cargo > 0:
            operations.append((start_loc, start_cargo, is_station_location(start_loc, self.stations)))

        for wp in waypoints:
            cargo = wp.get("cargo")
            if cargo is not None and cargo != 0:
                loc = wp["name"]
                operations.append((loc, cargo, is_station_location(loc, self.stations)))

        # Get the set of all locations that have cargo operations
        op_locations = [loc for loc, _, _ in operations]

        if not operations:
            # No cargo operations - just show simple route
            output_lines = [f"START: {start_loc}"]
            path, _ = self.get_path_with_security(start_sys, end_sys,
                                                   is_freighter=self.ship.type == "Freighter")
            output_lines.append(f"GO: {self._format_route_segment(path)}")
            output_lines.append(f"DONE: 00:00")
            return "\n".join(output_lines)

        max_capacity = self.ship.cargo_size
        total_load = sum(c for _, c, _ in operations if c > 0)
        trips_needed = math.ceil(total_load / max_capacity)

        output_lines = []

        # We need to plan trips that:
        # 1. Start at start location
        # 2. Visit waypoints in some order that respects dependencies
        # 3. End at destination

        # For this implementation, we'll use a simplified approach:
        # Each trip: start -> (collect from remaining source waypoints) -> end

        # Remaining cargo at each location (only loads)
        remaining_cargo = {}
        for loc, cargo, _ in operations:
            if cargo > 0:
                remaining_cargo[loc] = cargo

        # Get paths between all relevant locations
        # First, get all unique systems
        all_systems = set([start_sys, end_sys])
        for loc, _, _ in operations:
            all_systems.add(self.get_system_name(loc))

        # For complex scenarios, we'd need TSP, but for the example,
        # we can simplify: assume waypoints are along the main route

        # For each trip
        for trip_idx in range(trips_needed):
            if trip_idx > 0:
                output_lines.append("")
                output_lines.append(f"[--- TRIP {trip_idx + 1} ---]")

            trip_start_time = 0.0
            trip_cargo = 0.0
            trip_operations = []
            current_loc = start_loc

            # Determine cargo to load on this trip
            # Fill up from available sources in order
            sources_to_visit = []
            for loc, cargo in list(remaining_cargo.items()):
                if trip_cargo < max_capacity:
                    can_take = min(cargo, max_capacity - trip_cargo)
                    if can_take > 0:
                        sources_to_visit.append((loc, can_take))
                        trip_cargo += can_take
                        remaining_cargo[loc] -= can_take
                        if remaining_cargo[loc] <= 0:
                            del remaining_cargo[loc]

            # START line
            output_lines.append(f"START: {start_loc}")

            # Now we need to build the route for this trip
            # Visit sources in an efficient order, then go to end
            # For simplicity, just visit in order found (start -> waypoints -> end)
            # A more sophisticated approach would reorder to minimize travel

            current_loc_sys = start_sys
            current_loc_is_station = is_station_location(current_loc, self.stations)

            # Load at source locations
            for src_loc, src_cargo in sources_to_visit:
                src_sys = self.get_system_name(src_loc)
                src_is_station = is_station_location(src_loc, self.stations)

                # Travel from current location to source location
                if src_sys != current_loc_sys:
                    # Need to travel
                    path, _ = self.get_path_with_security(current_loc_sys, src_sys,
                                                           is_freighter=self.ship.type == "Freighter")

                    # If starting from station, UNDOCK now (only once at start of trip)
                    if trip_idx == 0 and current_loc_is_station:
                        output_lines.append("UNDOCK")
                        trip_start_time += self.dock_time
                        current_loc_is_station = False

                    route_str = self._format_route_segment(path)
                    output_lines.append(f"GO: {route_str}")

                    # Calculate time
                    seg = RouteSegment(current_loc, src_loc, current_loc_is_station,
                                      src_is_station, path)
                    trip_start_time += self.calculate_segment_time(seg)

                    current_loc = src_loc
                    current_loc_sys = src_sys
                    current_loc_is_station = src_is_station

                # Load cargo
                output_lines.append(f"LOAD: {src_cargo:,.2f} m3")
                trip_start_time += self.move_cargo_time

                # Dock at station if we arrived at one
                if src_is_station:
                    output_lines.append(f"DOCK: {src_loc}")
                    trip_start_time += self.dock_time
                    current_loc_is_station = True
                else:
                    current_loc_is_station = False

            # Now go to final destination
            # If we're at a station and need to leave
            if current_loc_is_station:
                output_lines.append("UNDOCK")
                trip_start_time += self.dock_time

            # Path to end
            if current_loc_sys != end_sys:
                path, _ = self.get_path_with_security(current_loc_sys, end_sys,
                                                       is_freighter=self.ship.type == "Freighter")
                route_str = self._format_route_segment(path)
                output_lines.append(f"GO: {route_str}")

                seg = RouteSegment(current_loc, end_loc,
                                  current_loc_is_station, is_station_location(end_loc, self.stations),
                                  path)
                trip_start_time += self.calculate_segment_time(seg)

            # Dock at final destination
            end_is_station = is_station_location(end_loc, self.stations)
            if end_is_station:
                output_lines.append(f"DOCK: {end_loc}")
                trip_start_time += self.dock_time

            # Unload cargo
            output_lines.append(f"UNLOAD: {trip_cargo:,.2f} m3")
            trip_start_time += self.move_cargo_time

            total_time += trip_start_time

        # DONE line
        output_lines.append(f"DONE: {format_time(total_time)}")

        # MOVED line
        output_lines.append(f"MOVED: {total_load:,.2f} m3")

        return "\n".join(output_lines)


def format_time(seconds: float) -> str:
    """Format seconds as HH:MM, rounding up to nearest minute."""
    minutes = math.ceil(seconds / 60)
    hours = minutes // 60
    minutes = minutes % 60
    return f"{hours:02d}:{minutes:02d}"


def main():
    args = parse_args()

    planner = ManifestRoutePlanner(args)
    output = planner.generate_output()
    print(output)


if __name__ == "__main__":
    main()
