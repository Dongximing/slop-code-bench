#!/usr/bin/env python3
"""
Hauling route planner for New Eden (EVE Online) using EVE SDE data.
"""

import argparse
import csv
import bz2
import math
import sys
from collections import defaultdict
from typing import Dict, List, Tuple, Set, Optional, Any

AU_IN_M = 149597870700.0  # 1 AU in meters


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan travel routes in New Eden",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("start", help="Starting location (station name or system name)")
    parser.add_argument("end", help="Destination (station name or system name)")
    parser.add_argument("--align", type=float, required=True,
                        help="Time in seconds to align pre-warp (must be > 0)")
    parser.add_argument("--top-speed", type=float, required=True, dest="top_speed",
                        help="Maximum subwarp speed in m/s (must be >= 0)")
    parser.add_argument("--warp-speed", type=float, required=True, dest="warp_speed",
                        help="Maximum warp speed in AU/s (must be > 0)")
    parser.add_argument("--dock-time", type=float, required=True, dest="dock_time",
                        help="Time in seconds to dock/undock (must be > 0)")
    parser.add_argument("--gate-time", type=float, required=True, dest="gate_time",
                        help="Time in seconds to use a gate (must be > 0)")
    parser.add_argument("--sde", required=True, help="Path to SDE directory")
    return parser.parse_args()


def validate_args(args: argparse.Namespace):
    """Validate all argument constraints."""
    if args.align <= 0:
        raise ValueError("--align must be > 0")
    if args.top_speed < 0:
        raise ValueError("--top-speed must be >= 0")
    if args.warp_speed <= 0:
        raise ValueError("--warp-speed must be > 0")
    if args.dock_time <= 0:
        raise ValueError("--dock-time must be > 0")
    if args.gate_time <= 0:
        raise ValueError("--gate-time must be > 0")


def load_sde_data(sde_path: str) -> Tuple[Dict, Dict, Dict, Dict]:
    """Load all SDE CSV.bz2 files into data structures.

    Returns:
        systems: dict name -> {id, security, x, y, z}
        jumps: list of (system_id1, system_id2) tuples
        stations: dict name -> {id, system_id, x, y, z}
        denormalize: dict id -> {type_id, x, y, z}
    """
    # Load mapSolarSystems.csv.bz2
    systems = {}
    with bz2.open(f"{sde_path}/mapSolarSystems.csv.bz2", "rt") as f:
        reader = csv.DictReader(f)
        for row in reader:
            systems[row["systemName"]] = {
                "id": int(row["systemID"]),
                "security": float(row["security"]),
                "x": float(row["x"]),
                "y": float(row["y"]),
                "z": float(row["z"])
            }

    # Load mapSolarSystemJumps.csv.bz2 - treat as bidirectional
    jumps = []
    system_id_to_name = {s["id"]: name for name, s in systems.items()}
    with bz2.open(f"{sde_path}/mapSolarSystemJumps.csv.bz2", "rt") as f:
        reader = csv.DictReader(f)
        for row in reader:
            from_id = int(row["fromSystemID"])
            to_id = int(row["toSystemID"])
            jumps.append((from_id, to_id))

    # Load staStations.csv.bz2
    stations = {}
    with bz2.open(f"{sde_path}/staStations.csv.bz2", "rt") as f:
        reader = csv.DictReader(f)
        for row in reader:
            station_name = row["stationName"]
            stations[station_name] = {
                "id": int(row["stationID"]),
                "system_id": int(row["solarSystemID"]),
                "x": float(row["x"]),
                "y": float(row["y"]),
                "z": float(row["z"])
            }

    # Load mapDenormalize.csv.bz2
    denormalize = {}
    with bz2.open(f"{sde_path}/mapDenormalize.csv.bz2", "rt") as f:
        reader = csv.DictReader(f)
        for row in reader:
            item_id = int(row["itemID"])
            denormalize[item_id] = {
                "type_id": int(row["typeID"]),
                "x": float(row["x"]),
                "y": float(row["y"]),
                "z": float(row["z"])
            }

    return systems, jumps, stations, denormalize


