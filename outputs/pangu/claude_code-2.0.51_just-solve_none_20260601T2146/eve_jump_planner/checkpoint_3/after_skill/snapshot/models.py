"""Data models for jump freighter planner."""

from dataclasses import dataclass
import math
from typing import List

METER_PER_LY = 9_460_730_472_580_800.0
MAX_FATIGUE_HOURS = 5
MAX_COOLDOWN_MINUTES = 30
DEFAULT_RANGE = 10
DEFAULT_FUEL = 10_000
DEFAULT_REDUCTION = 90


@dataclass
class Vector3:
    """3D vector for position."""
    x: float
    y: float
    z: float

    def distance_to(self, other: 'Vector3') -> float:
        return math.sqrt((self.x - other.x)**2 + (self.y - other.y)**2 + (self.z - other.z)**2)


@dataclass
class SolarSystem:
    """Represents a solar system."""
    system_id: int
    name: str
    position: Vector3
    security: float
    constellation_id: int
    region_id: int
    security_class: str = None

    @property
    def is_high_sec(self) -> bool:
        return round(self.security, 1) >= 0.5

    def __hash__(self):
        return hash(self.system_id)

    def __eq__(self, other):
        return isinstance(other, SolarSystem) and self.system_id == other.system_id


@dataclass
class Station:
    """Represents a station."""
    station_id: int
    name: str
    position: Vector3
    solar_system_id: int
    solar_system_name: str
    security: float

    def __hash__(self):
        return hash(self.station_id)

    def __eq__(self, other):
        return isinstance(other, Station) and self.station_id == other.station_id


@dataclass
class Jump:
    """Represents a single jump in the route."""
    distance_ly: float
    destination_system: SolarSystem
    destination_station: Station | None
    isotopes_used: int

    def __repr__(self):
        return f"JUMP {self.distance_ly:.2f} LY: {self.destination_system.name} ({self.isotopes_used}K isotopes)"


@dataclass
class RouteState:
    """State for A* pathfinding."""
    current_system: SolarSystem
    jumps: int
    time_waiting_minutes: float
    fatigue_minutes: float
    cooldown_minutes: float
    total_distance_ly: float
    total_isotopes: int
    path: List[Jump]

    def __lt__(self, other):
        return (self.jumps, self.time_waiting_minutes,
                self.total_distance_ly, self.current_system.name) < \
               (other.jumps, other.time_waiting_minutes,
                other.total_distance_ly, other.current_system.name)
