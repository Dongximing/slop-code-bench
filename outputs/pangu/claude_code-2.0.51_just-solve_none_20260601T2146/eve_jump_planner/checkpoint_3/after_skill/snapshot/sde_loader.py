"""SDE data loader module."""

import bz2
import csv
from typing import Dict, List, Tuple

from models import SolarSystem, Station, Vector3


def load_systems(sde_path: str) -> Dict[int, SolarSystem]:
    """Load solar systems from mapSolarSystems.csv.bz2."""
    systems = {}
    sde_path = sde_path.rstrip('/')
    filename = f"{sde_path}/mapSolarSystems.csv.bz2"

    with bz2.open(filename, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            system_id = int(row['solarSystemID'])
            systems[system_id] = SolarSystem(
                system_id=system_id,
                name=row['solarSystemName'],
                position=Vector3(
                    float(row['x']),
                    float(row['y']),
                    float(row['z'])
                ),
                security=float(row['security']),
                constellation_id=int(row['constellationID']),
                region_id=int(row['regionID']),
                security_class=row.get('securityClass')
            )
    return systems


def load_stations(sde_path: str, systems: Dict[int, SolarSystem]) -> Dict[int, Station]:
    """Load stations from staStations.csv.bz2."""
    stations = {}
    sde_path = sde_path.rstrip('/')
    filename = f"{sde_path}/staStations.csv.bz2"

    with bz2.open(filename, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            station_id = int(row['stationID'])
            system_id = int(row['solarSystemID'])
            system = systems.get(system_id)
            if system:
                stations[station_id] = Station(
                    station_id=station_id,
                    name=row['stationName'],
                    position=Vector3(
                        float(row['x']),
                        float(row['y']),
                        float(row['z'])
                    ),
                    solar_system_id=system_id,
                    solar_system_name=system.name,
                    security=float(row['security'])
                )
    return stations


def load_jumps(sde_path: str) -> List[Tuple[int, int]]:
    """Load jump connections from mapSolarSystemJumps.csv.bz2."""
    jumps = []
    sde_path = sde_path.rstrip('/')
    filename = f"{sde_path}/mapSolarSystemJumps.csv.bz2"

    with bz2.open(filename, 'rt', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            from_id = int(row['fromSolarSystemID'])
            to_id = int(row['toSolarSystemID'])
            jumps.append((from_id, to_id))
    return jumps
