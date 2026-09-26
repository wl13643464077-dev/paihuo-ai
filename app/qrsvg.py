"""付款二维码 → SVG。

优先用 requirements 里的 ``qrcode`` 库；运行环境没装时退回下面这个无依赖的
小型编码器(字节模式、纠错等级 M、版本 1-10，足够装下微信 code_url)。
两者都失败时返回空字符串，由前端显示“用微信扫一扫”+链接文本兜底。
"""
from __future__ import annotations

import logging

log = logging.getLogger("qrsvg")

# 纠错等级 M：每块纠错码字数、块数(版本 1-10)。
_ECC_PER_BLOCK_M = (None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26)
_NUM_BLOCKS_M = (None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5)
_MAX_VERSION = 10
_QUIET = 4


def _gf_mul(x: int, y: int) -> int:
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
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _raw_modules(version: int) -> int:
    result = (16 * version + 128) * version + 64
    if version >= 2:
        count = version // 7 + 2
        result -= (25 * count - 10) * count - 55
        if version >= 7:
            result -= 36
    return result


def _data_codewords(version: int) -> int:
    return _raw_modules(version) // 8 - _ECC_PER_BLOCK_M[version] * _NUM_BLOCKS_M[version]


def _alignment_positions(version: int, size: int) -> list[int]:
    if version == 1:
        return []
    count = version // 7 + 2
    step = (version * 8 + count * 3 + 5) // (count * 4 - 4) * 2
    result = [size - 7 - i * step for i in range(count - 1)] + [6]
    return list(reversed(result))


def _encode_data(payload: bytes, version: int) -> list[int]:
    bits: list[int] = []

    def put(value: int, length: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(length)))

    put(0b0100, 4)  # 字节模式
    put(len(payload), 8 if version <= 9 else 16)
    for byte in payload:
        put(byte, 8)
    capacity = _data_codewords(version) * 8
    put(0, min(4, capacity - len(bits)))
    put(0, (-len(bits)) % 8)
    pad = 0xEC
    while len(bits) < capacity:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    return [
        int("".join(str(bit) for bit in bits[i:i + 8]), 2)
        for i in range(0, len(bits), 8)
    ]


def _interleave(data: list[int], version: int) -> list[int]:
    blocks_n = _NUM_BLOCKS_M[version]
    ecc_len = _ECC_PER_BLOCK_M[version]
    raw = _raw_modules(version) // 8
    short_n = blocks_n - raw % blocks_n
    short_len = raw // blocks_n
    divisor = _rs_divisor(ecc_len)
    blocks = []
    k = 0
    for i in range(blocks_n):
        take = short_len - ecc_len + (0 if i < short_n else 1)
        chunk = data[k:k + take]
        k += take
        ecc = _rs_remainder(chunk, divisor)
        if i < short_n:
            chunk = chunk + [0]
        blocks.append(chunk + ecc)
    result = []
    for i in range(len(blocks[0])):
        for j, block in enumerate(blocks):
            if i != short_len - ecc_len or j >= short_n:
                result.append(block[i])
    return result


