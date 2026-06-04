import re

with open('test_macros_cycle.tex', 'r') as f:
    text = f.read()

print('Raw text:')
print(repr(text[:100]))

begin_match = re.search(r'\\begin\{document\}', text)
print('begin_match:', begin_match)

preamble = text[:begin_match.start()]
print('Preamble:')
print(repr(preamble))

# Try matching the def
for match in re.finditer(r'\\def\s*(\\[a-zA-Z]+)\s*\{([^}]*)\}', preamble):
    print('Match:', match.group(0))
    print('  cmd:', match.group(1))
    print('  replacement:', repr(match.group(2)))
