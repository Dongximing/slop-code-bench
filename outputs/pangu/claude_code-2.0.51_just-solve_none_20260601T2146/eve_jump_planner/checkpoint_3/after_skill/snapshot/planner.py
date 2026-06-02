"""Jump freighter route planning logic."""

import heapq
import math
from typing import Dict, List, Optional, Tuple, Set

from models import (
    SolarSystem, Station, Jump, RouteState,
    METER_PER_LY, MAX_FATIGUE_HOURS, MAX_COOLDOWN_MINUTES,
    DEFAULT_RANGE, DEFAULT_FUEL, DEFAULT_REDUCTION
)


class JumpFreighterPlanner:
    """Plans jump freighter logistics runs."""

    def __init__(
        self,
        systems: Dict[int, SolarSystem],
        stations: Dict[int, Station],
        jumps: List[Tuple[int, int]],
        max_range: int = DEFAULT_RANGE,
        isotopes_per_jump: int = DEFAULT_FUEL,
        reduction: int = DEFAULT_REDUCTION
    ):
        self.systems = systems
        self.stations = stations
        self.stations_by_name = {s.name.lower(): s for s in stations.values()}
        self.jumps = jumps
        self.max_range = max_range
        self.isotopes_per_jump = isotopes_per_jump
        self.reduction = reduction / 100.0

    def calculate_distance_ly(self, system1: SolarSystem, system2: SolarSystem) -> float:
        return system1.position.distance_to(system2.position) / METER_PER_LY

    def get_effective_distance(self, distance_ly: float) -> float:
        return 0.1 * distance_ly * self.reduction

    def calculate_isotopes(self, distance_ly: float) -> int:
        raw_isotopes = distance_ly * self.isotopes_per_jump
        return math.ceil(raw_isotopes / 1000)

    def calculate_fatigue_cooldown(
        self,
        prev_fatigue_minutes: float,
        prev_cooldown_minutes: float,
        distance_ly: float
    ) -> Tuple[float, float]:
        effective_distance = self.get_effective_distance(distance_ly)

        if prev_fatigue_minutes == 0 and prev_cooldown_minutes == 0:
            new_fatigue = 10 * (1 + effective_distance)
            new_cooldown = 1 + effective_distance
        else:
            new_fatigue = max(prev_fatigue_minutes, 10) * (1 + effective_distance)
            new_cooldown = new_fatigue / 10

        new_fatigue = min(new_fatigue, MAX_FATIGUE_HOURS * 60)
        new_cooldown = min(new_cooldown, MAX_COOLDOWN_MINUTES)

        return new_fatigue, new_cooldown

    @staticmethod
    def is_end_system_valid(system: SolarSystem) -> bool:
        return not system.is_high_sec

    def find_high_sec_entrance(
        self,
        hs_destination: SolarSystem,
        max_extra_gates: int
    ) -> Optional[Tuple[SolarSystem, Station, List[SolarSystem]]]:
        adj: Dict[int, List[int]] = {}
        for from_id, to_id in self.jumps:
            adj.setdefault(from_id, []).append(to_id)
            adj.setdefault(to_id, []).append(from_id)

        stations_by_system: Dict[int, List[Station]] = {}
        for station in self.stations.values():
            stations_by_system.setdefault(station.solar_system_id, []).append(station)

        visited: Set[int] = set()
        queue = [(hs_destination, [hs_destination])]
        valid_entrances = []

        while queue:
            current_system, path = queue.pop(0)

            if current_system.system_id in visited:
                continue
            visited.add(current_system.system_id)

            hs_gate_count = len([s for s in path if s.is_high_sec]) - 1

            if hs_gate_count >= 1 and not current_system.is_high_sec:
                sys_id = current_system.system_id
                if sys_id in stations_by_system:
                    best_station = None
                    min_warp_dist = float('inf')
                    for station in stations_by_system[sys_id]:
                        warp_dist = station.position.distance_to(current_system.position)
                        if warp_dist < min_warp_dist:
                            min_warp_dist = warp_dist
                            best_station = station

                    if best_station:
                        valid_entrances.append((
                            current_system, best_station, path.copy(),
                            hs_gate_count, min_warp_dist
                        ))

            if hs_gate_count >= max_extra_gates:
                continue

            for neighbor_id in adj.get(current_system.system_id, []):
                neighbor = self.systems.get(neighbor_id)
                if neighbor and neighbor.system_id not in visited:
                    new_path = path + [neighbor]
                    new_hs_count = len([s for s in new_path if s.is_high_sec]) - 1

                    if neighbor.is_high_sec and new_hs_count <= max_extra_gates:
                        queue.append((neighbor, new_path))

        if not valid_entrances:
            return None

        valid_entrances.sort(key=lambda x: (x[4], x[3]))
        best_system, best_station, hs_path, _, _ = valid_entrances[0]
        path_to_destination = list(reversed(hs_path))

        return best_system, best_station, path_to_destination

    def find_route_to_entrance(
        self,
        start_station: Station,
        entrance_station: Station,
        entrance_system: SolarSystem
    ) -> Optional[List[Jump]]:
        return self.find_route(start_station, entrance_station)

    def find_hs_path_jumps(
        self,
        from_system: SolarSystem,
        path: List[SolarSystem],
        _end_station: Station
    ) -> List[Jump]:
        jumps = []
        current_system = from_system
        stations_by_system: Dict[int, List[Station]] = {}
        for station in self.stations.values():
            stations_by_system.setdefault(station.solar_system_id, []).append(station)

        for next_system in path:
            distance = self.calculate_distance_ly(current_system, next_system)
            isotopes = self.calculate_isotopes(distance)
            next_station = None
            sys_id = next_system.system_id
            if sys_id in stations_by_system:
                closest = min(stations_by_system[sys_id],
                    key=lambda s: s.position.distance_to(next_system.position))
                next_station = closest

            jump = Jump(distance, next_system, next_station, isotopes)
            jumps.append(jump)
            current_system = next_system

        return jumps

    def find_route(
        self,
        start_station: Station,
        end_station: Station,
        max_extra_gates: int = 0
    ) -> Optional[List[Jump]]:
        start_system = self.systems.get(start_station.solar_system_id)
        end_system = self.systems.get(end_station.solar_system_id)

        if not start_system or not end_system:
            return None

        if end_system.is_high_sec:
            entrance_result = self.find_high_sec_entrance(end_system, max_extra_gates)
            if not entrance_result:
                return None

            entrance_system, entrance_station, hs_path = entrance_result
            entrance_jumps = self.find_route_to_entrance(
                start_station, entrance_station, entrance_system
            )
            if not entrance_jumps:
                return None

            hs_jumps = self.find_hs_path_jumps(entrance_system, hs_path, end_station)
            return entrance_jumps + hs_jumps

        if not self.is_end_system_valid(end_system):
            return None

        open_set = []
        closed_set: Set[int] = set()
        best_for_system: Dict[int, RouteState] = {}

        initial_state = RouteState(
            current_system=start_system, jumps=0,
            time_waiting_minutes=0, fatigue_minutes=0,
            cooldown_minutes=0, total_distance_ly=0.0,
            total_isotopes=0, path=[]
        )
        heapq.heappush(open_set, (0, initial_state))

        max_iterations = 1000000
        iterations = 0

        while open_set and iterations < max_iterations:
            iterations += 1
            _, current_state = heapq.heappop(open_set)

            if current_state.current_system.system_id == end_system.system_id:
                return current_state.path

            system_id = current_state.current_system.system_id
            if system_id in closed_set:
                continue
            closed_set.add(system_id)
            best_for_system[system_id] = current_state

            for neighbor_system in self.systems.values():
                if neighbor_system.system_id in closed_set:
                    continue

                distance = self.calculate_distance_ly(current_state.current_system, neighbor_system)
                if distance > self.max_range:
                    continue

                time_waiting = current_state.time_waiting_minutes
                if current_state.cooldown_minutes > 0:
                    time_waiting += current_state.cooldown_minutes

                new_fatigue, new_cooldown = self.calculate_fatigue_cooldown(
                    current_state.fatigue_minutes,
                    current_state.cooldown_minutes,
                    distance
                )
                isotopes = self.calculate_isotopes(distance)

                jump = Jump(distance, neighbor_system, None, isotopes)
                new_path = current_state.path + [jump]
                new_state = RouteState(
                    current_system=neighbor_system,
                    jumps=current_state.jumps + 1,
                    time_waiting_minutes=time_waiting,
                    fatigue_minutes=new_fatigue,
                    cooldown_minutes=new_cooldown,
                    total_distance_ly=current_state.total_distance_ly + distance,
                    total_isotopes=current_state.total_isotopes + isotopes,
                    path=new_path
                )

                priority = (
                    new_state.jumps,
                    new_state.time_waiting_minutes,
                    new_state.total_distance_ly,
                    new_state.current_system.name
                )
                heapq.heappush(open_set, (priority, new_state))

        return None

    def find_full_route(
        self,
        start_station: Station,
        end_station: Station,
        max_extra_gates: int = 0
    ) -> Tuple[Optional[List[Jump]], int, int, float, float, float]:
        jumps = self.find_route(start_station, end_station, max_extra_gates)

        if not jumps:
            return None, 0, 0, 0.0, 0.0, 0.0

        total_isotopes = sum(j.isotopes_used for j in jumps)
        total_distance = sum(j.distance_ly for j in jumps)

        total_waiting = 0
        fatigue = 0
        cooldown = 0

        for i, jump in enumerate(jumps):
            if i > 0 and cooldown > 0:
                total_waiting += cooldown
            fatigue, cooldown = self.calculate_fatigue_cooldown(
                fatigue, cooldown, jump.distance_ly
            )

        return jumps, total_isotopes, total_waiting, total_distance, fatigue, cooldown