class _Matrix:
    def __init__(self, version: int):
        self.version = version
        self.size = version * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]

    def set_function(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = bool(dark)
        self.function[y][x] = True

    def draw_function_patterns(self) -> None:
        size = self.size
        for i in range(size):
            self.set_function(6, i, i % 2 == 0)
            self.set_function(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        self.set_function(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        positions = _alignment_positions(self.version, size)
        last = len(positions) - 1
        for i, ax in enumerate(positions):
            for j, ay in enumerate(positions):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_function(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.version << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1
                a, b = size - 11 + i % 3, i // 3
                self.set_function(a, b, bit)
                self.set_function(b, a, bit)

    def draw_format(self, mask: int) -> None:
        data = (0 << 3) | mask  # 纠错等级 M 的格式位为 00
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        size = self.size

        def bit(i: int) -> bool:
            return bool((bits >> i) & 1)

        for i in range(6):
            self.set_function(8, i, bit(i))
        self.set_function(8, 7, bit(6))
        self.set_function(8, 8, bit(7))
        self.set_function(7, 8, bit(8))
        for i in range(9, 15):
            self.set_function(14 - i, 8, bit(i))
        for i in range(8):
            self.set_function(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_function(8, size - 15 + i, bit(i))
        self.set_function(8, size - 8, True)

    def draw_codewords(self, codewords: list[int]) -> None:
        size = self.size
        i = 0
        total = len(codewords) * 8
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = size - 1 - vert if upward else vert
                    if not self.function[y][x] and i < total:
                        self.dark[y][x] = bool((codewords[i >> 3] >> (7 - (i & 7))) & 1)
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        tests = (
            lambda x, y: (x + y) % 2 == 0,
            lambda x, y: y % 2 == 0,
            lambda x, y: x % 3 == 0,
            lambda x, y: (x + y) % 3 == 0,
            lambda x, y: (x // 3 + y // 2) % 2 == 0,
            lambda x, y: x * y % 2 + x * y % 3 == 0,
            lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
            lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
        )
        test = tests[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and test(x, y):
                    self.dark[y][x] = not self.dark[y][x]

    def penalty(self) -> int:
        """简化罚分(连续同色、2x2 同色块、深浅比例)，只用于挑一个观感较好的掩码。"""
        size, grid = self.size, self.dark
        score = 0
        for lines in (grid, [list(col) for col in zip(*grid)]):
            for line in lines:
                run, color = 0, None
                for cell in line:
                    if cell == color:
                        run += 1
                    else:
                        if run >= 5:
                            score += run - 2
                        run, color = 1, cell
                if run >= 5:
                    score += run - 2
        for y in range(size - 1):
            for x in range(size - 1):
                c = grid[y][x]
                if c == grid[y][x + 1] == grid[y + 1][x] == grid[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(1 for cell in row if cell) for row in grid)
        total = size * size
        score += (abs(dark * 20 - total * 10) + total - 1) // total * 10
        return score


def matrix(text: str) -> list[list[bool]]:
    """把文本编码成二维码模块矩阵(True=深色)。超出容量抛 ValueError。"""
    payload = str(text).encode("utf-8")
    version = next(
        (
            v for v in range(1, _MAX_VERSION + 1)
            if 4 + (8 if v <= 9 else 16) + len(payload) * 8 <= _data_codewords(v) * 8
        ),
        None,
    )
    if version is None:
        raise ValueError("内容太长，无法生成二维码")
    codewords = _interleave(_encode_data(payload, version), version)
    best = None
    for mask in range(8):
        grid = _Matrix(version)
        grid.draw_function_patterns()
        grid.draw_codewords(codewords)
        grid.apply_mask(mask)
        grid.draw_format(mask)
        score = grid.penalty()
        if best is None or score < best[0]:
            best = (score, grid)
    return best[1].dark


def _svg_from_matrix(grid: list[list[bool]]) -> str:
    size = len(grid) + _QUIET * 2
    cells = "".join(
        f"M{x + _QUIET} {y + _QUIET}h1v1h-1z"
        for y, row in enumerate(grid)
        for x, cell in enumerate(row)
        if cell
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'shape-rendering="crispEdges" role="img" aria-label="微信付款二维码">'
        f'<rect width="{size}" height="{size}" fill="#fff"/>'
        f'<path fill="#000" d="{cells}"/></svg>'
    )


def _svg_with_library(text: str) -> str:
    import qrcode  # type: ignore[import-not-found]

    code = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M, border=_QUIET
    )
    code.add_data(text)
    code.make(fit=True)
    grid = [row[_QUIET:-_QUIET] for row in code.get_matrix()[_QUIET:-_QUIET]]
    return _svg_from_matrix(grid)


def svg(text: str) -> str:
    """返回二维码 SVG 字符串；都失败时返回空串(前端显示链接文本兜底)。"""
    if not text:
        return ""
    try:
        return _svg_with_library(text)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 - 第三方库异常不影响付款
        log.warning("qrcode library failed error_type=%s", type(exc).__name__)
    try:
        return _svg_from_matrix(matrix(text))
    except Exception as exc:  # noqa: BLE001
        log.warning("builtin qr encoder failed error_type=%s", type(exc).__name__)
        return ""
