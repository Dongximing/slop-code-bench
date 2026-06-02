"""3-valued logic implementation."""


class TriValue:
    """3-valued logic value using two masks:
    - known_mask: 1 = bit is known, 0 = bit is unknown (X)
    - value_mask: the value for known bits (meaningful only where known_mask bit is 1)
    """

    def __init__(self, value_mask: int = 0, known_mask: int = None, width: int = 1):
        self.value_mask = value_mask
        self.known_mask = (1 << width) - 1 if known_mask is None else known_mask
        self.width = width

    @property
    def is_fully_known(self) -> bool:
        return self.known_mask == (1 << self.width) - 1

    @property
    def has_unknown(self) -> bool:
        return self.known_mask != (1 << self.width) - 1

    def get_bit(self, bit: int) -> str:
        if not (self.known_mask >> bit) & 1:
            return 'X'
        return '1' if (self.value_mask >> bit) & 1 else '0'

    def to_int(self) -> int:
        if self.has_unknown:
            raise ValueError("Cannot convert TriValue with unknown bits to int")
        return self.value_mask

    def __repr__(self):
        return f"TriValue(value={self.value_mask}, known={self.known_mask}, width={self.width})"

    @staticmethod
    def from_int(value: int, width: int) -> 'TriValue':
        return TriValue(value, (1 << width) - 1, width)

    @staticmethod
    def from_bit(bit: str, width: int = 1) -> 'TriValue':
        if bit.upper() == 'X':
            return TriValue(0, 0, width)
        return TriValue(int(bit), 1, width)

    def copy(self) -> 'TriValue':
        return TriValue(self.value_mask, self.known_mask, self.width)

    def with_width(self, width: int) -> 'TriValue':
        if width == self.width:
            return self.copy()
        clip = (1 << min(width, self.width)) - 1
        return TriValue(self.value_mask & clip, self.known_mask & clip, width)

    @staticmethod
    def not_(a: 'TriValue') -> 'TriValue':
        return TriValue((~a.value_mask) & a.known_mask, a.known_mask, a.width)

    @staticmethod
    def and2(a: 'TriValue', b: 'TriValue') -> 'TriValue':
        a_known0 = a.known_mask & ~a.value_mask
        b_known0 = b.known_mask & ~b.value_mask
        result_known0 = a_known0 | b_known0

        a_known1 = a.known_mask & a.value_mask
        b_known1 = b.known_mask & b.value_mask
        result_known1 = a_known1 & b_known1

        return TriValue(result_known1, result_known0 | result_known1, a.width)

    @staticmethod
    def or2(a: 'TriValue', b: 'TriValue') -> 'TriValue':
        a_known1 = a.known_mask & a.value_mask
        b_known1 = b.known_mask & b.value_mask
        result_known1 = a_known1 | b_known1

        a_known0 = a.known_mask & ~a.value_mask
        b_known0 = b.known_mask & ~b.value_mask
        result_known0 = a_known0 & b_known0

        return TriValue(result_known1, result_known0 | result_known1, a.width)

    @staticmethod
    def xor2(a: 'TriValue', b: 'TriValue') -> 'TriValue':
        both_known = a.known_mask & b.known_mask
        return TriValue((a.value_mask ^ b.value_mask) & both_known, both_known, a.width)

    @staticmethod
    def mux(sel: 'TriValue', a: 'TriValue', b: 'TriValue') -> 'TriValue':
        if sel.known_mask & 1:
            return a.copy() if sel.value_mask & 1 else b.copy()

        both_known = a.known_mask & b.known_mask
        same_value = both_known & ~(a.value_mask ^ b.value_mask)
        return TriValue((a.value_mask | b.value_mask) & same_value, same_value, a.width)

    @staticmethod
    def eq(a: 'TriValue', b: 'TriValue') -> 'TriValue':
        mask = (1 << a.width) - 1
        both_known = a.known_mask & b.known_mask

        diff_bits = both_known & (a.value_mask ^ b.value_mask)
        if diff_bits:
            return TriValue(0, 1, 1)

        if both_known == mask and not (a.value_mask ^ b.value_mask):
            return TriValue(1, 1, 1)

        return TriValue(0, 0, 1)

    @staticmethod
    def reduce_and(a: 'TriValue') -> 'TriValue':
        mask = (1 << a.width) - 1
        if a.known_mask & ~a.value_mask:
            return TriValue(0, 1, 1)
        if a.known_mask == mask and (a.value_mask & mask) == mask:
            return TriValue(1, 1, 1)
        return TriValue(0, 0, 1)

    @staticmethod
    def reduce_or(a: 'TriValue') -> 'TriValue':
        mask = (1 << a.width) - 1
        if a.known_mask & a.value_mask:
            return TriValue(1, 1, 1)
        if a.known_mask == mask and (a.value_mask & mask) == 0:
            return TriValue(0, 1, 1)
        return TriValue(0, 0, 1)

    @staticmethod
    def reduce_xor(a: 'TriValue') -> 'TriValue':
        mask = (1 << a.width) - 1
        if a.known_mask != mask:
            return TriValue(0, 0, 1)
        return TriValue(bin(a.value_mask & mask).count('1') % 2, 1, 1)


def format_trivalue(value: TriValue) -> str:
    if value.width == 1:
        if not (value.known_mask & 1):
            return 'X'
        return '1' if (value.value_mask & 1) else '0'
    return '0b' + ''.join(value.get_bit(i) for i in range(value.width - 1, -1, -1))


def format_value(value: int, width: int, radix: str) -> str:
    if width == 1:
        return str(value & 1)
    if radix == 'bin':
        return f"0b{bin(value)[2:].zfill(width)}"
    if radix == 'hex':
        hex_width = (width + 3) // 4
        return f"0x{hex(value)[2:].zfill(hex_width)}"
    return str(value)
