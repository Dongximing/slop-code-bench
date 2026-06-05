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
from collections import deque
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
    x: float  # in meters
    y: float
    z: float


@dataclass
class Stargate:
    """Represents a stargate."""
    id: int
    system_id: int
    name: str
    x: float  # in meters
    y: float
    z: float


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
            security=row['security'],
            x=float(row['x']),
            y=float(row['y']),
            z=float(row['z'])
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


def load_stargates(sde_dir: str) -> dict[int, list[Stargate]]:
    """Load stargates from SDE, grouped by system."""
    denormalize_file = os.path.join(sde_dir, 'mapDenormalize.csv.bz2')

    with bz2.open(denormalize_file, 'rt') as f:
        df = pd.read_csv(f)

    # Filter for stargates (groupID 10)
    stargates_df = df[df['groupID'] == 10]

    stargates_by_system = {}
    for _, row in stargates_df.iterrows():
        system_id = int(row['solarSystemID'])
        stargate = Stargate(
            id=row['itemID'],
            system_id=system_id,
            name=row['itemName'],
            x=float(row['x']),
            y=float(row['y']),
            z=float(row['z'])
        )
        if system_id not in stargates_by_system:
            stargates_by_system[system_id] = []
        stargates_by_system[system_id].append(stargate)

    return stargates_by_system


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


def distance_meters(x1: float, y1: float, z1: float, x2: float, y2: float, z2: float) -> float:
    """Calculate distance in meters between two 3D points."""
    dx = x1 - x2
    dy = y1 - y2
    dz = z1 - z2
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def find_hs_entrance_candidates(
    destination_hs_system: System,
    systems: dict[int, System],
    stations: dict[str, Station],
    system_jumps: dict[int, list[int]],
    stargates_by_system: dict[int, list[Stargate]],
    max_extra_gates: int
) -> list[tuple[System, Station, float, int]]:
    """
    Find all non-HS systems with stations that have gates connecting to HS systems.
    Returns list of (entrance_system, best_station, warp_distance_to_gate, extra_hs_gates)
    sorted by preference.

    Preference order for HS destination routes:
    1. Distance to entrance gate (minimize)
    2. Min High Sec Gates (minimize)
    3. Min Jumps (handled by pathfinding)
    4. Min Time Waiting (handled by pathfinding)
    5. Total Trip LY (handled by pathfinding)
    6. Lexicographic sorting of systems
    """
    # First, collect all non-HS entrance systems with their minimum extra gates
    entrance_min_gates = {}  # {entrance_system_id: min_extra_gates}

    # Find all HS systems reachable via HS gates from destination (within max_extra_gates)
    reachable_hs_systems = find_hs_systems_within_gates(
        destination_hs_system, systems, system_jumps, max_extra_gates
    )

    for hs_system in reachable_hs_systems:
        extra_gates = count_hs_gates_between(hs_system, destination_hs_system, systems, system_jumps)
        if extra_gates < 0 or extra_gates > max_extra_gates:
            continue

        # Find non-HS neighbors of this HS system
        if hs_system.id not in system_jumps:
            continue

        for neighbor_id in system_jumps[hs_system.id]:
            if neighbor_id not in systems:
                continue
            neighbor = systems[neighbor_id]

            # Must be non-HS (lowsec or nullsec)
            if is_high_sec(neighbor.security):
                continue

            # Must not be Pochven or Zarzakh
            if is_pochven(neighbor.region_id) or is_zarzakh(neighbor.id):
                continue

            # Must have at least one station
            if not find_stations_in_system(neighbor.id, stations):
                continue

            # Track minimum extra gates for this entrance system
            if neighbor.id not in entrance_min_gates:
                entrance_min_gates[neighbor.id] = extra_gates
            else:
                entrance_min_gates[neighbor.id] = min(entrance_min_gates[neighbor.id], extra_gates)

    # Now build candidates with proper stargate info
    candidates = []
    for entrance_id, min_gates in entrance_min_gates.items():
        entrance_system = systems[entrance_id]
        entrance_stations = find_stations_in_system(entrance_id, stations)

        # Find all HS neighbors of entrance to find closest stargate
        best_station = None
        best_distance = float('inf')

        for neighbor_id in system_jumps.get(entrance_id, []):
            neighbor = systems.get(neighbor_id)
            if neighbor and is_high_sec(neighbor.security):
                # Check if this HS neighbor is within our max_extra_gates
                gates = count_hs_gates_between(neighbor, destination_hs_system, systems, system_jumps)
                if gates < 0 or gates > max_extra_gates:
                    continue

                # Find stargate in entrance system connecting to this HS neighbor
                entrance_stargates = stargates_by_system.get(entrance_id, [])
                entrance_stargate = None
                for sg in entrance_stargates:
                    if neighbor.name in sg.name:
                        entrance_stargate = sg
                        break

                if entrance_stargate is None and entrance_stargates:
                    entrance_stargate = entrance_stargates[0]

                if entrance_stargate:
                    # Find closest station to this stargate
                    for station in entrance_stations:
                        dist = distance_meters(
                            station.x, station.y, station.z,
                            entrance_stargate.x, entrance_stargate.y, entrance_stargate.z
                        )
                        if dist < best_distance:
                            best_distance = dist
                            best_station = station

        if best_station is None:
            best_station = min(entrance_stations, key=lambda s: s.name)
            best_distance = 0

        candidates.append((entrance_system, best_station, best_distance, min_gates))

    # Sort by preference: warp distance, extra HS gates, system name
    candidates.sort(key=lambda x: (x[2], x[3], x[0].name))

    return candidates


