"""Check the figure layout in docs/FIGURE_FORMAT.md against real figure dumps.

Run: python verify_layout.py [dumps folder]   (default: dumps)
Every field the figure editor lets you edit must keep passing here. Read-only: never writes a dump.
"""
import glob
import os
import re
import struct
import sys

import figure as fig  # noqa: E402  (figure_info and the checksums: an independent check of figure_fields)


def raw_a(a, st):
    """Struct offset 0x00-0x6F of the main area starting at a -> raw offset (sector trailers skipped)."""
    blk, off = divmod(st, 16)
    return a + (blk + blk // 3) * 16 + off


def raw_b(b, st):
    """Struct offset 0x70-0xAF of the second area starting at b -> raw offset."""
    blk, off = divmod(st - 0x70, 16)
    return b + (blk + (blk >= 2)) * 16 + off


def struct_bytes(d, a, start, n):
    """n struct bytes from offset start of the area at a, read across blocks with trailers skipped."""
    out, blk = b'', a // 16
    while len(out) < start + n:
        if blk % 4 != 3:
            out += bytes(d[blk * 16:blk * 16 + 16])
        blk += 1
    return out[start:start + n]


def u(d, o, n):
    return int.from_bytes(d[o:o + n], 'little')


def upgrades(d, a, b):
    return ((u(d, raw_b(b, 0x76), 2) & 0xF) << 10) | (u(d, raw_a(a, 0x10), 3) & 0x3FF)


def main(folder):
    assert raw_a(0x80, 0x4A) == 0x80 + 0x5A  # Dolphin keeps hero points at area + 0x5A
    seen, fails = {}, []

    def check(ok, what):
        if not ok:
            fails.append(what)

    for path in sorted(glob.glob(os.path.join(glob.escape(folder), '*.sky'))):
        name = os.path.basename(path)
        data = open(path, 'rb').read()
        fid = struct.unpack_from('<H', data, 0x10)[0]
        info = fig.figure_info(data)
        d = fig.crypt(data)
        a, b = fig.newest(d, 0x80, 0x240, 9), fig.newest(d, 0x110, 0x2D0, 2)
        if info.kind == 'trap':  # type 3 of a trap covers 0x110 struct bytes from 0x40 (all villain slots)
            check(struct.unpack_from('<H', d, a + 0x0A)[0] == fig.crc16(struct_bytes(d, a, 0x40, 0x110)), f'{name}: trap checksum')
            continue
        if 3200 <= fid < 3300 and any(data[0x80:0x90]):  # SuperChargers vehicle: same checksums as a Skylander
            check(all(s == c for s, c in fig.area_checksums(d, a)), f'{name}: vehicle checksums')
            check(u(d, raw_b(b, 0x78), 2) <= 33000, f'{name}: gearbits over 33000')
            continue
        if info.kind != 'skylander' or info.damaged:
            continue
        xp = (u(d, raw_a(a, 0x00), 3), u(d, raw_b(b, 0x73), 2), u(d, raw_b(b, 0x78), 4))
        # the third counter can pass 101000: Bumble Blast's original holds 101001, written by the game
        check(xp[0] <= 33000 and xp[1] <= 65535, f'{name}: xp parts {xp}')
        check(xp[1] == 0 or xp[0] == 33000, f'{name}: 2012 xp before 2011 xp is full')
        if xp == (33000, 63500, 101000):
            check(info.level == 20, f'{name}: all xp counters full but level {info.level}')
        up = upgrades(d, a, b)
        check(up & 1 or not up & 0b111000000, f'{name}: path upgrades without a path')
        build = d[raw_a(a, 0x4C):raw_a(a, 0x4C) + 3]
        check(not any(build) or (11 <= build[0] <= 17 and 1 <= build[1] <= 12 and 1 <= build[2] <= 31), f'{name}: build date {list(build)}')
        placed = raw_a(a, 0x40)
        m = re.search(r'(\d{4})-(\d\d)-(\d\d) \d\d\.\d\d\.\d\d', name)
        if m and 'before restore' in name and any(d[placed:placed + 6]):  # placed the same day it was saved (some figures keep no date)
            check((u(d, placed + 4, 2), d[placed + 3], d[placed + 2]) == tuple(map(int, m.groups())), f'{name}: last placed date')
        if 'Bumble Blast (Jolly)' in name:  # the Jolly variant comes wearing its holiday hat (2013 slot, id 153)
            check(u(d, raw_b(b, 0x7C), 1) == 153, f'{name}: hat')
        uid = re.search(r'\(([0-9a-f]{8})\)', name).group(1)
        seen.setdefault(uid, []).append((name, up, u(d, raw_a(a, 0x05), 4), (u(d, placed + 4, 2), d[placed + 3], d[placed + 2], d[placed + 1], d[placed])))

    for uid, saves in seen.items():  # within one figure: later saves only add upgrades and play time
        saves.sort(key=lambda s: (s[3], ' - current' in s[0]))  # no placed date: current is the newest
        for (n1, up1, t1, _), (n2, up2, t2, _) in zip(saves, saves[1:]):
            check(up1 & ~up2 == 0, f'{n1} -> {n2}: upgrades lost')
            check(t1 <= t2, f'{n1} -> {n2}: play time went down')

    print('\n'.join(fails) or f'layout OK on {sum(map(len, seen.values()))} Skylander saves and every trap/vehicle in {folder}')
    return not fails


if __name__ == '__main__':
    sys.exit(0 if main(sys.argv[1] if sys.argv[1:] else os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dumps')) else 1)
