"""QR codes for call links, so a phone can open a link shown on a computer.

A small encoder (byte mode, error correction level M, versions 1 to 40)
after Project Nayuki's QR Code generator library (MIT License, Copyright
(c) Project Nayuki, https://www.nayuki.io/page/qr-code-generator-library). Sleeptalk
needs nothing else from a QR library, and the settings page must work
offline, so it is kept here rather than added as a dependency.
"""

from __future__ import annotations

# Level M: error correction codewords per block, and blocks, by version.
_ECC_PER_BLOCK = (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
                  26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28)
_BLOCKS = (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16,
           17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49)
_FORMAT_M = 0  # level M's two format bits
_QUIET = 4  # modules of light border a scanner needs


def qr_matrix(text: str) -> list[list[bool]]:
    """The modules of ``text``'s QR code, rows of booleans (True is dark)."""
    data = text.encode("utf-8")
    for version in range(1, 41):
        count_bits = 8 if version < 10 else 16
        if 4 + count_bits + 8 * len(data) <= _data_codewords(version) * 8:
            break
    else:
        raise ValueError("too long for a QR code")
    return _Symbol(version, _codewords(data, version, count_bits)).modules


def qr_svg(text: str) -> str:
    """``text`` as a QR code in SVG: dark modules on white, with its border."""
    modules = qr_matrix(text)
    size = len(modules) + 2 * _QUIET
    path = "".join(
        f"M{x + _QUIET},{y + _QUIET}h1v1h-1z"
        for y, row in enumerate(modules) for x, dark in enumerate(row) if dark
    )
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges">'
            f'<rect width="{size}" height="{size}" fill="#fff"/><path d="{path}" fill="#000"/></svg>')


# -- codewords ----------------------------------------------------------------


def _raw_modules(version: int) -> int:
    """Modules left for data and error correction once the patterns are drawn."""
    result = (16 * version + 128) * version + 64
    if version >= 2:
        align = version // 7 + 2
        result -= (25 * align - 10) * align - 55
        if version >= 7:
            result -= 36
    return result


def _data_codewords(version: int) -> int:
    return _raw_modules(version) // 8 - _ECC_PER_BLOCK[version] * _BLOCKS[version]


def _codewords(data: bytes, version: int, count_bits: int) -> list[int]:
    bits: list[int] = []

    def put(value: int, length: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(length)))

    put(0b0100, 4)  # byte mode
    put(len(data), count_bits)
    for byte in data:
        put(byte, 8)
    capacity = _data_codewords(version) * 8
    put(0, min(4, capacity - len(bits)))
    put(0, -len(bits) % 8)
    pad = 0xEC
    while len(bits) < capacity:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    return _with_error_correction(words, version)


def _with_error_correction(data: list[int], version: int) -> list[int]:
    blocks_n, ecc_len = _BLOCKS[version], _ECC_PER_BLOCK[version]
    raw = _raw_modules(version) // 8
    short_n = blocks_n - raw % blocks_n
    short_len = raw // blocks_n
    divisor = _rs_divisor(ecc_len)
    blocks: list[list[int]] = []
    k = 0
    for i in range(blocks_n):
        chunk = data[k:k + short_len - ecc_len + (0 if i < short_n else 1)]
        k += len(chunk)
        ecc = _rs_remainder(chunk, divisor)
        if i < short_n:
            chunk = [*chunk, 0]
        blocks.append(chunk + ecc)
    return [block[i] for i in range(len(blocks[0])) for j, block in enumerate(blocks)
            if i != short_len - ecc_len or j >= short_n]


