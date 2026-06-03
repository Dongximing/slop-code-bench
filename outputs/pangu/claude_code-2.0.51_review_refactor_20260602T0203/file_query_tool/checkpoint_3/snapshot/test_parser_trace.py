#!/usr/bin/env python3
import sys
sys.path.insert(0, '/workspace')

class DebugParser:
    def __init__(self, sql: str):
        self.sql = sql.strip()
        self.pos = 0
        self.length = len(self.sql)
        print(f"SQL: '{self.sql}'")
        print(f"Length: {self.length}")

    def skip_whitespace(self):
        """Skip whitespace characters."""
        start = self.pos
        while self.pos < self.length and self.sql[self.pos].isspace():
            self.pos += 1
        if self.pos > start:
            print(f"  Skipped whitespace, pos now {self.pos}")

    def peek(self, chars: str = None) -> str:
        """Peek at the current character or characters."""
        self.skip_whitespace()
        if chars:
            if self.sql.startswith(chars, self.pos):
                return chars
            return None
        if self.pos < self.length:
            return self.sql[self.pos]
        return None

    def consume(self, expected: str = None) -> str:
        """Consume the expected string or current character."""
        self.skip_whitespace()
        if expected:
            if self.sql.startswith(expected, self.pos):
                self.pos += len(expected)
                print(f"  Consumed '{expected}', pos now {self.pos}")
                return expected
            raise ValueError(f"Expected '{expected}' at position {self.pos}")
        if self.pos < self.length:
            char = self.sql[self.pos]
            self.pos += 1
            print(f"  Consumed char '{char}', pos now {self.pos}")
            return char
        raise ValueError(f"Unexpected end of SQL")

    def parse_select_list(self):
        """Parse the SELECT clause."""
        self.consume("SELECT")
        items = []
        iteration = 0
        while True:
            iteration += 1
            print(f"\n  SELECT loop iteration {iteration}, pos={self.pos}")
            self.skip_whitespace()

            # Check if we've hit another clause
            if self.peek() == "FROM":
                print(f"    Detected FROM keyword, breaking")
                break

            # Parse expression (handle *)
            expr = self.parse_expression_debug()

            # Parse alias (optional)
            alias = None
            self.skip_whitespace()
            if self.peek("AS"):
                self.consume("AS")
                self.skip_whitespace()
                alias = self.parse_identifier()
            elif self.pos < self.length and self.sql[self.pos].isalpha():
                alias_start = self.pos
                potential_alias = self.parse_identifier()
                if potential_alias.upper() in ['FROM', 'WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET', 'JOIN', 'ON', 'AND', 'OR', 'NOT']:
                    expr += " " + potential_alias
                    self.pos = alias_start
                else:
                    alias = potential_alias

            items.append({'expr': expr, 'alias': alias})

            self.skip_whitespace()
            if self.peek() == ',':
                self.consume(',')
            else:
                print(f"    No comma, breaking")
                break

        return items

    def parse_expression_debug(self):
        """Parse an expression with debug output."""
        self.skip_whitespace()
        print(f"    parse_expression_debug at pos={self.pos}, char='{self.peek()}'")

        # Handle wildcard (*)
        if self.peek() == '*':
            self.consume('*')
            print(f"    Returned '*'")
            return '*'

        # Handle identifiers
        ident = self.parse_identifier()
        print(f"    Parsed identifier: '{ident}'")

        # Check for table-qualified column
        if self.peek('.'):
            self.consume('.')
            ident = f"{ident}.{self.parse_identifier()}"

        return ident

    def parse_identifier(self) -> str:
        """Parse an identifier."""
        self.skip_whitespace()
        start = self.pos
        while self.pos < self.length and (self.sql[self.pos].isalnum() or self.sql[self.pos] == '_'):
            self.pos += 1
        if start == self.pos:
            raise ValueError(f"Expected identifier at position {start}")
        result = self.sql[start:self.pos]
        print(f"    parse_identifier: '{result}'")
        return result

    def parse(self):
        """Parse the complete statement."""
        result = {'select': self.parse_select_list()}
        return result


# Test
sql = "SELECT * FROM users LIMIT 5"
parser = DebugParser(sql)
result = parser.parse()
print(f"\nFinal result: {result}")