def find_hs_systems_within_gates(
    start_hs: System,
    systems: dict[int, System],
    system_jumps: dict[int, list[int]],
    max_gates: int
) -> list[System]:
    """Find all HS systems within max_gates from start_hs using BFS."""
    result = [start_hs]

    if max_gates == 0:
        return result

    visited = {start_hs.id}
    queue = deque([(start_hs.id, 0)])

    while queue:
        current_id, gates = queue.popleft()

        if current_id not in system_jumps:
            continue

        for neighbor_id in system_jumps[current_id]:
            if neighbor_id in visited:
                continue

            neighbor = systems.get(neighbor_id)
            if neighbor is None:
                continue

            # Only travel through HS
            if not is_high_sec(neighbor.security):
                continue

            visited.add(neighbor_id)
            new_gates = gates + 1

            if new_gates <= max_gates:
                result.append(neighbor)
                queue.append((neighbor_id, new_gates))

    return result


def count_hs_gates_between(
    start_hs: System,
    end_hs: System,
    systems: dict[int, System],
    system_jumps: dict[int, list[int]]
) -> int:
    """
    Count the minimum number of HS gates between two HS systems.
    Uses BFS. Returns -1 if no path exists.
    """
    if start_hs.id == end_hs.id:
        return 0

    visited = {start_hs.id}
    queue = deque([(start_hs.id, 0)])

    while queue:
        current_id, gates = queue.popleft()

        if current_id not in system_jumps:
            continue

        for neighbor_id in system_jumps[current_id]:
            if neighbor_id in visited:
                continue

            neighbor = systems.get(neighbor_id)
            if neighbor is None:
                continue

            # Only travel through HS
            if not is_high_sec(neighbor.security):
                continue

            if neighbor_id == end_hs.id:
                return gates + 1

            visited.add(neighbor_id)
            queue.append((neighbor_id, gates + 1))

    return -1  # No path found


def find_hs_gate_path(
    start_hs: System,
    end_hs: System,
    systems: dict[int, System],
    system_jumps: dict[int, list[int]]
) -> list[System]:
    """
    Find the path of HS systems between two HS systems.
    Returns list of systems including start and end, or empty list if no path.
    """
    if start_hs.id == end_hs.id:
        return [start_hs]

    visited = {start_hs.id}
    parent = {start_hs.id: None}
    queue = deque([start_hs.id])

    while queue:
        current_id = queue.popleft()

        if current_id not in system_jumps:
            continue

        for neighbor_id in system_jumps[current_id]:
            if neighbor_id in visited:
                continue

            neighbor = systems.get(neighbor_id)
            if neighbor is None:
                continue

            # Only travel through HS
            if not is_high_sec(neighbor.security):
                continue

            visited.add(neighbor_id)
            parent[neighbor_id] = current_id

            if neighbor_id == end_hs.id:
                # Reconstruct path
                path = []
                node = end_hs.id
                while node is not None:
                    path.append(systems[node])
                    node = parent[node]
                path.reverse()
                return path

            queue.append(neighbor_id)

    return []  # No path found


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


