#!/usr/bin/env python3
"""Jump Freighter logistics planning tool."""

import argparse
import sys

from models import DEFAULT_RANGE, DEFAULT_FUEL, DEFAULT_REDUCTION
from sde_loader import load_systems, load_stations, load_jumps
from planner import JumpFreighterPlanner


def find_station(stations_by_name, name: str):
    name_lower = name.lower().strip()
    if name_lower in stations_by_name:
        return stations_by_name[name_lower]
    for station_name, station in stations_by_name.items():
        if name_lower in station_name or station_name in name_lower:
            return station
    return None


def format_time(minutes: float) -> str:
    return f"{int(minutes) // 60:02d}:{int(minutes) % 60:02d}"


def format_isotopes(isotopes_k: int) -> str:
    return f"{isotopes_k}K"


def print_route_summary(total_isotopes, total_waiting, total_distance,
                        end_cooldown, end_fatigue):
    print("SUMMARY:")
    print(f"  End Cooldown: {format_time(end_cooldown)}")
    print(f"  End Fatigue: {format_time(end_fatigue)}")
    print(f"  Isotopes Used: {total_isotopes:,}K")
    print(f"  Time Waiting: {format_time(total_waiting)}")
    print(f"  Total LY: {total_distance:.2f}")


def output_jump(jump, current_system, stations_by_name, end_station,
                can_cloak=False, is_last_jump=False, is_last_hs_section=False):
    sys_id = jump.destination_system.system_id
    dest_station = next(
        (s for s in stations_by_name.values()
         if s.solar_system_id == sys_id), None)
    jump.destination_station = dest_station

    if can_cloak:
        sf, st = round(current_system.security, 1), round(jump.destination_system.security, 1)
        print(f"GO: {current_system.name} ({sf}) -> {jump.destination_system.name} ({st})")
        if dest_station:
            print(f"DOCK: {dest_station.name}")
        return jump.destination_system, True

    print(f"JUMP {jump.distance_ly:.2f} LY: {jump.destination_system.name} "
          f"({format_isotopes(jump.isotopes_used)} isotopes)")

    if is_last_hs_section:
        dock_name = end_station.name
    elif is_last_jump:
        dock_name = end_station.name
    elif dest_station:
        dock_name = dest_station.name
    else:
        dock_name = None

    if dock_name:
        print(f"DOCK: {dock_name}")

    return jump.destination_system, False


def print_route(jumps, stations_by_name, end_station, start_system,
                args, end_cooldown, end_fatigue, total_isotopes,
                total_waiting, total_distance):
    cloak_used = False
    cur = start_system

    is_hs_dest = (end_station.solar_system
                  and end_station.solar_system.is_high_sec)

    if is_hs_dest:
        hs_idx = next(
            (i for i, j in enumerate(jumps) if j.destination_system.is_high_sec),
            None
        )

        if hs_idx is not None and hs_idx > 0:
            non_hs, hs = jumps[:hs_idx], jumps[hs_idx:]

            for i, jump in enumerate(non_hs):
                can_cloak = args.cloak and not cloak_used and i < len(non_hs) - 1
                cur, used = output_jump(
                    jump, cur, stations_by_name, end_station, can_cloak,
                    is_last_hs_section=bool(hs)
                )
                if used:
                    cloak_used = True

            for i, jump in enumerate(hs):
                is_last = i == len(hs) - 1
                sf, st = round(cur.security, 1), round(jump.destination_system.security, 1)
                print(f"GO: {cur.name} ({sf}) -> {jump.destination_system.name} ({st})")
                if is_last:
                    print(f"DOCK: {end_station.name}")
                else:
                    sys_id = jump.destination_system.system_id
                    ds = next(
                        (s for s in stations_by_name.values()
                         if s.solar_system_id == sys_id), None)
                    jump.destination_station = ds
                    if ds:
                        print(f"DOCK: {ds.name}")
                    print("UNDOCK")
                cur = jump.destination_system
        else:
            for i, jump in enumerate(jumps):
                can_cloak = args.cloak and not cloak_used and i < len(jumps) - 1
                cur, used = output_jump(
                    jump, cur, stations_by_name, end_station,
                    can_cloak, is_last_jump=(i == len(jumps) - 1)
                )
                if used:
                    cloak_used = True
    else:
        for i, jump in enumerate(jumps):
            can_cloak = args.cloak and not cloak_used and i < len(jumps) - 1
            cur, used = output_jump(
                jump, cur, stations_by_name, end_station,
                can_cloak, is_last_jump=(i == len(jumps) - 1)
            )
            if used:
                cloak_used = True

    print_route_summary(total_isotopes, total_waiting, total_distance,
                        end_cooldown, end_fatigue)


def main():
    parser = argparse.ArgumentParser(description="Jump Freighter logistics planning tool")
    parser.add_argument('--start', required=True, help='Start station name')
    parser.add_argument('--end', required=True, help='End station name')
    parser.add_argument('--sde', required=True, help='Path to SDE directory')
    parser.add_argument('--range', dest='max_range', type=int, default=DEFAULT_RANGE,
                        choices=range(5, 11),
                        help=f'Max LY range for a jump (int in [5,10], defaults to {DEFAULT_RANGE})')
    parser.add_argument('--fuel', type=int, default=DEFAULT_FUEL, choices=range(1, 10001),
                        help=f'Isotopes per jump (int in [1,10000], default is {DEFAULT_FUEL})')
    parser.add_argument('--reduction', type=int, default=DEFAULT_REDUCTION, choices=range(0, 101),
                        help=f'Effective jump distance reduction (int in [0,100], default is {DEFAULT_REDUCTION})')
    parser.add_argument('--cloak', action='store_true', help='Enable cloak gate jump behavior')
    parser.add_argument('--max-extra-gates', '-gates', type=int, default=0,
                        help='Maximum extra high security systems willing to take for closer station (default: 0)')

    args = parser.parse_args()

    print("Loading SDE data...", file=sys.stderr)
    systems = load_systems(args.sde)
    stations = load_stations(args.sde, systems)
    jumps = load_jumps(args.sde)
    print(f"Loaded {len(systems)} systems, {len(stations)} stations, {len(jumps)} jump connections",
          file=sys.stderr)

    stations_by_name = {s.name.lower(): s for s in stations.values()}
    start_station = find_station(stations_by_name, args.start)
    end_station = find_station(stations_by_name, args.end)

    if not start_station:
        print(f"Error: Start station '{args.start}' not found", file=sys.stderr)
        sys.exit(1)
    if not end_station:
        print(f"Error: End station '{args.end}' not found", file=sys.stderr)
        sys.exit(1)

    planner = JumpFreighterPlanner(
        systems=systems, stations=stations, jumps=jumps,
        max_range=args.max_range, isotopes_per_jump=args.fuel,
        reduction=args.reduction
    )

    result = planner.find_full_route(start_station, end_station, args.max_extra_gates)
    jumps_found, total_isotopes, total_waiting, total_distance, end_fatigue, end_cooldown = result

    if jumps_found is None:
        print(f"Error: No route found from '{args.start}' to '{args.end}'", file=sys.stderr)
        sys.exit(1)

    print(f"START: {start_station.name}")
    print("UNDOCK")

    start_system = systems[start_station.solar_system_id]
    print_route(jumps_found, stations_by_name, end_station, start_system,
                args, end_cooldown, end_fatigue, total_isotopes,
                total_waiting, total_distance)


if __name__ == '__main__':
    main()
