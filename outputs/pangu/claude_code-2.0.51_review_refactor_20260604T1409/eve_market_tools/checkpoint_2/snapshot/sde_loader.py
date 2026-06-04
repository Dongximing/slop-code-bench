"""
SDE (Static Data Export) loader module.
Provides name/ID resolution and metadata from EVE Online SDE files.
"""

import csv
import bz2
from pathlib import Path
from collections import defaultdict
from typing import Optional

class SDELoader:
    """Loads and indexes SDE data for quick lookups."""

    # Solar system IDs for main hubs
    HUB_SYSTEMS = {
        "jita": 30000142,    # Jita IV
        "amarr": 30002187,   # Amarr VIII (Oris)
        "dodixie": 30002659, # Dodixie IX
        "rens": 30002510,    # Rens VI
        "hek": 30002053,     # Hek VIII
    }

    def __init__(self, sde_path: str):
        """Initialize SDE loader with path to SDE directory."""
        self.sde_path = Path(sde_path)

        # Type mappings
        self.type_id_to_name: dict[int, str] = {}
        self.type_name_to_id: dict[str, int] = {}
        self.type_id_to_volume: dict[int, float] = {}

        # Region mappings
        self.region_id_to_name: dict[int, str] = {}
        self.region_name_to_id: dict[str, int] = {}

        # Station mappings
        self.location_id_to_station_name: dict[int, str] = {}
        self.location_id_to_type: dict[int, str] = {}  # "Station" or "Structure"
        self.station_id_to_solar_system: dict[int, int] = {}
        self.solar_system_to_region: dict[int, int] = {}

        # Hub station IDs (will be populated after loading)
        self.hub_location_ids: dict[str, set[int]] = defaultdict(set)

    def load_all(self):
        """Load all SDE data files."""
        self._load_inv_types()
        self._load_map_regions()
        self._load_sta_stations()
        self._load_map_solar_systems()
        self._identify_hubs()

    def _load_inv_types(self):
        """Load item types from invTypes.csv."""
        filepath = self.sde_path / "invTypes.csv.bz2"
        if not filepath.exists():
            raise FileNotFoundError(f"SDE file not found: {filepath}")

        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    type_id = int(row['typeID'])
                    type_name = row['typeName']
                    volume = float(row['volume']) if row['volume'] and row['volume'] != '0E-10' else 0.0

                    self.type_id_to_name[type_id] = type_name
                    self.type_name_to_id[type_name] = type_id
                    self.type_id_to_volume[type_id] = volume
                except (KeyError, ValueError) as e:
                    # Skip malformed rows
                    continue

    def _load_map_regions(self):
        """Load regions from mapRegions.csv."""
        filepath = self.sde_path / "mapRegions.csv.bz2"

        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    region_id = int(row['regionID'])
                    region_name = row['regionName']

                    self.region_id_to_name[region_id] = region_name
                    self.region_name_to_id[region_name] = region_id
                except (KeyError, ValueError) as e:
                    continue

    def _load_sta_stations(self):
        """Load stations from staStations.csv."""
        filepath = self.sde_path / "staStations.csv.bz2"

        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    location_id = int(row['stationID'])
                    name = row['stationName']
                    station_type = row.get('type', 'Station')
                    solar_system = int(row['solarSystemID'])

                    # Structure types are identified by the type column being "Structure"
                    self.location_id_to_type[location_id] = "Structure" if station_type == 'Structure' else "Station"

                    self.location_id_to_station_name[location_id] = name
                    self.station_id_to_solar_system[location_id] = solar_system
                except (KeyError, ValueError) as e:
                    continue

    def _load_map_solar_systems(self):
        """Load solar systems from mapSolarSystems.csv."""
        filepath = self.sde_path / "mapSolarSystems.csv.bz2"

        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    solar_system_id = int(row['solarSystemID'])
                    region_id = int(row['regionID'])
                    self.solar_system_to_region[solar_system_id] = region_id
                except (KeyError, ValueError) as e:
                    continue

    def _identify_hubs(self):
        """Identify main hub stations by their solar system."""
        for hub_key, system_id in self.HUB_SYSTEMS.items():
            for location_id, solar_system in self.station_id_to_solar_system.items():
                if solar_system == system_id:
                    self.hub_location_ids[hub_key].add(location_id)

    def get_type_name(self, type_id: int) -> Optional[str]:
        """Get type name from ID."""
        return self.type_id_to_name.get(type_id)

    def get_type_id(self, type_name: str) -> Optional[int]:
        """Get type ID from name."""
        return self.type_name_to_id.get(type_name)

    def get_region_name(self, region_id: int) -> Optional[str]:
        """Get region name from ID."""
        return self.region_id_to_name.get(region_id)

    def get_region_id(self, region_name: str) -> Optional[int]:
        """Get region ID from name."""
        return self.region_name_to_id.get(region_name)

    def get_station_name(self, location_id: int) -> Optional[str]:
        """Get station name from location ID."""
        return self.location_id_to_station_name.get(location_id)

    def get_location_type(self, location_id: int) -> Optional[str]:
        """Get location type (Station/Structure) from location ID."""
        return self.location_id_to_type.get(location_id)

    def get_region_for_station(self, location_id: int) -> Optional[int]:
        """Get region ID for a station's solar system."""
        solar_system = self.station_id_to_solar_system.get(location_id)
        if solar_system:
            return self.solar_system_to_region.get(solar_system)
        return None

    def is_hub_station(self, location_id: int) -> Optional[str]:
        """Check if a location is one of the main hubs. Returns hub key or None."""
        for hub_key, hub_ids in self.hub_location_ids.items():
            if location_id in hub_ids:
                return hub_key
        return None

    def get_hub_location_ids(self, hub_key: str) -> set[int]:
        """Get all location IDs for a hub."""
        return self.hub_location_ids.get(hub_key, set())

    def get_all_hubs(self) -> dict[str, set[int]]:
        """Get all hub location IDs."""
        return dict(self.hub_location_ids)
