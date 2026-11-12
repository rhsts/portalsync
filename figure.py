"""PortalSync's figure format: decrypting and encrypting a Skylanders figure image, its save areas and checksums,
reading what a figure holds (figure_info, figure_fields) and writing proven fields back (edit, maxed, repaired).
Nothing here touches the portal, files in dumps/ or the window. docs/FIGURE_FORMAT.md lists every field.
Copyright (C) 2026 rhsts. SPDX-License-Identifier: GPL-2.0-or-later (see LICENSE).
"""
import ctypes
import hashlib
import json
import os
import struct
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))  # the json files


def changed_blocks(a, b):
    """Data blocks (not block 0, not sector trailers) where two 1024-byte figure images differ."""
    return [n for n in range(1, 64) if n % 4 != 3 and a[n * 16:n * 16 + 16] != b[n * 16:n * 16 + 16]]


try:  # Dolphin's list_skylanders (Source/Core/Core/IOS/USB/Emulated/Skylanders/Skylander.cpp)
    with open(os.path.join(HERE, 'figure_names.json'), encoding='utf-8') as _f:
        NAMES = json.load(_f)  # "id:variant" -> character name
except OSError:
    NAMES = {}
try:  # "id:variant" -> [game, element], from Dolphin's list_skylanders (Senseis: Imaginators, no element)
    with open(os.path.join(HERE, 'figure_details.json'), encoding='utf-8') as _f:
        DETAILS = json.load(_f)
except OSError:
    DETAILS = {}
try:  # "id" -> the Skylanders Wiki's upgrade names (CC-BY-SA), made by tools/fetch_upgrade_names.py
    with open(os.path.join(HERE, 'upgrade_names.json'), encoding='utf-8') as _f:
        UPGRADE_NAMES = json.load(_f).get('figures', {})
except (OSError, ValueError):
    UPGRADE_NAMES = {}


def figure_name(data):
    """Character name of a figure image; id at 0x10 and variant at 0x1C, little-endian, as Dolphin reads them."""
    fid, var = struct.unpack_from('<H', data, 0x10)[0], struct.unpack_from('<H', data, 0x1C)[0]
    if f'{fid}:{var}' in NAMES:
        return NAMES[f'{fid}:{var}']
    return f'{NAMES[f"{fid}:0"]} (variant {var:#06x})' if f'{fid}:0' in NAMES else f'Figure {fid}'


