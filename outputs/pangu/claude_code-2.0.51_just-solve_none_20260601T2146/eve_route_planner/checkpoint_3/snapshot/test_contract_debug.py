import sys
sys.path.insert(0, '.')

from hauling import *

# Load test data
config = load_yaml('test_contracts.yaml')
systems, jumps, stations, _, _ = load_sde_data('sde')

start_type, start_sys_data, _ = identify_location('Jita', systems, stations)
start_sys_id = start_sys_data['system_id'] if start_type == 'station' else start_sys_data['id']

contracts = load_contracts('test_contracts.jsonl')

print(f"Start system: {systems[start_sys_id]['name']} (ID: {start_sys_id})")
print(f"Contracts: {len(contracts)}")
print()

for ship_name in config['ships']:
    calc = HaulingCalculator(config, ship_name, systems, jumps, stations, config['times'])
    ehp = get_ship_ehp(ship_name, config)
    ship_type = config['ships'][ship_name]['type']
    
    print(f"\n{'='*60}")
    print(f"Ship: {ship_name} (Type: {ship_type})")
    print(f"EHP: {ehp}")
    print(f"Cargo Size: {calc.cargo_size}")
    print(f"{'='*60}")
    
    for contract in contracts:
        params = calculate_contract_parameters(contract, systems, jumps, calc, start_sys_id)
        if params is None:
            print(f"  Contract {contract['id']}: No valid route!")
            continue
            
        isk_per_jump = params['reward'] / params['total_jumps'] if params['total_jumps'] > 0 else 0
        isk_per_ehp = params['reward'] / ehp if ehp > 0 else float('inf')
        
        min_isk = config.get('min_isk_per_jump', 0)
        max_isk_ehp = config.get('max_isk_per_ehp', float('inf'))
        
        # Check constraints
        cargo_ok = params['m3'] <= calc.cargo_size
        isk_jump_ok = isk_per_jump >= min_isk
        isk_ehp_ok = isk_per_ehp <= max_isk_ehp or ship_type == 'Blockade Runner'
        
        status = "PASS" if (cargo_ok and isk_jump_ok and isk_ehp_ok) else "FAIL"
        
        print(f"  Contract {contract['id']}: reward={params['reward']}, m3={params['m3']}")
        print(f"    total_jumps={params['total_jumps']}, isk/jump={isk_per_jump:.2f}")
        print(f"    isk/ehp={isk_per_ehp:.4f}")
        print(f"    cargo_ok={cargo_ok}, min_isk_ok={isk_jump_ok}, max_ehp_ok={isk_ehp_ok}")
        print(f"    STATUS: {status}")
