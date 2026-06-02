import json

with open('test_contracts.jsonl', 'r') as f:
    contracts = []
    for i, line in enumerate(f, start=1):
        line = line.strip()
        if line:
            contract = json.loads(line)
            contract['id'] = i
            contracts.append(contract)
            print(f"Contract {i}: {contract['reward']} ISK, {contract['m3']} m3")