def format_security(security: float) -> str:
    """Format security to 1 decimal place."""
    return f"{round(security, 1)}"


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


@dataclass
class HSReturnPath:
    """Represents the return path to HS destination."""
    entrance_system: System  # Non-HS system with station
    entrance_station: Station  # Station in entrance system
    hs_gate_path: list[System]  # Path of HS systems from entrance HS to destination HS
    entrance_hs_system: System  # The HS system the entrance connects to


def find_optimal_path_to_hs_entrance(
    start_system: System,
    entrance_system: System,
    entrance_station: Station,
    systems: dict[int, System],
    stations: dict[str, Station],
    max_range: float,
    fuel_per_jump: int,
    reduction: int,
    system_jumps: dict[int, list[int]] = None,
    use_cloak: bool = False
) -> list[JumpStep]:
    """
    Find the optimal jump path to an HS entrance system.
    Similar to find_optimal_path but targets a specific station.
    """
    # Build candidate midpoints - systems with stations that can be destinations
    valid_midpoints = {}
    for sys_id, sys in systems.items():
        sys_stations = find_stations_in_system(sys_id, stations)
        if sys_stations:
            valid_midpoints[sys_id] = sys

    import heapq

    initial_state = (0, 0, 0.0, "", start_system.id, 0.0, 0.0, [], use_cloak)
    pq = [initial_state]
    best = {}

    while pq:
        jumps, ceiled_wait, total_ly, _, current_sys_id, fatigue, cooldown, path, cloak_available = heapq.heappop(pq)

        current_system = systems[current_sys_id]

        # Check if we reached the entrance system
        if current_sys_id == entrance_system.id:
            result = []
            state = JumpState()

            for from_id, to_id, dist, iso, dock_name, wait, is_gate, gate_from_id in path:
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
            # unless it's the entrance system
            if next_sys_id != entrance_system.id and not can_be_destination(next_system, systems):
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

            wait_before = 0.0
            if new_cooldown > 0:
                wait_before = new_cooldown
                new_time_waiting = wait_before
                new_cooldown = 0.0

            effective_distance = (1 - reduction / 100.0) * distance

            if new_fatigue == 0 and new_cooldown == 0:
                new_fatigue = 10.0 * (1.0 + effective_distance)
                new_cooldown = 1.0 + effective_distance
            else:
                max_fatigue = max(new_fatigue, 10.0)
                new_fatigue = max_fatigue * (1.0 + effective_distance)
                new_cooldown = new_fatigue / 10.0

            new_fatigue = min(new_fatigue, 300.0)
            new_cooldown = min(new_cooldown, 30.0)
            new_fatigue = max(0.0, new_fatigue)
            new_cooldown = max(0.0, new_cooldown)

            # Find stations in target system
            target_stations = find_stations_in_system(next_sys_id, stations)
            if not target_stations:
                continue

            # Pick the entrance station if we're going to entrance system, else lexicographically first
            if next_sys_id == entrance_system.id:
                best_station = entrance_station
            else:
                best_station = min(target_stations, key=lambda s: s.name)

            new_total_ly = total_ly + distance
            new_jumps = jumps + 1

            total_time_waiting = sum(p[5] for p in path) + new_time_waiting
            new_ceiled_wait = math.ceil(total_time_waiting)

            new_rounded_ly = round(new_total_ly, 2)

            new_cloak_available = False

            state_key = (next_sys_id, new_cloak_available)

            if state_key in best:
                best_jumps, best_wait, best_ly, best_systems = best[state_key]
                path_systems = tuple(systems[p[1]].name for p in (path + [(current_sys_id, next_sys_id)]))
                if (new_jumps, new_ceiled_wait, new_rounded_ly, path_systems) >= (best_jumps, best_wait, best_ly, best_systems):
                    continue

            path_systems = tuple(systems[p[1]].name for p in (path + [(current_sys_id, next_sys_id)]))
            best[state_key] = (new_jumps, new_ceiled_wait, new_rounded_ly, path_systems)

            iso = calculate_isotopes(distance, fuel_per_jump)
            new_path = path + [(current_sys_id, next_sys_id, distance, iso, best_station.name, wait_before, False, None)]

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

        # Gate jumps (cloak mode)
        if cloak_available and system_jumps and current_sys_id in system_jumps:
            for gate_dest_id in system_jumps[current_sys_id]:
                if gate_dest_id not in systems:
                    continue

                gate_dest_system = systems[gate_dest_id]

                new_cloak_available = False

                for next_sys_id, next_system in valid_midpoints.items():
                    if next_sys_id == gate_dest_id:
                        continue

                    if next_sys_id != entrance_system.id and not can_be_destination(next_system, systems):
                        continue

                    distance = gate_dest_system.distance_ly(next_system)

                    if distance > max_range:
                        continue

                    new_fatigue = fatigue
                    new_cooldown = cooldown
                    new_time_waiting = 0.0

                    wait_before = 0.0
                    if new_cooldown > 0:
                        wait_before = new_cooldown
                        new_time_waiting = wait_before
                        new_cooldown = 0.0

                    effective_distance = (1 - reduction / 100.0) * distance

                    if new_fatigue == 0 and new_cooldown == 0:
                        new_fatigue = 10.0 * (1.0 + effective_distance)
                        new_cooldown = 1.0 + effective_distance
                    else:
                        max_fatigue = max(new_fatigue, 10.0)
                        new_fatigue = max_fatigue * (1.0 + effective_distance)
                        new_cooldown = new_fatigue / 10.0

                    new_fatigue = min(new_fatigue, 300.0)
                    new_cooldown = min(new_cooldown, 30.0)
                    new_fatigue = max(0.0, new_fatigue)
                    new_cooldown = max(0.0, new_cooldown)

                    target_stations = find_stations_in_system(next_sys_id, stations)
                    if not target_stations:
                        continue

                    if next_sys_id == entrance_system.id:
                        best_station = entrance_station
                    else:
                        best_station = min(target_stations, key=lambda s: s.name)

                    new_total_ly = total_ly + distance
                    new_jumps = jumps + 1

                    total_time_waiting = sum(p[5] for p in path) + new_time_waiting
                    new_ceiled_wait = math.ceil(total_time_waiting)

                    new_rounded_ly = round(new_total_ly, 2)

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
                    new_path = path + [
                        (current_sys_id, gate_dest_id, 0.0, 0, best_station.name, 0.0, True, current_sys_id),
                        (gate_dest_id, next_sys_id, distance, iso, best_station.name, wait_before, False, None)
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

    initial_state = (0, 0, 0.0, "", start_system.id, 0.0, 0.0, [], use_cloak)
    pq = [initial_state]

    best = {}

    while pq:
        jumps, ceiled_wait, total_ly, _, current_sys_id, fatigue, cooldown, path, cloak_available = heapq.heappop(pq)

        current_system = systems[current_sys_id]

        # Check if we reached destination
        if current_sys_id == end_system.id:
            result = []
            state = JumpState()

            for from_id, to_id, dist, iso, dock_name, wait, is_gate, gate_from_id in path:
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

            wait_before = 0.0
            if new_cooldown > 0:
                wait_before = new_cooldown
                new_time_waiting = wait_before
                new_cooldown = 0.0

            effective_distance = (1 - reduction / 100.0) * distance

            if new_fatigue == 0 and new_cooldown == 0:
                new_fatigue = 10.0 * (1.0 + effective_distance)
                new_cooldown = 1.0 + effective_distance
            else:
                max_fatigue = max(new_fatigue, 10.0)
                new_fatigue = max_fatigue * (1.0 + effective_distance)
                new_cooldown = new_fatigue / 10.0

            new_fatigue = min(new_fatigue, 300.0)
            new_cooldown = min(new_cooldown, 30.0)
            new_fatigue = max(0.0, new_fatigue)
            new_cooldown = max(0.0, new_cooldown)

            target_stations = find_stations_in_system(next_sys_id, stations)
            if not target_stations:
                continue

            best_station = min(target_stations, key=lambda s: s.name)

            new_total_ly = total_ly + distance
            new_jumps = jumps + 1

            total_time_waiting = sum(p[5] for p in path) + new_time_waiting
            new_ceiled_wait = math.ceil(total_time_waiting)

            new_rounded_ly = round(new_total_ly, 2)

            new_cloak_available = False

            state_key = (next_sys_id, new_cloak_available)

            if state_key in best:
                best_jumps, best_wait, best_ly, best_systems = best[state_key]
                path_systems = tuple(systems[p[1]].name for p in (path + [(current_sys_id, next_sys_id)]))
                if (new_jumps, new_ceiled_wait, new_rounded_ly, path_systems) >= (best_jumps, best_wait, best_ly, best_systems):
                    continue

            path_systems = tuple(systems[p[1]].name for p in (path + [(current_sys_id, next_sys_id)]))
            best[state_key] = (new_jumps, new_ceiled_wait, new_rounded_ly, path_systems)

            iso = calculate_isotopes(distance, fuel_per_jump)
            new_path = path + [(current_sys_id, next_sys_id, distance, iso, best_station.name, wait_before, False, None)]

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

        # Gate jumps (cloak mode)
        if cloak_available and system_jumps and current_sys_id in system_jumps:
            for gate_dest_id in system_jumps[current_sys_id]:
                if gate_dest_id not in systems:
                    continue

                gate_dest_system = systems[gate_dest_id]

                new_cloak_available = False

                for next_sys_id, next_system in valid_midpoints.items():
                    if next_sys_id == gate_dest_id:
                        continue

                    if next_sys_id != end_system.id and not can_be_destination(next_system, systems):
                        continue

                    distance = gate_dest_system.distance_ly(next_system)

                    if distance > max_range:
                        continue

                    new_fatigue = fatigue
                    new_cooldown = cooldown
                    new_time_waiting = 0.0

                    wait_before = 0.0
                    if new_cooldown > 0:
                        wait_before = new_cooldown
                        new_time_waiting = wait_before
                        new_cooldown = 0.0

                    effective_distance = (1 - reduction / 100.0) * distance

                    if new_fatigue == 0 and new_cooldown == 0:
                        new_fatigue = 10.0 * (1.0 + effective_distance)
                        new_cooldown = 1.0 + effective_distance
                    else:
                        max_fatigue = max(new_fatigue, 10.0)
                        new_fatigue = max_fatigue * (1.0 + effective_distance)
                        new_cooldown = new_fatigue / 10.0

                    new_fatigue = min(new_fatigue, 300.0)
                    new_cooldown = min(new_cooldown, 30.0)
                    new_fatigue = max(0.0, new_fatigue)
                    new_cooldown = max(0.0, new_cooldown)

                    target_stations = find_stations_in_system(next_sys_id, stations)
                    if not target_stations:
                        continue

                    best_station = min(target_stations, key=lambda s: s.name)

                    new_total_ly = total_ly + distance
                    new_jumps = jumps + 1

                    total_time_waiting = sum(p[5] for p in path) + new_time_waiting
                    new_ceiled_wait = math.ceil(total_time_waiting)

                    new_rounded_ly = round(new_total_ly, 2)

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
                    new_path = path + [
                        (current_sys_id, gate_dest_id, 0.0, 0, best_station.name, 0.0, True, current_sys_id),
                        (gate_dest_id, next_sys_id, distance, iso, best_station.name, wait_before, False, None)
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
    parser.add_argument('--max-extra-gates', '-gates', type=int, default=0,
                        help='Maximum extra HS gates when destination is in HS (default: 0)')

    args = parser.parse_args()

    # Load data
    try:
        systems = load_systems(args.sde)
        stations = load_stations(args.sde)
        system_jumps = load_system_jumps(args.sde)
        stargates_by_system = load_stargates(args.sde)
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

    # Check if destination is in HS
    is_hs_destination = is_high_sec(end_system.security)

    if is_hs_destination:
        # Check if start is also in HS (need to handle this case)
        if is_high_sec(start_system.security):
            # Both in HS - we can't use JF jump, must gate
            print(f"Error: Both start and end stations are in HS. No jump path needed.", file=sys.stderr)
            sys.exit(1)

        # Find entrance candidates for HS destination
        entrance_candidates = find_hs_entrance_candidates(
            end_system, systems, stations, system_jumps,
            stargates_by_system, args.max_extra_gates
        )

        if not entrance_candidates:
            print(f"Error: No entrance system found to reach HS destination", file=sys.stderr)
            sys.exit(1)

        # Try each entrance candidate and find the best overall route
        # Limit to top 10 candidates for performance
        best_route = None
        best_hs_return = None
        best_score = None

        for entrance_system, entrance_station, warp_dist, extra_gates in entrance_candidates[:10]:
            # Find path to entrance
            path_to_entrance = find_optimal_path_to_hs_entrance(
                start_system,
                entrance_system,
                entrance_station,
                systems,
                stations,
                float(args.range),
                args.fuel,
                args.reduction,
                system_jumps=system_jumps if args.cloak else None,
                use_cloak=args.cloak
            )

            if not path_to_entrance:
                continue

            # Calculate HS gate path - try all HS neighbors and pick shortest
            best_hs_gate_path = None
            best_entrance_hs = None

            for neighbor_id in system_jumps.get(entrance_system.id, []):
                neighbor = systems.get(neighbor_id)
                if neighbor and is_high_sec(neighbor.security):
                    # Check if path exists and is within extra_gates limit
                    gates_needed = count_hs_gates_between(neighbor, end_system, systems, system_jumps)
                    if gates_needed >= 0 and gates_needed <= args.max_extra_gates:
                        hs_gate_path = find_hs_gate_path(neighbor, end_system, systems, system_jumps)
                        if hs_gate_path and (best_hs_gate_path is None or len(hs_gate_path) < len(best_hs_gate_path)):
                            best_hs_gate_path = hs_gate_path
                            best_entrance_hs = neighbor

            if best_hs_gate_path is None:
                continue

            entrance_hs_system = best_entrance_hs
            hs_gate_path = best_hs_gate_path

            # Calculate score for comparison
            # Priority: warp_dist > extra_gates > jumps > wait > ly > lexicographic
            total_jumps = len([s for s in path_to_entrance if not s.is_gate_jump])
            total_wait = sum(s.wait_minutes for s in path_to_entrance)
            total_ly = sum(s.distance_ly for s in path_to_entrance)

            score = (
                warp_dist,
                extra_gates,
                total_jumps,
                math.ceil(total_wait),
                round(total_ly, 2),
                tuple(s.to_system.name for s in path_to_entrance)
            )

            if best_score is None or score < best_score:
                best_score = score
                best_route = path_to_entrance
                best_hs_return = HSReturnPath(
                    entrance_system=entrance_system,
                    entrance_station=entrance_station,
                    hs_gate_path=hs_gate_path,
                    entrance_hs_system=entrance_hs_system
                )

        if best_route is None:
            print(f"Error: No valid path found from '{args.start}' to '{args.end}'", file=sys.stderr)
            sys.exit(1)

        path = best_route
        hs_return = best_hs_return
    else:
        # Non-HS destination - use original logic
        if not can_be_destination(end_system, systems):
            print(f"Error: End station cannot be in Pochven or Zarzakh", file=sys.stderr)
            sys.exit(1)

        path = find_optimal_path(
            start_system,
            end_system,
            systems,
            stations,
            float(args.range),
            args.fuel,
            args.reduction,
            system_jumps=system_jumps if args.cloak else None,
            use_cloak=args.cloak
        )

        if not path:
            print(f"Error: No valid path found from '{args.start}' to '{args.end}'", file=sys.stderr)
            sys.exit(1)

        hs_return = None

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
            from_sys = step.gate_from_system
            to_sys = step.to_system
            from_sec = format_security(from_sys.security)
            to_sec = format_security(to_sys.security)
            print(f"GO: {from_sys.name} ({from_sec}) -> {to_sys.name} ({to_sec})")
        else:
            print("UNDOCK")
            print(f"JUMP {step.distance_ly:.2f} LY: {step.to_system.name} ({format_isotopes(step.isotopes)} isotopes)")
            print(f"DOCK: {step.dock_station.name}")

    # Output HS return path if applicable
    if hs_return:
        # The entrance system is the last jump destination
        # After docking there, we undock and warp to the gate
        print("UNDOCK")
        print(f"WARP TO GATE: {hs_return.entrance_hs_system.name}")

        # Output the HS gate path
        for i in range(len(hs_return.hs_gate_path) - 1):
            from_sys = hs_return.hs_gate_path[i]
            to_sys = hs_return.hs_gate_path[i + 1]
            from_sec = format_security(from_sys.security)
            to_sec = format_security(to_sys.security)
            print(f"GO: {from_sys.name} ({from_sec}) -> {to_sys.name} ({to_sec})")

    print("SUMMARY:")
    print(f"  End Cooldown: {format_time(state.cooldown_minutes)}")
    print(f"  End Fatigue: {format_time(state.fatigue_minutes)}")
    print(f"  Isotopes Used: {format_isotopes(total_isotopes)}")
    print(f"  Time Waiting: {format_time(state.time_waiting_minutes)}")
    print(f"  Total LY: {total_ly:.2f}")


if __name__ == '__main__':
    main()