def crc16(data):
    """CRC16-CCITT (init 0xFFFF, poly 0x1021): the header checksum a figure keeps at 0x1E over bytes 0x00-0x1D."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = (crc << 1 ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


def crypt(data, encrypt=False):
    """Decrypt (or encrypt) a figure's save blocks, as Dolphin's SkylanderFigure::DecryptFigure: blocks 8-63
    except sector trailers and all-zero blocks, AES-128-ECB with key MD5(blocks 0-1, block index, the
    Activision string). AES comes from Windows (bcrypt.dll), so no extra package is needed."""
    from ctypes import wintypes as w
    bc = ctypes.WinDLL('bcrypt')
    alg = ctypes.c_void_p()
    mode = 'ChainingModeECB\0'.encode('utf-16-le')
    if bc.BCryptOpenAlgorithmProvider(ctypes.byref(alg), 'AES', None, 0) or \
            bc.BCryptSetProperty(alg, 'ChainingMode', mode, len(mode), 0):
        raise OSError('Windows AES not available')
    run = bc.BCryptEncrypt if encrypt else bc.BCryptDecrypt
    out, n = bytearray(data), w.ULONG()
    try:
        for i in range(8, 64):
            block = bytes(data[i * 16:i * 16 + 16])
            if i % 4 == 3 or not any(block):
                continue
            key = hashlib.md5(bytes(data[:32]) + bytes([i]) + b' Copyright (C) 2010 Activision. All Rights Reserved. ').digest()
            h, buf = ctypes.c_void_p(), ctypes.create_string_buffer(16)
            bc.BCryptGenerateSymmetricKey(alg, ctypes.byref(h), None, 0, key, 16, 0)
            run(h, block, 16, None, None, 0, buf, 16, ctypes.byref(n), 0)
            bc.BCryptDestroyKey(h)
            out[i * 16:i * 16 + 16] = buf.raw
    finally:
        bc.BCryptCloseAlgorithmProvider(alg, 0)
    return bytes(out)


B_BLOCKS = (0x00, 0x10, 0x30, 0x40)  # the data blocks of a second save area (0x110 / 0x2D0)


def b_checksum(d, b):
    """(stored, computed) checksum type 6 of the second save area at offset b: CRC16-CCITT over its four data
    blocks with 06 01 in place of its own checksum (matches all 54 of the user's areas, 2026-10-16)."""
    data = b''.join(bytes(d[b + o:b + o + 16]) for o in B_BLOCKS)
    return struct.unpack_from('<H', data, 0)[0], crc16(b'\x06\x01' + data[2:])


def seal(d, a, b=None, trap=False):
    """Set the checksums of main area a (and second area b) in a decrypted image; a trap's type 3 is its own."""
    (_, t1), (_, t2), (_, t3) = area_checksums(d, a)
    t3 = trap_checksums(d, a)[2][1] if trap else t3
    struct.pack_into('<HH', d, a + 0x0A, t3, t2)
    struct.pack_into('<H', d, a + 0x0E, area_checksums(d, a)[0][1])  # type 1 covers types 2 and 3
    if b is not None:
        struct.pack_into('<H', d, b, b_checksum(d, b)[1])


def recrypt(original, plain):
    """Encrypt a changed decrypted image, keeping the original's stored bytes for every block whose content did
    not change. crypt() leaves zero blocks alone, but a figure can hold an encrypted empty block; re-encrypting
    everything would rewrite those for nothing."""
    old, new = crypt(original), bytearray(crypt(plain, encrypt=True))
    for i in range(64):
        if old[i * 16:i * 16 + 16] == plain[i * 16:i * 16 + 16]:
            new[i * 16:i * 16 + 16] = original[i * 16:i * 16 + 16]
    return bytes(new)


def repaired(data):
    """The figure with the checksums of its newest save areas made valid again, all progress kept. This is
    what SuperChargers did to the user's Snap Shot (2026-10-11): it saved the same data with good checksums."""
    d = bytearray(crypt(data))
    a, b = newest(d, 0x80, 0x240, 9), newest(d, 0x110, 0x2D0, 2)
    seal(d, a, b if any(data[b:b + 16]) else None)
    return recrypt(data, bytes(d))


def raw_a(a, st):
    """Struct offset (Runes' numbering, sector trailers skipped) -> raw offset, for an area starting at a sector."""
    blk, off = divmod(st, 16)
    return a + (blk + blk // 3) * 16 + off


def raw_b(b, st):
    """Struct offset 0x70-0xAF of the second area at b (0x110 / 0x2D0) -> raw offset."""
    blk, off = divmod(st - 0x70, 16)
    return b + (blk + (blk >= 2)) * 16 + off


def struct_bytes(d, a, start, n):
    """n struct bytes from offset start of the area at a, read across blocks with the sector trailers skipped."""
    return bytes(d[raw_a(a, st)] for st in range(start, start + n))


def trap_checksums(d, a):
    """A trap's type 3 covers 0x110 struct bytes from 0x40 (every villain slot; matches all of the user's trap
    dumps, 2026-10-27); types 1 and 2 as a Skylander's."""
    (s1, c1), (s2, c2), _ = area_checksums(d, a)
    return [(s1, c1), (s2, c2), (struct.unpack_from('<H', d, a + 0x0A)[0], crc16(struct_bytes(d, a, 0x40, 0x110)))]


def edit(data, gold=None, nickname=None, xp=None, upgrades=None, gearbits=None, villain=None):
    """The figure with new values, saved the way the game and Dolphin's SetData save: into the older area of each
    pair, with a counter one past the newest, so the previous save stays as fallback. Only the fields proven on
    the user's figures (docs/FIGURE_FORMAT.md)."""
    trap = kind_of(data) == 'trap'
    d = bytearray(crypt(data))
    a, b = newest(d, 0x80, 0x240, 9), newest(d, 0x110, 0x2D0, 2)
    a2, b2 = 0x80 + 0x240 - a, 0x110 + 0x2D0 - b
    # the second area: only for a figure that has one, or an edit that needs it (a figure only ever played in
    # Spyro's Adventure has none, and got one for nothing)
    second = not trap and bool(any(data[b:b + 16]) or (xp or 0) > 33000 or (upgrades or 0) >> 10 or gearbits is not None)
    for st in range(0, 0x150 if trap else 0x70, 16):  # a trap's area runs on over all its villain slots
        d[raw_a(a2, st):raw_a(a2, st) + 16] = d[raw_a(a, st):raw_a(a, st) + 16]
    d[a2 + 9] = (d[a + 9] + 1) & 0xFF
    if second:
        for o in B_BLOCKS:
            d[b2 + o:b2 + o + 16] = d[b + o:b + o + 16]
        d[b2 + 2] = (d[b + 2] + 1) & 0xFF
    A = lambda st: raw_a(a2, st)
    B = lambda st: raw_b(b2, st)
    if gold is not None:
        struct.pack_into('<H', d, A(0x03), gold)
    if nickname is not None:
        name = nickname.encode('utf-16-le').ljust(32, b'\0')
        d[A(0x20):A(0x20) + 16], d[A(0x30):A(0x30) + 16] = name[:16], name[16:32]
    if xp is not None:
        d[A(0x00):A(0x00) + 3] = min(xp, 33000).to_bytes(3, 'little')
        if second:
            struct.pack_into('<H', d, B(0x73), min(max(xp - 33000, 0), 63500))
            struct.pack_into('<I', d, B(0x78), min(max(xp - 96500, 0), 101000))
    if upgrades is not None:
        struct.pack_into('<H', d, A(0x10), (int.from_bytes(d[A(0x10):A(0x10) + 2], 'little') & ~0x3FF) | (upgrades & 0x3FF))
        if second:
            struct.pack_into('<H', d, B(0x76), (int.from_bytes(d[B(0x76):B(0x76) + 2], 'little') & ~0xF) | (upgrades >> 10 & 0xF))
    if gearbits is not None:
        struct.pack_into('<H', d, B(0x78), gearbits)
    if villain is not None:
        d[A(0x10)] = villain
    seal(d, a2, b2 if second else None, trap)
    return recrypt(data, bytes(d))


def edited(data, gold=None, nickname=None, level=None):
    """edit() with a level instead of experience: the level's first XP."""
    return edit(data, gold, nickname, None if level is None else LEVELS[level - 1])


def fix_checksums(data, plain):
    """A hand-edited decrypted image with the checksums of its newest areas set (Skylanders, vehicles, traps;
    other kinds keep theirs), encrypted: the editor's Hex tab."""
    d, k = bytearray(plain), kind_of(plain)
    if k in ('skylander', 'vehicle', 'trap'):
        a, b = newest(d, 0x80, 0x240, 9), newest(d, 0x110, 0x2D0, 2)
        seal(d, a, b if k != 'trap' and any(d[b:b + 16]) else None, k == 'trap')
    return recrypt(data, bytes(d))


def maxed(data):
    """Level 20, 65000 gold and every upgrade the figure can have (its path kept, the first when none)."""
    f = figure_fields(data)
    full = 0b1011011101 if 1000 <= f['id'] < 3000 else 0b1111111101  # a Swapper half: 3 basic, 2 path upgrades
    up = f['upgrades'] | full
    if f['wowpow_ok']:
        up |= 1 << 10
    if 600 <= f['id'] < 700:  # a Sensei: Imaginators keeps its upgrades another way (see page_upgrades)
        up = f['upgrades']
    return edit(data, gold=None if f['gold'] is None else 65000, xp=None if f['level'] is None else LEVELS[-1], upgrades=up)


def kind_of(data):
    """'skylander', 'trap', 'vehicle', 'crystal', 'new' (never saved by a game) or 'other' (items, trophies)."""
    fid = struct.unpack_from('<H', data, 0x10)[0]
    used = any(data[0x80:0x90]) or any(data[0x240:0x250])
    if 210 <= fid <= 220:
        return 'trap' if used else 'new'
    if 3200 <= fid < 3300:
        return 'vehicle' if used else 'new'
    if 680 <= fid < 700:
        return 'crystal'
    if 200 <= fid < 400 or 3300 <= fid < 3400 or fid >= 3500:
        return 'other'
    return 'skylander' if used else 'new'


def date_at(d, o):
    """'2026-10-11 13:28' from a minute, hour, day, month, year (u16) field; '' when never set."""
    return f'{struct.unpack_from("<H", d, o + 4)[0]:04}-{d[o + 3]:02}-{d[o + 2]:02} {d[o + 1]:02}:{d[o]:02}' if any(d[o:o + 6]) else ''


PLATFORMS_2011 = ('Wii', 'Xbox 360', 'PS3', 'PC', '3DS')
PLATFORMS_2013 = ('Wii U / Android', 'Xbox One', 'PS4', 'iOS', '', '', 'Switch')
UPGRADES = ('On a path', 'Bottom path', 'Upgrade 1', 'Upgrade 2', 'Upgrade 3', 'Upgrade 4', 'Path upgrade 1',
            'Path upgrade 2', 'Path upgrade 3', 'Soul Gem', 'Wow Pow / Sky-Chi', 'Other path upgrade 1',
            'Other path upgrade 2', 'Other path upgrade 3')
FIGURE_KINDS = {'skylander': 'Skylander', 'trap': 'Trap', 'vehicle': 'Vehicle', 'crystal': 'Creation Crystal',
                'new': 'Never saved by a game', 'other': 'Item or trophy'}


def figure_fields(data):
    """Everything the figure editor shows about a figure image, as a dict. Keys ending in _ are documented (Runes)
    but not proven on real figures: shown read-only. docs/FIGURE_FORMAT.md has the layout and the evidence."""
    fid, var = struct.unpack_from('<H', data, 0x10)[0], struct.unpack_from('<H', data, 0x1C)[0]
    details = DETAILS.get(f'{fid}:{var}') or DETAILS.get(f'{fid}:0') or ['', '']
    f = {'name': figure_name(data), 'uid': data[:4].hex(), 'id': fid, 'variant': var, 'kind': kind_of(data),
         'game': details[0], 'element': details[1]}
    if f['kind'] in ('new', 'other'):
        return f
    d = crypt(data)
    a, b = newest(d, 0x80, 0x240, 9), newest(d, 0x110, 0x2D0, 2)
    A = lambda st, n: int.from_bytes(d[raw_a(a, st):raw_a(a, st) + n], 'little')
    B = lambda st, n: int.from_bytes(d[raw_b(b, st):raw_b(b, st) + n], 'little')
    has_b = any(data[b:b + 16])
    if f['kind'] == 'trap':
        f['damaged'] = any(s != c for s, c in trap_checksums(d, a))
        f['villains'] = [(A(0x10 + k * 0x30, 1), A(0x11 + k * 0x30, 1)) for k in range(min(A(0x01, 1), 6))]
        return f
    if f['kind'] == 'crystal':  # nothing proven: Runes' crystal notes may be wrong, the user's crystal is blank
        f['money_'], f['time_'], f['placed_'] = A(0x03, 2), A(0x05, 4), date_at(d, raw_a(a, 0x40))
        f['blank'] = not any(d[0x80:0x90]) and not any(d[0x240:0x250])
        return f
    f['damaged'] = any(s != c for s, c in area_checksums(d, a)) or (has_b and b_checksum(d, b)[0] != b_checksum(d, b)[1])
    f['time'], f['placed'] = A(0x05, 4), date_at(d, raw_a(a, 0x40))
    if f['kind'] == 'vehicle':
        f['gearbits'] = B(0x78, 2) if has_b else 0
        f['vehicle_xp_'], f['vehicle_flags_'], f['mods_'] = A(0x00, 3), A(0x10, 3), A(0x3E, 2)
        f['cosmetics_'] = [A(o, 1) for o in (0x18, 0x19, 0x1A, 0x1B)]  # decoration, topper, neon, shout
        return f
    xp = A(0x00, 3) + (B(0x73, 2) + B(0x78, 4) if has_b else 0)
    f['xp'] = xp
    f['level'] = None if 1000 <= fid < 2000 or 600 <= fid < 700 else sum(xp >= need for need in LEVELS)
    f['gold'] = None if 1000 <= fid < 2000 else A(0x03, 2)  # a Swapper's bottom half: the top keeps them
    f['nickname'] = (d[raw_a(a, 0x20):raw_a(a, 0x20) + 16] + d[raw_a(a, 0x30):raw_a(a, 0x30) + 16]).decode(
        'utf-16-le', 'replace').split('\0')[0]
    f['reset'], f['owners'] = date_at(d, raw_a(a, 0x50)), A(0x4F, 1)
    build = d[raw_a(a, 0x4C):raw_a(a, 0x4C) + 3]
    f['build'] = f'20{build[0]:02}-{build[1]:02}-{build[2]:02}' if any(build) else ''
    f['platforms'] = [p for i, p in enumerate(PLATFORMS_2011) if A(0x13, 1) >> i & 1] + \
                     [p for i, p in enumerate(PLATFORMS_2013) if p and A(0x17, 1) >> i & 1]
    f['upgrades'] = ((B(0x76, 2) & 0xF) << 10 if has_b else 0) | (A(0x10, 3) & 0x3FF)
    f['wowpow_ok'] = bool(var & 0x0800) or 600 <= fid < 700 or bool(f['upgrades'] >> 10 & 1)
    return f


def upgrade_names(fid):
    """The Skylanders Wiki's names of a character's upgrades (upgrade_names.json), blank where unknown. A Swapper
    half has 2 path upgrades (as on the user's halves) and the wiki's 3 basic upgrades in boxes 1-3: the user's
    Hoot Loop (Bottom) has only box 1 set and the game shows only Portable Hole, the wiki's first, as bought
    (2026-10-30). Box 4 is no upgrade the game sells, so the editor leaves it alone."""
    n = UPGRADE_NAMES.get(str(fid), {})
    return {'page': n.get('page', ''), 'basic': n.get('basic', [''] * 4), 'soulgem': n.get('soulgem', ''),
            'paths': n.get('paths', ['', '']), 'path1': n.get('path1', [''] * 3), 'path2': n.get('path2', [''] * 3),
            'wowpow': n.get('wowpow', '')}


def area_checksums(d, a):
    """Checksum types 1, 2 and 3 of the decrypted save area at offset a, as (stored, computed) pairs. Type 1
    covers the area's first block with 05 00 in place of its own checksum; type 2 the next three data
    blocks; type 3 the three after that plus 0xE0 zero bytes (matches all of the user's figures, 2026-10-16)."""
    stored = lambda o: struct.unpack_from('<H', d, a + o)[0]
    return [(stored(0x0E), crc16(d[a:a + 0x0E] + b'\x05\x00')),
            (stored(0x0C), crc16(d[a + 0x10:a + 0x30] + d[a + 0x40:a + 0x50])),
            (stored(0x0A), crc16(d[a + 0x50:a + 0x70] + d[a + 0x80:a + 0x90] + bytes(0xE0)))]


# Total experience for levels 1-20. A figure keeps its experience in three counters that cap at 33000,
# 63500 and 101000: exactly the steps to level 10, 15 and 20, which checks this table at those levels.
LEVELS = (0, 1000, 2200, 3800, 6000, 9000, 13000, 18200, 24800, 33000,
          42700, 53900, 66600, 80800, 96500, 113700, 132400, 152600, 174300, 197500)


# Trap Team's trappable villains by the id a trap stores. The game's files number them 1001-1046 (e.g.
# character/1037_Tornado.arc) and each file holds that villain's voice lines, filed under its name
# (criminal_audio_sky4/bad_juju/...). Some folders keep a dev name; those were matched by element and
# codename: builder, composer, whiteout, cleptopatra, evil_twin, woollace, trolly_roller, hoodblink,
# troll_punches, maulrus, possession_mage, and (codename only) hawker, wood_chipper, stun_gun,
# dreadberry, junk_chunker, chow_hound, walking_timebomb. The user's four traps hold villains of their
# own element under this table (2026-10-16).
VILLAINS = {
    1: 'Chompy Mage', 2: 'Dr. Krankcase', 3: 'Wolfgang', 4: 'Chef Pepper Jack', 5: 'Nightshade', 6: 'Luminous',
    7: 'Golden Queen', 8: 'Dreamcatcher', 9: 'The Gulper', 10: 'Kaos', 11: 'Cuckoo Clocker', 12: 'Buzzer Beak',
    13: 'Shield Shredder', 14: 'Cross Crow', 15: 'Chompy', 16: 'Brawl and Chain', 17: 'Bomb Shell',
    18: 'Masker Mind', 19: 'Chill Bill', 20: 'Sheep Creep', 21: 'Shrednaught', 22: 'Chomp Chest',
    23: 'Broccoli Guy', 24: 'Rage Mage', 25: 'Lob Goblin', 26: 'Bone Chompy', 27: 'Fisticuffs',
    28: 'Trolling Thunder', 29: 'Hood Sickle', 30: 'Bruiser Cruiser', 31: 'Brawlrus', 32: 'Tussle Sprout',
    33: 'Krankenstein', 34: 'Scrap Shooter', 35: 'Slobber Trap', 36: 'Grinnade', 37: 'Bad Juju',
    38: 'Blaster-Tron', 39: 'Tae Kwon Crow', 40: 'Pain-Yatta', 41: 'Smoke Scream', 42: 'Eye Five',
    43: 'Grave Clobber', 44: 'Threatpack', 45: 'Mab Lobs', 46: 'Eye Scream'}


def newest(d, a, b, at):
    """Offset of the newer of two save areas a and b, by their counters at a + at and b + at (they wrap)."""
    if not any(d[b:b + 16]):
        return a
    return b if (d[a + at] + 1) & 0xFF == d[b + at] or not any(d[a:a + 16]) else a


def figure_info(data):
    """What a figure image holds: level, gold, nickname, and whether its newest save is damaged (the game
    then says "A toy on the portal has a problem", seen with Snap Shot on 2026-10-10). Traps, vehicles,
    items and trophies keep other data: for them only 'kind' is filled in."""
    fid = struct.unpack_from('<H', data, 0x10)[0]
    info = SimpleNamespace(kind='other', level=None, gold=None, nickname='', damaged=False, old_damaged=False,
                           villains=[], xp=None)
    if 210 <= fid <= 220 and (any(data[0x80:0x90]) or any(data[0x240:0x250])):  # a trap that has held a villain
        # Its newest area's first block counts the villains; each following sector starts with one's id.
        # The first is the one in the trap now (a newly trapped villain takes that place, 2026-10-16).
        d = crypt(data)
        a = newest(d, 0x80, 0x240, 9)
        info.kind = 'trap'
        info.villains = [VILLAINS.get(d[a + 0x10 + k * 0x40], f'villain {d[a + 0x10 + k * 0x40]}')
                         for k in range(min(d[a + 1], 6))]
        return info
    # items, traps, vehicles, trophies (Dolphin's types), and Imaginators Creation Crystals: a crystal passes checksum
    # types 1 and 2, but its third and second-area blocks use another layout (the user's two, 2026-10-18), so
    # reading it like a Skylander called it damaged and found a garbage nickname
    if 200 <= fid < 400 or 3200 <= fid < 3400 or fid >= 3500 or 680 <= fid < 700:
        return info
    if not any(data[0x80:0x90]) and not any(data[0x240:0x250]):
        info.kind = 'new'
        return info
    d = crypt(data)
    a = newest(d, 0x80, 0x240, 9)
    used = [x for x in (0x80, 0x240) if any(data[x:x + 16])]
    bad = {x: any(s != c for s, c in area_checksums(d, x)) for x in used}
    b = newest(d, 0x110, 0x2D0, 2)
    bad_b = {x: b_checksum(d, x)[0] != b_checksum(d, x)[1] for x in (0x110, 0x2D0) if any(data[x:x + 16])}
    info.kind, info.damaged = 'skylander', bad[a] or bad_b.get(b, False)
    info.old_damaged = any(v for x, v in bad.items() if x != a) or any(v for x, v in bad_b.items() if x != b)
    info.gold = struct.unpack_from('<H', d, a + 3)[0]
    info.nickname = (d[a + 0x20:a + 0x30] + d[a + 0x40:a + 0x50]).decode('utf-16-le', 'replace').split('\0')[0]
    if 1000 <= fid < 2000:  # a Swapper's bottom half: the top half keeps the level and gold
        info.gold = None
    elif not 600 <= fid < 700:  # Imaginators count experience differently
        xp = int.from_bytes(d[a:a + 3], 'little') + struct.unpack_from('<H', d, b + 3)[0] + int.from_bytes(d[b + 8:b + 11], 'little')
        info.level, info.xp = sum(xp >= need for need in LEVELS), xp
    return info


def stats(info):
    """' · level 12 · 6,896 gold' for a figure_info, '' when it has none."""
    return ((f' · level {info.level}' if getattr(info, 'level', None) else '')
            + (f' · {info.gold:,} gold' if getattr(info, 'gold', None) is not None else '')
            + (f' · holds {info.villains[0]}' if getattr(info, 'villains', None) else ''))


def figure_key(data):
    """'id:variant' of a figure image, the key of NAMES and DETAILS."""
    return f'{struct.unpack_from("<H", data, 0x10)[0]}:{struct.unpack_from("<H", data, 0x1C)[0]}'


def partner(key):
    """The other half of a Swapper ('2015:8192' -> '1015:8192'), or None. Bottoms are ids 1000-1999, tops
    2000-2999, with the same variant (all 60 halves in figure_names.json pair up this way)."""
    fid, var = map(int, key.split(':'))
    other = fid + 1000 if 1000 <= fid < 2000 else fid - 1000 if 2000 <= fid < 3000 else None
    return f'{other}:{var}' if other is not None and f'{other}:{var}' in NAMES else None