def build_system_graph(systems: Dict, jumps: List[Tuple]) -> Dict[int, Set[int]]:
    """Build adjacency list for system graph using system IDs."""
    graph = defaultdict(set)
    for from_id, to_id in jumps:
        graph[from_id].add(to_id)
        graph[to_id].add(from_id)
    return graph


def resolve_start_position(start: str, systems: Dict, stations: Dict) -> Tuple[str, str, bool]:
    """Resolve the start position.

    Returns:
        (type, name, system_name) where type is 'station' or 'system'
    """
    # Check if start is a station
    if start in stations:
        return "station", start, None
    # Check if start is a system
    if start in systems:
        return "system", start, None
    # Try partial match
    for station_name in stations:
        if station_name == start:
            return "station", station_name, None
    for system_name in systems:
        if system_name == start:
            return "system", system_name, None
    raise ValueError(f"Start location '{start}' not found")


def resolve_end_position(end: str, systems: Dict, stations: Dict) -> Tuple[str, str, Optional[str]]:
    """Resolve the end position.

    Returns:
        (type, name, station_name) where type is 'station' or 'system'
    """
    # Check if end is a station
    if end in stations:
        return "station", end, None
    # Check if end is a system
    if end in systems:
        return "system", end, None
    # Try partial match
    for station_name in stations:
        if station_name == end:
            return "station", station_name, None
    for system_name in systems:
        if system_name == end:
            return "system", system_name, None
    raise ValueError(f"End location '{end}' not found")


def calculate_warp_time(
    distance_m: float,
    warp_speed_au_s: float,
    top_speed_ms: float,
    align_time: float,
    dock_time: float,
    gate_time: float,
    is_first_leg: bool,
    is_last_leg: bool,
    current_location_is_station: bool,
    next_location_is_station: bool
) -> float:
    """Calculate total time for a warp leg including all phases.

    Args:
        distance_m: Distance in meters
        warp_speed_au_s: Maximum warp speed in AU/s
        top_speed_ms: Maximum subwarp speed in m/s
        align_time: Time to align in seconds
        dock_time: Time to dock/undock in seconds
        gate_time: Time to use a gate in seconds
        is_first_leg: True if this is the first warp leg (may need undock)
        is_last_leg: True if this is the last warp leg (may need dock)
        current_location_is_station: True if currently docked at a station
        next_location_is_station: True if destination is a station

    Returns:
        Total time in seconds
    """
    total_time = 0.0

    # Warp speed in m/s
    warp_speed_ms = warp_speed_au_s * AU_IN_M

    # Dropout speed
    v_drop = min(top_speed_ms / 2, 100)

    # Acceleration constant
    k_a = warp_speed_au_s  # AU/s

    # Deceleration constant (capped at 2 AU/s)
    k_d = min(warp_speed_au_s / 3, 2.0)

    # Acceleration distance in meters
    d_a = AU_IN_M  # 1 AU in meters

    # Deceleration distance in meters
    d_d = warp_speed_ms / k_d

    # Minimum warp distance
    d_min = d_a + d_d

    # Adjust warp speed if distance is less than minimum
    if distance_m < d_min:
        # Reduced peak warp speed
        v_warp_ms = distance_m * k_a * k_d / (k_a + k_d)
    else:
        v_warp_ms = warp_speed_ms

    # Convert back to AU/s for calculations
    v_warp_au_s = v_warp_ms / AU_IN_M

    # Acceleration time
    t_accel = (1 / k_a) * math.log(v_warp_au_s / k_a)

    # Deceleration time
    t_decel = (1 / k_d) * math.log(v_warp_au_s / (v_drop / AU_IN_M))

    total_time = t_accel + t_decel

    # Add cruise time if applicable
    if distance_m >= d_min:
        t_cruise = (distance_m - (d_a + d_d)) / v_warp_ms
        total_time += t_cruise

    # Add alignment time if not at station initially
    if not current_location_is_station:
        total_time += align_time

    # Add undock time if starting at station
    if current_location_is_station:
        total_time += dock_time

    # Add gate time (for each intermediate gate, but this is system-to-system, so 1 gate per jump)
    # Actually, gate time is for traversing a stargate within a system
    # This happens between systems, so add gate time for each jump
    if not is_last_leg and not next_location_is_station:
        # We're warping to a gate in the destination system
        total_time += gate_time

    # Add dock time if ending at station
    if is_last_leg and next_location_is_station:
        total_time += dock_time

    return total_time


