#!/usr/bin/env python3
"""
EVE Online Travel Planning Tool

Calculates optimal routes through New Eden accounting for:
- Warp physics (acceleration, cruise, deceleration phases)
- Zarzakh gate locking mechanics
- Station docking/undocking times
- Gate jump times
"""

import argparse
import bz2
import csv
import heapq
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

# Constants
AU_IN_M = 149_597_870_700.0  # 1 AU in meters
ZARZAKH_SYSTEM_ID = 30100000
ZARZAKH_LOCK_TIME = 6 * 60 * 60  # 6 hours in seconds


@dataclass
class System:
    """Represents a solar system."""
    id: int
    name: str
    security: float
    x: float
    y: float
    z: float


@dataclass
class Station:
    """Represents a station."""
    id: int
    name: str
    system_id: int
    x: float
    y: float
    z: float


@dataclass
class Stargate:
    """Represents a stargate."""
    id: int
    system_id: int
    name: str
    x: float
    y: float
    z: float


@dataclass
class TravelParams:
    """Travel parameters from CLI."""
    align_time: float
    top_speed: float
    warp_speed: float
    dock_time: float
    gate_time: float


class SDELoader:
    """Loads EVE Static Data Export."""

    def __init__(self, sde_dir: str):
        self.sde_dir = sde_dir
        self.systems: Dict[int, System] = {}
        self.system_names: Dict[str, int] = {}
        self.stations: Dict[int, Station] = {}
        self.station_names: Dict[str, int] = {}
        self.stargates_by_system: Dict[int, List[Stargate]] = {}  # system_id -> list of gates
        self.gate_positions: Dict[int, Tuple[float, float, float]] = {}  # gate_id -> position
        self.gate_connections: Dict[int, int] = {}  # gate_id -> destination_gate_id
        self.system_jumps: Dict[int, List[int]] = {}  # system_id -> list of connected systems
        self.system_stations: Dict[int, List[Station]] = {}  # system_id -> stations

    def load_all(self):
        """Load all SDE data."""
        self._load_systems()
        self._load_stations()
        self._load_stargates()
        self._load_jumps()

    def _load_systems(self):
        """Load solar systems."""
        path = os.path.join(self.sde_dir, "mapSolarSystems.csv.bz2")
        with bz2.open(path, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                system = System(
                    id=int(row['solarSystemID']),
                    name=row['solarSystemName'],
                    security=float(row['security']),
                    x=float(row['x']),
                    y=float(row['y']),
                    z=float(row['z'])
                )
                self.systems[system.id] = system
                self.system_names[system.name.lower()] = system.id

    def _load_stations(self):
        """Load stations."""
        path = os.path.join(self.sde_dir, "staStations.csv.bz2")
        with bz2.open(path, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                station = Station(
                    id=int(row['stationID']),
                    name=row['stationName'],
                    system_id=int(row['solarSystemID']),
                    x=float(row['x']),
                    y=float(row['y']),
                    z=float(row['z'])
                )
                self.stations[station.id] = station
                self.station_names[station.name.lower()] = station.id

                if station.system_id not in self.system_stations:
                    self.system_stations[station.system_id] = []
                self.system_stations[station.system_id].append(station)

    def _load_stargates(self):
        """Load stargates from denormalize."""
        path = os.path.join(self.sde_dir, "mapDenormalize.csv.bz2")
        with bz2.open(path, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                group_id = int(row['groupID']) if row['groupID'] else 0
                if group_id != 10:  # Stargate group
                    continue

                system_id = int(row['solarSystemID']) if row['solarSystemID'] else None
                if system_id is None:
                    continue

                gate_id = int(row['itemID'])
                gate = Stargate(
                    id=gate_id,
                    system_id=system_id,
                    name=row['itemName'],
                    x=float(row['x']),
                    y=float(row['y']),
                    z=float(row['z'])
                )

                if system_id not in self.stargates_by_system:
                    self.stargates_by_system[system_id] = []
                self.stargates_by_system[system_id].append(gate)
                self.gate_positions[gate_id] = (gate.x, gate.y, gate.z)

    def _load_jumps(self):
        """Load system jumps and map gate connections."""
        path = os.path.join(self.sde_dir, "mapSolarSystemJumps.csv.bz2")
        with bz2.open(path, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                from_sys = int(row['fromSolarSystemID'])
                to_sys = int(row['toSolarSystemID'])

                if from_sys not in self.system_jumps:
                    self.system_jumps[from_sys] = []
                if to_sys not in self.system_jumps[from_sys]:
                    self.system_jumps[from_sys].append(to_sys)

                # Map gate connections based on system pairs
                from_gates = self.stargates_by_system.get(from_sys, [])
                to_gates = self.stargates_by_system.get(to_sys, [])

                from_system_name = self.systems[from_sys].name
                to_system_name = self.systems[to_sys].name

                for gate in from_gates:
                    # Extract destination name from gate name
                    gate_dest = gate.name.replace("Stargate (", "").replace(")", "")
                    if gate_dest != to_system_name:
                        continue

                    # Find matching destination gate
                    for dest_gate in to_gates:
                        dest_name = dest_gate.name.replace("Stargate (", "").replace(")", "")
                        if dest_name == from_system_name:
                            self.gate_connections[gate.id] = dest_gate.id
                            self.gate_connections[dest_gate.id] = gate.id
                            break
                    break

    def get_system_by_name(self, name: str) -> Optional[System]:
        """Get system by name (case insensitive)."""
        sys_id = self.system_names.get(name.lower())
        return self.systems.get(sys_id) if sys_id else None

    def get_station_by_name(self, name: str) -> Optional[Station]:
        """Get station by name (case insensitive)."""
        sta_id = self.station_names.get(name.lower())
        return self.stations.get(sta_id) if sta_id else None

    def resolve_location(self, name: str) -> Tuple[Optional[System], Optional[Station]]:
        """Resolve a location name to system and optionally station."""
        # Try station first (more specific)
        station = self.get_station_by_name(name)
        if station:
            system = self.systems[station.system_id]
            return system, station

        # Try system
        system = self.get_system_by_name(name)
        return system, None

    def get_gates_for_system(self, system_id: int) -> List[Stargate]:
        """Get all stargates in a system."""
        return self.stargates_by_system.get(system_id, [])

    def get_connected_systems(self, system_id: int) -> List[int]:
        """Get all systems connected via gates."""
        return self.system_jumps.get(system_id, [])

    def get_gate_for_destination(self, from_sys: int, to_sys: int) -> Optional[Stargate]:
        """Get the stargate in from_sys that leads to to_sys."""
        gates = self.stargates_by_system.get(from_sys, [])
        to_system_name = self.systems[to_sys].name

        for gate in gates:
            gate_dest = gate.name.replace("Stargate (", "").replace(")", "")
            if gate_dest == to_system_name:
                return gate
        return None

    def get_gate_destination_system(self, gate_id: int) -> Optional[int]:
        """Get the system ID that a gate leads to."""
        dest_gate_id = self.gate_connections.get(gate_id)
        if dest_gate_id is None:
            return None

        # Find which system the destination gate is in
        for sys_id, gates in self.stargates_by_system.items():
            for gate in gates:
                if gate.id == dest_gate_id:
                    return sys_id
        return None


def calculate_distance(x1: float, y1: float, z1: float,
                       x2: float, y2: float, z2: float) -> float:
    """Calculate Euclidean distance in meters."""
    return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2 + (z2 - z1) ** 2)


def calculate_warp_time_simple(distance_m: float, params: TravelParams) -> float:
    """
    Calculate warp time following the exact specification.

    The formulas:
    - v_w = warp speed in AU/s
    - v_s = subwarp speed in m/s
    - v_drop = min(v_s/2, 100) m/s
    - k_a = v_w (AU/s)
    - k_d = min(v_w/3, 2) (AU/s)
    - v_warp_ms = v_w * AU_IN_M

    - d_a = 1 AU
    - d_d = v_warp_ms / k_d
    - d_min = d_a + d_d

    If D < d_min:
        v_warp_ms = D * k_a * k_d / (k_a + k_d)

    t_accel = (1/k_a) * ln(v_warp_ms / (k_a * AU_IN_M))
    t_decel = (1/k_d) * ln(v_warp_ms / v_drop)

    If D >= d_min:
        t_cruise = (D - d_a - d_d) / v_warp_ms
    """
    if distance_m <= 0:
        return 0.0

    v_w = params.warp_speed  # AU/s
    v_s = params.top_speed  # m/s
    v_drop = min(v_s / 2.0, 100.0)  # m/s

    k_a = v_w  # AU/s
    k_d = min(v_w / 3.0, 2.0)  # AU/s

    # Initial max warp speed in m/s
    v_warp_ms = v_w * AU_IN_M

    # Distances
    d_a = AU_IN_M  # 1 AU in meters
    d_d = v_warp_ms / k_d  # deceleration distance
    d_min = d_a + d_d

    # Check if we reach full warp
    t_cruise = 0.0
    if distance_m < d_min:
        # Reduced peak warp speed
        v_warp_ms = (distance_m * k_a * k_d) / (k_a + k_d)
    else:
        # Full warp speed, cruise phase exists
        t_cruise = (distance_m - d_a - d_d) / v_warp_ms

    # Acceleration time
    v_warp_au = v_warp_ms / AU_IN_M
    if v_warp_au > k_a:
        t_accel = (1.0 / k_a) * math.log(v_warp_au / k_a)
    else:
        t_accel = 0.0

    # Deceleration time
    if v_warp_ms > v_drop:
        t_decel = (1.0 / k_d) * math.log(v_warp_ms / v_drop)
    else:
        t_decel = 0.0

    return t_accel + t_cruise + t_decel


# State representation for pathfinding
# (system_id, position_type, position_id, zarzakh_locked_gate)
# position_type: 'station', 'gate', or 'in_space'
# position_id: station_id, gate_id, or 0 for in_space

@dataclass(order=True)
class PQItem:
    """Priority queue item."""
    time: float
    data: Tuple = field(compare=False)  # (state_tuple, path, total_time)


class RoutePlanner:
    """Plans optimal routes through New Eden."""

    def __init__(self, sde: SDELoader, params: TravelParams):
        self.sde = sde
        self.params = params

    def plan(self, start_name: str, end_name: str) -> Tuple[float, List[str]]:
        """
        Plan route from start to end.

        Returns (total_time, route_systems) where route_systems is a list of system names.
        """
        # Resolve start and end locations
        start_sys, start_sta = self.sde.resolve_location(start_name)
        end_sys, end_sta = self.sde.resolve_location(end_name)

        if start_sys is None:
            raise ValueError(f"Could not resolve start location: {start_name}")
        if end_sys is None:
            raise ValueError(f"Could not resolve end location: {end_name}")

        # Special case: same system
        if start_sys.id == end_sys.id:
            total_time = self._same_system_time(start_sys, start_sta, end_sta)
            return total_time, [start_sys.name]

        # General pathfinding
        return self._find_path(start_sys, start_sta, end_sys, end_sta)

    def _same_system_time(self, system: System, start_sta: Optional[Station],
                          end_sta: Optional[Station]) -> float:
        """Calculate time for travel within same system."""
        gates = self.sde.get_gates_for_system(system.id)
        gates_sorted = sorted(gates, key=lambda g: g.name)
        start_gate = gates_sorted[0] if gates_sorted else None

        if start_sta and end_sta:
            # Station to station
            distance = calculate_distance(start_sta.x, start_sta.y, start_sta.z,
                                          end_sta.x, end_sta.y, end_sta.z)
            warp_time = calculate_warp_time_simple(distance, self.params)
            return self.params.dock_time + self.params.align_time + warp_time + self.params.dock_time

        elif start_sta and not end_sta:
            # Station to system (exit)
            if start_gate:
                distance = calculate_distance(start_sta.x, start_sta.y, start_sta.z,
                                              start_gate.x, start_gate.y, start_gate.z)
                warp_time = calculate_warp_time_simple(distance, self.params)
                return self.params.dock_time + self.params.align_time + warp_time + self.params.gate_time
            return self.params.dock_time

        elif not start_sta and end_sta:
            # System entry to station
            if start_gate:
                distance = calculate_distance(start_gate.x, start_gate.y, start_gate.z,
                                              end_sta.x, end_sta.y, end_sta.z)
                warp_time = calculate_warp_time_simple(distance, self.params)
                return self.params.align_time + warp_time + self.params.dock_time
            return self.params.dock_time

        else:
            # System to system (same system) - no time
            return 0.0

    def _find_path(self, start_sys: System, start_sta: Optional[Station],
                   end_sys: System, end_sta: Optional[Station]) -> Tuple[float, List[str]]:
        """Find optimal path using Dijkstra's algorithm with Zarzakh constraints."""

        # Priority queue: (time, state_tuple, path)
        # state_tuple: (system_id, pos_type, pos_id, zarzakh_lock)
        pq: List[PQItem] = []

        # Track best time to reach each state
        best: Dict[Tuple, float] = {}

        # Initialize based on start type
        if start_sta:
            # Starting docked at station - first need to undock
            initial_state = (start_sys.id, 'station', start_sta.id, None)
            initial_time = 0.0
            # After undock, we're in space at station position
            initial_path = [(start_sys.id, 'station', start_sta.id, 0.0)]
        else:
            # Starting in space at first lexicographic gate
            gates = self.sde.get_gates_for_system(start_sys.id)
            gates_sorted = sorted(gates, key=lambda g: g.name)
            if gates_sorted:
                start_gate = gates_sorted[0]
                initial_state = (start_sys.id, 'gate', start_gate.id, None)
            else:
                initial_state = (start_sys.id, 'in_space', 0, None)
            initial_time = 0.0
            initial_path = [(start_sys.id, 'gate', gates_sorted[0].id if gates_sorted else 0, 0.0)]

        heapq.heappush(pq, PQItem(initial_time, (initial_state, initial_path)))
        best[initial_state] = initial_time

        while pq:
            item = heapq.heappop(pq)
            current_time = item.time
            state_tuple, path = item.data
            sys_id, pos_type, pos_id, zarzakh_lock = state_tuple

            # Check if we've reached destination
            if sys_id == end_sys.id:
                # Calculate remaining time to reach end position
                remaining_time = self._time_to_end(sys_id, pos_type, pos_id, end_sta, zarzakh_lock)
                total_time = current_time + remaining_time

                # Build route from path
                route = self._build_route(path, start_sys, start_sta, end_sys, end_sta)
                return total_time, route

            state_key = state_tuple
            if current_time > best.get(state_key, float('inf')):
                continue

            # Expand neighbors
            neighbors = self._get_neighbors(sys_id, pos_type, pos_id, zarzakh_lock)

            for next_state, action_time in neighbors:
                next_key = next_state
                new_time = current_time + action_time

                if next_key not in best or new_time < best[next_key]:
                    best[next_key] = new_time
                    new_path = path + [(next_state[0], next_state[1], next_state[2], action_time)]
                    heapq.heappush(pq, PQItem(new_time, (next_state, new_path)))

        raise ValueError("No route found")

    def _time_to_end(self, sys_id: int, pos_type: str, pos_id: int,
                     end_sta: Optional[Station], zarzakh_lock: Optional[int]) -> float:
        """Calculate remaining time from current position to end."""
        # Get current position
        if pos_type == 'station':
            station = self.sde.stations[pos_id]
            pos = (station.x, station.y, station.z)
        elif pos_type == 'gate':
            pos = self.sde.gate_positions.get(pos_id, (0, 0, 0))
        else:
            pos = (0, 0, 0)

        if end_sta:
            # Need to dock at station
            distance = calculate_distance(pos[0], pos[1], pos[2],
                                          end_sta.x, end_sta.y, end_sta.z)
            warp_time = calculate_warp_time_simple(distance, self.params)

            if pos_type == 'station':
                # Undock, align, warp, dock
                return self.params.dock_time + self.params.align_time + warp_time + self.params.dock_time
            else:
                # Align, warp, dock
                return self.params.align_time + warp_time + self.params.dock_time
        else:
            # End is system - already there
            return 0.0

    def _get_neighbors(self, sys_id: int, pos_type: str, pos_id: int,
                       zarzakh_lock: Optional[int]) -> List[Tuple[Tuple, float]]:
        """Get neighboring states from current position."""
        neighbors = []

        system = self.sde.systems[sys_id]
        gates = self.sde.get_gates_for_system(sys_id)
        stations = self.sde.system_stations.get(sys_id, [])

        # Get current position
        if pos_type == 'station':
            station = self.sde.stations[pos_id]
            pos = (station.x, station.y, station.z)
        elif pos_type == 'gate':
            pos = self.sde.gate_positions.get(pos_id, (0, 0, 0))
        else:
            pos = (0, 0, 0)

        # If at station, can undock and warp to gates
        if pos_type == 'station':
            for gate in gates:
                distance = calculate_distance(pos[0], pos[1], pos[2],
                                              gate.x, gate.y, gate.z)
                warp_time = calculate_warp_time_simple(distance, self.params)
                # Undock + align + warp
                time_cost = self.params.dock_time + self.params.align_time + warp_time

                next_state = (sys_id, 'gate', gate.id, zarzakh_lock)
                neighbors.append((next_state, time_cost))

        # If at gate, can jump through or warp to other gates
        if pos_type == 'gate':
            current_gate_id = pos_id

            # Jump through gate
            dest_sys_id = self.sde.get_gate_destination_system(current_gate_id)
            if dest_sys_id:
                dest_gate_id = self.sde.gate_connections.get(current_gate_id)

                # Check Zarzakh constraints
                can_jump = True
                new_lock = zarzakh_lock

                # Entering Zarzakh
                if dest_sys_id == ZARZAKH_SYSTEM_ID:
                    new_lock = current_gate_id

                # Leaving Zarzakh
                if sys_id == ZARZAKH_SYSTEM_ID:
                    if zarzakh_lock is not None and current_gate_id != zarzakh_lock:
                        can_jump = False
                    else:
                        new_lock = None

                if can_jump and dest_gate_id:
                    next_state = (dest_sys_id, 'gate', dest_gate_id, new_lock)
                    neighbors.append((next_state, self.params.gate_time))

            # Warp to other gates in same system
            for gate in gates:
                if gate.id == current_gate_id:
                    continue
                distance = calculate_distance(pos[0], pos[1], pos[2],
                                              gate.x, gate.y, gate.z)
                warp_time = calculate_warp_time_simple(distance, self.params)
                time_cost = self.params.align_time + warp_time

                next_state = (sys_id, 'gate', gate.id, zarzakh_lock)
                neighbors.append((next_state, time_cost))

        return neighbors

    def _build_route(self, path: List[Tuple], start_sys: System,
                     start_sta: Optional[Station], end_sys: System,
                     end_sta: Optional[Station]) -> List[str]:
        """Build list of system names from path."""
        route = []
        prev_sys = None

        for state in path:
            sys_id = state[0]
            if sys_id != prev_sys:
                system = self.sde.systems[sys_id]
                route.append(system.name)
                prev_sys = sys_id

        # Ensure end system is included
        if not route or route[-1] != end_sys.name:
            route.append(end_sys.name)

        return route


def format_time(seconds: float) -> str:
    """Format time in HH:MM format, rounded up to nearest minute."""
    total_minutes = math.ceil(seconds / 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours:02d}:{minutes:02d}"


def main():
    parser = argparse.ArgumentParser(description="EVE Online Travel Planner")
    parser.add_argument("start", help="Starting location (system or station name)")
    parser.add_argument("end", help="Ending location (system or station name)")
    parser.add_argument("--align", type=float, required=True, help="Alignment time in seconds")
    parser.add_argument("--top-speed", type=float, required=True, help="Top subwarp speed in m/s")
    parser.add_argument("--warp-speed", type=float, required=True, help="Warp speed in AU/s")
    parser.add_argument("--dock-time", type=float, required=True, help="Dock/undock time in seconds")
    parser.add_argument("--gate-time", type=float, required=True, help="Gate jump time in seconds")
    parser.add_argument("--sde", type=str, required=True, help="Path to SDE directory")

    args = parser.parse_args()

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

    # Align time is ceiling'd due to server ticks
    align_time = math.ceil(args.align)

    params = TravelParams(
        align_time=align_time,
        top_speed=args.top_speed,
        warp_speed=args.warp_speed,
        dock_time=args.dock_time,
        gate_time=args.gate_time
    )

    # Load SDE
    sde = SDELoader(args.sde)
    sde.load_all()

    # Plan route
    planner = RoutePlanner(sde, params)
    try:
        total_time, route = planner.plan(args.start, args.end)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Output formatted plan
    output_plan(sde, args.start, args.end, total_time, route)


def output_plan(sde: SDELoader, start_name: str, end_name: str,
                total_time: float, route: List[str]):
    """Output the formatted travel plan."""
    # Resolve locations
    start_sys, start_sta = sde.resolve_location(start_name)
    end_sys, end_sta = sde.resolve_location(end_name)

    # START line
    if start_sta:
        print(f"START: {start_sta.name}")
    else:
        print(f"START: {start_sys.name}")

    # UNDOCK if starting from station
    if start_sta:
        print("UNDOCK")

    # GO line with route
    if len(route) > 1:
        route_parts = []
        for sys_name in route:
            system = sde.get_system_by_name(sys_name)
            if system:
                route_parts.append(f"{system.name} ({system.security:.1f})")
            else:
                route_parts.append(sys_name)
        print("GO: " + " -> ".join(route_parts))
    elif len(route) == 1 and not end_sta:
        # Single system route, end is system
        system = sde.get_system_by_name(route[0])
        if system:
            print(f"GO: {system.name} ({system.security:.1f})")

    # DOCK if ending at station
    if end_sta:
        print(f"DOCK: {end_sta.name}")

    # DONE line
    print(f"DONE: {format_time(total_time)}")


if __name__ == "__main__":
    main()