def _rs_multiply(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _rs_multiply(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _rs_multiply(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coefficient in enumerate(divisor):
            result[i] ^= _rs_multiply(coefficient, factor)
    return result


# -- the symbol ---------------------------------------------------------------


_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


class _Symbol:
    def __init__(self, version: int, codewords: list[int]) -> None:
        self.version = version
        self.size = version * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]
        self._draw_patterns()
        self._draw_codewords(codewords)
        best = min(range(8), key=self._penalty_with)
        self._apply_mask(best)
        self._draw_format(best)

    def _set(self, x: int, y: int, dark: bool) -> None:
        self.modules[y][x] = dark
        self.function[y][x] = True

    def _draw_patterns(self) -> None:
        size = self.size
        for i in range(size):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        self._set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        positions = self._alignment_positions()
        last = len(positions) - 1
        for i, ax in enumerate(positions):
            for j, ay in enumerate(positions):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue  # the finder patterns' corners
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format(0)  # reserves the area; drawn for real after masking
        if self.version >= 7:
            remainder = self.version
            for _ in range(12):
                remainder = (remainder << 1) ^ ((remainder >> 11) * 0x1F25)
            bits = self.version << 12 | remainder
            for i in range(18):
                dark = (bits >> i) & 1 == 1
                a, b = size - 11 + i % 3, i // 3
                self._set(a, b, dark)
                self._set(b, a, dark)

    def _alignment_positions(self) -> list[int]:
        if self.version == 1:
            return []
        count = self.version // 7 + 2
        step = (self.version * 8 + count * 3 + 5) // (count * 4 - 4) * 2
        return [6, *reversed([self.size - 7 - i * step for i in range(count - 1)])]

    def _draw_format(self, mask: int) -> None:
        data = _FORMAT_M << 3 | mask
        remainder = data
        for _ in range(10):
            remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
        bits = (data << 10 | remainder) ^ 0x5412

        def bit(i: int) -> bool:
            return (bits >> i) & 1 == 1

        size = self.size
        for i in range(6):
            self._set(8, i, bit(i))
        self._set(8, 7, bit(6))
        self._set(8, 8, bit(7))
        self._set(7, 8, bit(8))
        for i in range(9, 15):
            self._set(14 - i, 8, bit(i))
        for i in range(8):
            self._set(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._set(8, size - 15 + i, bit(i))
        self._set(8, size - 8, True)

    def _draw_codewords(self, codewords: list[int]) -> None:
        size = self.size
        total = len(codewords) * 8
        i = 0
        for right in range(size - 1, 0, -2):
            if right <= 6:
                right -= 1  # skip the vertical timing column
            upward = (right + 1) & 2 == 0
            for vert in range(size):
                y = size - 1 - vert if upward else vert
                for x in (right, right - 1):
                    if not self.function[y][x] and i < total:
                        self.modules[y][x] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1

    def _apply_mask(self, mask: int) -> None:
        test = _MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and test(x, y):
                    self.modules[y][x] = not self.modules[y][x]

    def _penalty_with(self, mask: int) -> int:
        self._apply_mask(mask)
        self._draw_format(mask)
        score = _penalty(self.modules)
        self._apply_mask(mask)  # a mask undoes itself
        return score


def _penalty(modules: list[list[bool]]) -> int:
    """How hard the symbol is to scan: long runs, 2x2 blocks, finder-like
    stretches and an uneven dark share all count against it."""
    size = len(modules)
    score = 0
    lines = ["".join("1" if dark else "0" for dark in row) for row in modules]
    lines += ["".join("1" if modules[y][x] else "0" for y in range(size)) for x in range(size)]
    for line in lines:
        run = 1
        for a, b in zip(line, line[1:]):
            if a == b:
                run += 1
            else:
                score += run - 2 if run >= 5 else 0
                run = 1
        score += run - 2 if run >= 5 else 0
        padded = f"0000{line}0000"
        score += 40 * (padded.count("10111010000") + padded.count("00001011101"))
    for y in range(size - 1):
        for x in range(size - 1):
            if modules[y][x] == modules[y][x + 1] == modules[y + 1][x] == modules[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, modules))
    score += 10 * (abs(dark * 20 - size * size * 10) // (size * size))
    return score