def calculate_system_distance(sys1: Dict, sys2: Dict) -> float:
    """Calculate 3D distance between two systems in meters."""
    dx = sys1["x"] - sys2["x"]
    dy = sys1["y"] - sys2["y"]
    dz = sys1["z"] - sys2["z"]
    return math.sqrt(dx*dx + dy*dy + dz*dz)


def find_first_system_lexicographically(systems: Dict, system_id: int, graph: Dict) -> int:
    """Find the first system lexicographically to jump to from a given system.

    Used when starting in space at a gate.
    """
    system_name = None
    for name, data in systems.items():
        if data["id"] == system_id:
            system_name = name
            break

    if system_name is None:
        return None

    # Get all connected systems
    neighbors = graph.get(system_id, set())
    if not neighbors:
        return None

    # Find lexicographically smallest system name
    neighbor_names = {}
    for nid in neighbors:
        for name, data in systems.items():
            if data["id"] == nid:
                neighbor_names[name] = nid
                break

    # Return the system with lexicographically smallest name
    sorted_names = sorted(neighbor_names.keys())
    return neighbor_names[sorted_names[0]]


def find_path_dijkstra(
    start_system_id: int,
    end_system_id: int,
    systems: Dict,
    graph: Dict,
    stations: Dict,
    stations_in_system: Dict,
    args: argparse.Namespace
) -> Tuple[List[str], float]:
    """Find shortest path using Dijkstra's algorithm with distance weights.

    Returns:
        (list of system names in order, total time)
    """
    import heapq

    # For Zarzakh handling: we need to track (system_id, previous_system_id)
    # When at Zarzakh, the previous_system_id determines which gate we can exit from

    ZARZAKH_NAME = "Zarzakh"
    zarzakh_id = None
    for name, data in systems.items():
        if name == ZARZAKH_NAME:
            zarzakh_id = data["id"]
            break

    if zarzakh_id is None:
        # Zarzakh not found, use simple Dijkstra
        return simple_dijkstra(start_system_id, end_system_id, systems, graph, args)

    # Priority queue: (total_distance, system_id, path)
    pq = [(0, start_system_id, [])]
    visited = set()

    while pq:
        dist, current, path = heapq.heappop(pq)

        state = current
        if current == zarzakh_id:
            # We need to track the previous system for Zarzakh
            if path:
                state = (current, path[-1])
            else:
                state = (current, None)

        if state in visited:
            continue

        visited.add(state)

        if current == end_system_id or (isinstance(state, tuple) and state[0] == end_system_id):
            # Found the destination
            final_path = path + [current]
            return final_path, dist

        # Get neighbors
        neighbors = graph.get(current, set())

        for neighbor in neighbors:
            if neighbor in visited:
                continue

            # Check Zarzakh constraint
            if current == zarzakh_id:
                # Can only leave Zarzakh back to the system we came from
                prev_system = path[-1] if path else None
                if neighbor != prev_system:
                    continue

            new_dist = dist + calculate_system_distance(
                systems.get_name_from_id(current),
                systems.get_name_from_id(neighbor)
            )

            new_path = path + [current]
            heapq.heappush(pq, (new_dist, neighbor, new_path))

    raise ValueError("No path found")


def simple_dijkstra(start_system_id: int, end_system_id: int,
                    systems: Dict, graph: Dict, args: argparse.Namespace) -> Tuple[List[int], float]:
    """Simple Dijkstra without Zarzakh handling."""
    import heapq

    pq = [(0, start_system_id, [])]
    visited = set()

    while pq:
        dist, current, path = heapq.heappop(pq)

        if current in visited:
            continue

        visited.add(current)

        if current == end_system_id:
            final_path = path + [current]
            return final_path, dist

        neighbors = graph.get(current, set())

        for neighbor in neighbors:
            if neighbor in visited:
                continue

            new_dist = dist + calculate_system_distance(
                next((s for s, d in systems.items() if d["id"] == current), None),
                next((s for s, d in systems.items() if d["id"] == neighbor), None)
            )

            new_path = path + [current]
            heapq.heappush(pq, (new_dist, neighbor, new_path))

    raise ValueError("No path found")


def get_systems_in_region(systems: Dict) -> Dict:
    """Create lookup from system ID to system name."""
    return {data["id"]: name for name, data in systems.items()}


def format_time(seconds: float) -> str:
    """Format seconds as HH:MM, rounding up to nearest minute."""
    import math
    minutes = math.ceil(seconds / 60)
    hours = minutes // 60
    minutes = minutes % 60
    return f"{hours:02d}:{minutes:02d}"


def generate_travel_plan(
    start_type: str,
    start_name: str,
    end_type: str,
    end_name: str,
    systems: Dict,
    stations: Dict,
    graph: Dict,
    args: argparse.Namespace
) -> str:
    """Generate the travel plan output."""

    # Get system ID for start
    if start_type == "station":
        start_station = stations[start_name]
        start_system_id = start_station["system_id"]
        start_system_name = None
        for name, data in systems.items():
            if data["id"] == start_system_id:
                start_system_name = name
                break
    else:
        start_system_name = start_name
        start_system_id = systems[start_name]["id"]

    # Get system ID for end
    if end_type == "station":
        end_station = stations[end_name]
        end_system_id = end_station["system_id"]
        end_system_name = None
        for name, data in systems.items():
            if data["id"] == end_system_id:
                end_system_name = name
                break
    else:
        end_system_name = end_name
        end_system_id = systems[end_name]["id"]

    # Build system ID to name mapping
    system_id_to_name = {data["id"]: name for name, data in systems.items()}

    # Find path
    path = []
    total_time = 0.0

    if start_system_id == end_system_id:
        # Same system
        path = [start_system_id]

        # Calculate warp time within the same system
        if start_type == "station" and end_type == "station":
            # Station to station in same system
            start_station_data = stations[start_name]
            end_station_data = stations[end_name]
            dist = calculate_system_distance(start_station_data, end_station_data)
            total_time = calculate_warp_time(
                dist, args.warp_speed, args.top_speed,
                args.align, args.dock_time, args.gate_time,
                True, True, True, True
            )
        elif start_type == "station" and end_type == "system":
            # Station to space in same system
            start_station_data = stations[start_name]
            # We need to warp to a gate or just leave the station area
            # For simplicity, warp to the gate (first lexicographic neighbor)
            first_neighbor = find_first_system_lexicographically(systems, start_system_id, graph)
            if first_neighbor:
                # Get the gate position (use system center for now)
                dist = 100000  # Approximate distance from station to gate
                total_time = calculate_warp_time(
                    dist, args.warp_speed, args.top_speed,
                    args.align, args.dock_time, args.gate_time,
                    True, False, True, False
                )
                total_time += args.gate_time  # Gate time
            else:
                total_time = args.dock_time + args.align  # Just undock and align
        elif start_type == "system" and end_type == "station":
            # Space to station in same system
            end_station_data = stations[end_name]
            dist = 100000  # Approximate distance from gate to station
            total_time = calculate_warp_time(
                dist, args.warp_speed, args.top_speed,
                args.align, args.dock_time, args.gate_time,
                False, True, False, True
            )
        else:
            # Space to space in same system - not really valid for route completion
            total_time = args.align
    else:
        # Different systems - find path
        # For now, use a simplified Dijkstra that just finds the shortest path by distance

        # Simple BFS/Dijkstra for path finding
        import heapq
        pq = [(0, start_system_id, [start_system_id])]
        visited = set()

        while pq:
            dist, current, path-so-far = heapq.heappop(pq)

            if current in visited:
                continue
            visited.add(current)

            if current == end_system_id:
                path = path-so-far
                break

            for neighbor in graph.get(current, set()):
                if neighbor not in visited:
                    sys1_name = system_id_to_name.get(current, "")
                    sys2_name = system_id_to_name.get(neighbor, "")
                    if sys1_name and sys2_name:
                        new_dist = dist + calculate_system_distance(
                            systems[sys1_name], systems[sys2_name]
                        )
                        heapq.heappush(pq, (new_dist, neighbor, path-so-far + [neighbor]))

        # Calculate total time for the path
        # Start from the appropriate location
        current_is_station = (start_type == "station")
        total_time = 0.0

        for i, sys_id in enumerate(path):
            sys_name = system_id_to_name[sys_id]

            if i == 0:
                # First system
                if current_is_station:
                    # Undock
                    total_time += args.dock_time
                # Align
                total_time += args.align

                # Warp to first destination (either next system or final destination)
                if len(path) > 1:
                    next_sys_id = path[i + 1]
                    next_sys_name = system_id_to_name[next_sys_id]
                    dist = calculate_system_distance(systems[sys_name], systems[next_sys_name])
                    total_time += calculate_warp_time(
                        dist, args.warp_speed, args.top_speed,
                        args.align, args.dock_time, args.gate_time,
                        True, False, True, False
                    )
                    # Gate time
                    total_time += args.gate_time
                else:
                    # Direct to destination in same system
                    if end_type == "station":
                        # Warp to station
                        dist = 100000  # Approximate
                        total_time += calculate_warp_time(
                            dist, args.warp_speed, args.top_speed,
                            args.align, args.dock_time, args.gate_time,
                            True, True, True, True
                        )
                    else:
                        total_time += args.align
            else:
                # Intermediate or final system
                if i == len(path) - 1:
                    # Final system
                    if end_type == "station":
                        # Warp to gate then to station
                        dist = 100000  # Approximate to gate
                        total_time += calculate_warp_time(
                            dist, args.warp_speed, args.top_speed,
                            args.align, args.dock_time, args.gate_time,
                            False, True, False, True
                        )
                        # Gate time for the arrival gate
                        total_time += args.gate_time
                        # Dock
                        total_time += args.dock_time
                    else:
                        # Just arrive in system
                        total_time += args.align
                else:
                    # Intermediate system - warp to next
                    next_sys_id = path[i + 1]
                    next_sys_name = system_id_to_name[next_sys_id]
                    dist = calculate_system_distance(systems[sys_name], systems[next_sys_name])
                    total_time += calculate_warp_time(
                        dist, args.warp_speed, args.top_speed,
                        args.align, args.dock_time, args.gate_time,
                        False, False, False, False
                    )
                    total_time += args.gate_time

    # Format output
    output_lines = []

    # START line
    output_lines.append(f"START: {start_name}")

    # UNDOCK line if starting at station
    if start_type == "station":
        output_lines.append("UNDOCK")

    # GO line with route
    if len(path) > 1 or (len(path) == 1 and start_system_id != end_system_id):
        route_systems = [system_id_to_name[sid] for sid in path]
        # Add security info
        route_with_security = []
        for sys_name in route_systems:
            sec = systems[sys_name]["security"]
            route_with_security.append(f"{sys_name} ({sec:.1f})")
        output_lines.append("GO: " + " -> ".join(route_with_security))

    # DOCK line if ending at station
    if end_type == "station":
        output_lines.append(f"DOCK: {end_name}")

    # DONE line
    output_lines.append(f"DONE: {format_time(total_time)}")

    return "\n".join(output_lines)


def main():
    args = parse_args()
    validate_args(args)

    # Load SDE data
    systems, jumps, stations, denormalize = load_sde_data(args.sde)

    # Build system graph
    graph = build_system_graph(systems, jumps)

    # Resolve start and end positions
    start_type, start_name, _ = resolve_start_position(args.start, systems, stations)
    end_type, end_name, _ = resolve_end_position(args.end, systems, stations)

    # Handle special case: same location
    if start_type == "station" and end_type == "station" and start_name == end_name:
        print(f"START: {start_name}")
        print(f"DOCK: {end_name}")
        print(f"DONE: 00:01")
        return

    if start_type == "system" and end_type == "system" and start_name == end_name:
        print(f"START: {start_name}")
        print("DONE: 00:00")
        return

    # Generate and print travel plan
    output = generate_travel_plan(
        start_type, start_name,
        end_type, end_name,
        systems, stations, graph, args
    )
    print(output)


if __name__ == "__main__":
    main()
