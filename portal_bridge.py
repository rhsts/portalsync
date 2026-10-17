"""PortalSync (formerly Portal Bridge): software Skylanders Portal of Power exported over USB/IP to usbip-win2.
Copyright (C) 2026 rhsts. SPDX-License-Identifier: GPL-2.0-or-later (see LICENSE).

  python portal_bridge.py capture         # real portal plugged in: save its descriptors + back up figures
  python portal_bridge.py [a.sky ...]     # serve the virtual portal on 127.0.0.1:3240, attach it, open the UI

Dumps are named after the character and the figure's ID, e.g. dumps/Zook (86bc1263) - current.sky:
  current               the copy the game plays on; game saves land here
  loaded copy           what you play when you load a backup by hand (the backup is never written)
and one backup per figure, the newest; a new one sends the older one to the Recycle Bin:
  original              the first read of the real figure
  saved <time>          a current copy that differed from the figure when it was placed again
  before restore <time> what the figure held just before a restore wrote to it
Real portal (plugged in): placing a figure backs it up (if it has no backup) and copies it into a
free virtual slot; lifting it empties that slot. Unplugging the real portal keeps the copies loaded.
  Cemu:    start the bridge before booting the game; the real portal can stay plugged in. Cemu
           counts every portal when a game boots ("too many Portals" with real + virtual), so the
           bridge loads portal_hide.dll into each Cemu it sees, which hides the real portal from
           Cemu while the virtual one is attached (see portal_hide.c). Without the DLL: plug the
           real portal in after the game has started. Figures then follow the real portal live.
  Dolphin: the bridge loads portal_hide.dll into Dolphin too. Without it, never plug the real portal
           in while Dolphin runs: Dolphin rescans, counts it ("too many USB Wireless Receivers") and
           opens it alongside the bridge; pulling it out in that state crashed Windows (bugcheck
           0x50). Then place figures first, unplug, and start Dolphin.
  RPCS3:   start the bridge before booting the game: with no portal at boot RPCS3 adds its own
           emulated one, and the virtual portal would then be a second portal. RPCS3 rescans USB
           every 4 seconds and hands every 1430:0150 it finds to the game, so the bridge loads
           portal_hide.dll into it too. Without the DLL: same rule as Dolphin.
Write-back (checkbox, on by default, choice kept in settings.json): game saves to a copied figure are also written to the real
figure while it sits on the real portal, data blocks only, each verified by reading it back.
Restore (button; the pop-up also opens by itself when a "saved" copy is made): writes a figure's
backup onto it the same way; what the figure held becomes its backup ("before restore").
Right-click menu on a figure: unload, restore, edit gold/nickname/level, repair a damaged save, reset to a new figure (each written like a
restore), back up all dumps to a zip. Start game: launches a Skylanders game in Cemu, RPCS3 or Dolphin.
Drop .sky files on the window to load them. At start the latest GitHub release is checked (VERSION).
Add Skylander (button): search the figures in dumps/ and load one's current copy or backup, or create
a new blank figure of any Skylander in figure_names.json (as Dolphin's "Create Skylander" does).
Figure data (figure_info): the save is decrypted to show level, gold, nickname (traps: the villain inside), and to catch a damaged
save (the game's "A toy on the portal has a problem"). A damaged figure placed on the portal gets a
repair offered (progress kept); a damaged copy is never offered, written or kept over an intact backup.

Portal sound (Trap Team's villain and trap sounds): played on the real Traptanium portal's speaker,
or on the PC's default audio device when there is none or the portal is away, see PortalSound.

Game writes go straight back into the loaded file.

Portal logic is ported from Dolphin Source/Core/Core/IOS/USB/Emulated/Skylanders/Skylander.cpp
(cross-checked with RPCS3 rpcs3/Emu/Io/Skylander.cpp). USB/IP framing follows the Linux
Documentation/usb/usbip_protocol.rst.
"""
import array
import csv
import glob
import hashlib
import ctypes
import itertools
import json
import os
import queue
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))  # PortalSync.exe: its folder
DESC_FILE = os.path.join(HERE, 'portal_descriptors.json')
DUMPS = os.path.join(HERE, 'dumps')
SETTINGS = os.path.join(HERE, 'settings.json')  # choices made in the window, kept between runs
VID, PID = 0x1430, 0x0150
HOST, TCP_PORT, BUSID = '127.0.0.1', 3240, '1-1'
USBIP_EXE = r'C:\Program Files\USBip\usbip.exe'
VERSION = '1.4.2'  # compared with the latest GitHub release at start (latest_release)
RELEASES = 'https://api.github.com/repos/rhsts/portalsync/releases/latest'
HIDE_DLL = os.path.join(HERE, 'portal_hide.dll')
HIDE_FROM = {'cemu.exe', 'cemu_release.exe', 'rpcs3.exe', 'dolphin.exe'}  # emulators that get portal_hide.dll
RUNNING_EMULATORS = {}  # pid -> exe name of the HIDE_FROM emulators running now (watch_emulators)
STATUS_PERIOD = 0.01  # seconds between 'S' reports while the host polls; Cemu's emulated portal uses 10 ms
EPIPE, ECONNRESET = 32, 104  # Linux errno values, as USB/IP expects

ADDED, READY, REMOVING, REMOVED = 3, 1, 2, 0  # Dolphin Skylander status enum


LOG_FILE = None  # main() opens bridge.log
LOG_LINES = deque(maxlen=500)  # (number, line) for the window's log pane
LOG_NUMBERS = itertools.count(1)
LOG_LOCK = threading.Lock()  # numbers stay in order across threads


def log(*a):
    line = ' '.join([time.strftime('%H:%M:%S'), *map(str, a)])
    with LOG_LOCK:
        if sys.stdout:  # None in PortalSync.exe (no console)
            print(line, flush=True)
        if LOG_FILE:
            print(line, file=LOG_FILE, flush=True)
        LOG_LINES.append((next(LOG_NUMBERS), line))


# --------------------------------------------------------------------------- portal logic


class Slot:
    def __init__(self):
        self.status = REMOVED
        self.queued = deque()
        self.data = None  # bytearray(1024)
        self.last_id = None
        self.path = None  # dump file that game writes are saved to


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


def figure_name(data):
    """Character name of a figure image; id at 0x10 and variant at 0x1C, little-endian, as Dolphin reads them."""
    fid, var = struct.unpack_from('<H', data, 0x10)[0], struct.unpack_from('<H', data, 0x1C)[0]
    if f'{fid}:{var}' in NAMES:
        return NAMES[f'{fid}:{var}']
    return f'{NAMES[f"{fid}:0"]} (variant {var:#06x})' if f'{fid}:0' in NAMES else f'Figure {fid}'


def dump_path(data, kind):
    """dumps/<character> (<uid>) - <kind>.sky, e.g. 'Zook (86bc1263) - current.sky'. Kinds: current (the
    copy the game plays on), loaded copy (see open_dump), and the backups, at most one per figure (see
    keep_one_backup): original (first read), saved <time> (a current copy that differed from the figure
    when it was placed), before restore <time> (what the figure held before a restore wrote to it)."""
    return os.path.join(DUMPS, f'{figure_name(data)} ({data[:4].hex()}) - {kind}.sky')


BACKUP = re.compile(r'.+ \(([0-9a-f]{8})\) - (?:original|(?:saved|before restore)'
                    r' (\d{4}-\d\d-\d\d \d\d\.\d\d\.\d\d)(?:-(\d+))?)\.sky')


def backups(uid=None):
    """{uid: [backup paths, oldest first]} for one figure or all; 'original' counts as the oldest."""
    found = {}
    for name in os.listdir(DUMPS) if os.path.isdir(DUMPS) else []:
        m = BACKUP.fullmatch(name)
        if m and uid in (None, m[1]):
            found.setdefault(m[1], []).append(((m[2] or '', int(m[3] or 1)), os.path.join(DUMPS, name)))
    return {u: [p for _, p in sorted(v)] for u, v in found.items()}


def has_backup(uid):
    return bool(backups(uid))


DUMP_NAME = re.compile(r'(.+) \(([0-9a-f]{8})\) - (.+)\.sky')


def played():
    """Every figure in dumps/, newest played first: (name, uid, current path or None, newest backup or
    None, time last played). From file names only."""
    found, saved = {}, backups()
    for name in os.listdir(DUMPS) if os.path.isdir(DUMPS) else []:
        m = DUMP_NAME.fullmatch(name)
        if m:
            entry = found.setdefault(m[2], [m[1], m[2], None, (saved.get(m[2]) or [None])[-1], 0])
            path = os.path.join(DUMPS, name)
            if m[3] == 'current':
                entry[2] = path
            entry[4] = max(entry[4], os.path.getmtime(path))
    return sorted(map(tuple, found.values()), key=lambda e: -e[4])


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


A_BLOCKS = (0x00, 0x10, 0x20, 0x40, 0x50, 0x60, 0x80)  # the data blocks of a main save area, by offset
B_BLOCKS = (0x00, 0x10, 0x30, 0x40)  # the data blocks of a second save area (0x110 / 0x2D0)


def b_checksum(d, b):
    """(stored, computed) checksum type 6 of the second save area at offset b: CRC16-CCITT over its four data
    blocks with 06 01 in place of its own checksum (matches all 54 of the user's areas, 2026-10-16)."""
    data = b''.join(bytes(d[b + o:b + o + 16]) for o in B_BLOCKS)
    return struct.unpack_from('<H', data, 0)[0], crc16(b'\x06\x01' + data[2:])


def seal(d, a, b=None):
    """Set the checksums of main area a (and second area b) in a decrypted image."""
    (_, t1), (_, t2), (_, t3) = area_checksums(d, a)
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


def edited(data, gold=None, nickname=None, level=None):
    """The figure with new gold, nickname and/or level, saved the way the game and Dolphin's SetData save: into
    the older area of each pair, with a counter one past the newest, so the previous save stays as fallback."""
    d = bytearray(crypt(data))
    a, b = newest(d, 0x80, 0x240, 9), newest(d, 0x110, 0x2D0, 2)
    a2, b2 = 0x80 + 0x240 - a, 0x110 + 0x2D0 - b
    for o in A_BLOCKS:
        d[a2 + o:a2 + o + 16] = d[a + o:a + o + 16]
    for o in B_BLOCKS:
        d[b2 + o:b2 + o + 16] = d[b + o:b + o + 16]
    d[a2 + 9], d[b2 + 2] = (d[a + 9] + 1) & 0xFF, (d[b + 2] + 1) & 0xFF
    if gold is not None:
        struct.pack_into('<H', d, a2 + 3, gold)
    if nickname is not None:
        name = nickname.encode('utf-16-le').ljust(32, b'\0')
        d[a2 + 0x20:a2 + 0x30], d[a2 + 0x40:a2 + 0x50] = name[:16], name[16:32]
    if level is not None:
        xp = LEVELS[level - 1]
        d[a2:a2 + 3] = min(xp, 33000).to_bytes(3, 'little')
        struct.pack_into('<H', d, b2 + 3, min(max(xp - 33000, 0), 63500))
        d[b2 + 8:b2 + 11] = min(max(xp - 96500, 0), 101000).to_bytes(3, 'little')
    seal(d, a2, b2)
    return recrypt(data, bytes(d))


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
                           villains=[])
    if 210 <= fid <= 220 and (any(data[0x80:0x90]) or any(data[0x240:0x250])):  # a trap that has held a villain
        # Its newest area's first block counts the villains; each following sector starts with one's id.
        # The first is the one in the trap now (a newly trapped villain takes that place, 2026-10-16).
        d = crypt(data)
        a = newest(d, 0x80, 0x240, 9)
        info.kind = 'trap'
        info.villains = [VILLAINS.get(d[a + 0x10 + k * 0x40], f'villain {d[a + 0x10 + k * 0x40]}')
                         for k in range(min(d[a + 1], 6))]
        return info
    if 200 <= fid < 400 or 3200 <= fid < 3400 or fid >= 3500:  # items, traps, vehicles, trophies (Dolphin's types)
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
        info.level = sum(xp >= need for need in LEVELS)
    return info


def stats(info):
    """' · level 12 · 6,896 gold' for a figure_info, '' when it has none."""
    return ((f' · level {info.level}' if getattr(info, 'level', None) else '')
            + (f' · {info.gold:,} gold' if getattr(info, 'gold', None) is not None else '')
            + (f' · holds {info.villains[0]}' if getattr(info, 'villains', None) else ''))


def damaged(path):
    try:
        with open(path, 'rb') as f:
            data = f.read()
        return len(data) != 1024 or figure_info(data).damaged
    except (OSError, ValueError, IndexError, KeyError, struct.error):
        return True  # unreadable or invalid is never evidence of a good backup


def new_figure(fid, var):
    """A blank figure, fresh out of the box, as Dolphin's SkylanderFigure::Create makes it: random UID,
    character id and variant, header checksum, sector trailer access bytes. Keys stay zero: the real
    portal reads them as zero too (checked on the user's figures, 2026-10-14)."""
    taken = {e[1] for e in played()}
    while (uid := os.urandom(4)).hex() in taken:
        pass
    data = bytearray(1024)
    data[0:8] = uid + bytes([uid[0] ^ uid[1] ^ uid[2] ^ uid[3], 0x81, 0x01, 0x0F])  # UID, BCC, ATQA, SAK
    struct.pack_into('<H', data, 0x10, fid)
    struct.pack_into('<H', data, 0x1C, var)
    struct.pack_into('<H', data, 0x1E, crc16(data[:0x1E]))
    for sector in range(16):
        data[sector * 64 + 54:sector * 64 + 58] = bytes.fromhex('0f0f0f69' if sector == 0 else '7f0f0869')
    return bytes(data)


def recycle(path):
    """Move a file to the Recycle Bin, never a permanent delete. True when it is gone from its folder."""
    from ctypes import wintypes as w

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [('hwnd', w.HWND), ('wFunc', w.UINT), ('pFrom', w.LPCWSTR), ('pTo', w.LPCWSTR),
                    ('fFlags', ctypes.c_ushort), ('fAnyOperationsAborted', w.BOOL),
                    ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', w.LPCWSTR)]

    # FO_DELETE with FOF_ALLOWUNDO (Recycle Bin) | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    op = SHFILEOPSTRUCTW(wFunc=3, pFrom=os.path.abspath(path) + '\0', fFlags=0x40 | 0x10 | 0x04 | 0x400)
    ok = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not op.fAnyOperationsAborted
    return ok and not os.path.exists(path)


BACKUPS_KEPT = 1  # backups per figure (Settings), the newest intact ones


def keep_one_backup(uid, keep=None):
    """At most BACKUPS_KEPT backups per figure: keep `keep` (default: the newest whose save is not damaged, else
    the existing files are all preserved) and the next newest intact ones, and move the others to the Recycle
    Bin. An invalid `keep` is ignored."""
    paths = backups(uid).get(uid, [])
    if not keep or keep not in paths or damaged(keep):
        keep = next((p for p in reversed(paths) if not damaged(p)), None)
    if keep is None:
        return  # no known-good copy: preserve everything for recovery
    keepers = {os.path.normcase(keep)} | {os.path.normcase(p) for p in [p for p in reversed(paths)
                                          if p != keep and not damaged(p)][:BACKUPS_KEPT - 1]}
    for path in paths:
        if os.path.normcase(path) not in keepers:
            gone = recycle(path)
            log(f'{os.path.basename(path)} ' + ('moved to the Recycle Bin' if gone else 'NOT moved to the Recycle Bin, kept')
                + f'; the backup is {os.path.basename(keep)}')


def stamped(data, kind):
    """dump_path with the time added, never an existing file: 'Zook (86bc1263) - saved 2026-10-10 12.57.04.sky'."""
    return fresh_name(dump_path(data, time.strftime(f'{kind} %Y-%m-%d %H.%M.%S'))[:-4])


def rename_old_dumps():
    """Older versions named dumps <uid>.sky, <uid>.mirror.sky, <uid>.play.sky, <uid>.mirror.<time>.sky and
    <uid>.before-restore.<time>.sky. Give them the readable names; an existing file is never overwritten."""
    kinds = {(None, False): 'original', ('mirror', False): 'current', ('play', False): 'loaded copy',
             ('mirror', True): 'saved', ('before-restore', True): 'before restore'}  # (old part, has a time)
    for name in os.listdir(DUMPS) if os.path.isdir(DUMPS) else []:
        m = re.fullmatch(r'([0-9a-f]{8})(?:\.(mirror|play|before-restore))?(?:\.(\d{8})-(\d{6})(-\d+)?)?\.sky', name)
        if not m or (m[2], bool(m[3])) not in kinds:
            continue
        with open(os.path.join(DUMPS, name), 'rb') as f:
            data = f.read()
        if len(data) != 1024:
            continue
        kind = kinds[m[2], bool(m[3])]
        if m[3]:
            kind += f' {m[3][:4]}-{m[3][4:6]}-{m[3][6:]} {m[4][:2]}.{m[4][2:4]}.{m[4][4:]}{m[5] or ""}'
        new = dump_path(data, kind)
        if os.path.exists(new):
            log(f'not renamed: {name} ({os.path.basename(new)} exists)')
        else:
            os.rename(os.path.join(DUMPS, name), new)
            log(f'renamed {name} -> {os.path.basename(new)}')


def fresh_name(stem):
    """stem.sky, or stem-2.sky and so on when that exists: kept copies are never overwritten."""
    path, n = stem + '.sky', 1
    while os.path.exists(path):
        n += 1
        path = f'{stem}-{n}.sky'
    return path


def move_dumps(src, dst):
    """Move every .sky from folder src to folder dst (Settings: dumps folder); a name already there gets -2."""
    os.makedirs(dst, exist_ok=True)
    moved = 0
    for name in os.listdir(src) if os.path.isdir(src) else []:
        if name.endswith('.sky'):
            target = os.path.join(dst, name)
            shutil.move(os.path.join(src, name), target if not os.path.exists(target) else fresh_name(target[:-4]))
            moved += 1
    return moved


def save_dump(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)  # never leave a half-written dump


def is_backup(path):
    path = os.path.abspath(path)
    return os.path.normcase(os.path.dirname(path)) == os.path.normcase(DUMPS) and BACKUP.fullmatch(os.path.basename(path)) is not None


def open_dump(path):
    """Return (path_to_play_with, data). A backup (original, saved, before restore) is swapped for
    '... - loaded copy.sky', made from it on first use, so the backup is never written."""
    if is_backup(path):
        play = path[:path.rfind(' - ')] + ' - loaded copy.sky'
        if not os.path.exists(play):
            shutil.copyfile(path, play)
        path = play
    with open(path, 'rb') as f:
        data = f.read()
    if len(data) != 1024:
        raise ValueError(f'{os.path.basename(path)} is {len(data)} bytes, a .sky dump is 1024')
    return os.path.abspath(path), data


class Portal:
    """Dolphin's SkylanderPortal + SkylanderUSB command switch, 16 slots."""

    def __init__(self, r_reply):
        self.r_reply = r_reply  # captured from the real portal: identifies portal type to the game
        self.slots = [Slot() for _ in range(16)]
        self.queries = deque()  # 32-byte replies waiting for an interrupt IN transfer
        self.active = False
        self.counter = 0
        self.on_command = None  # Mirror hook: sees every command after the virtual portal handled it

    def command(self, buf):
        if buf[:1] == b'W' and len(buf) < 19:
            self.queries.append(bytes(32))  # reject incomplete writes before padding short reports
            return
        buf = bytes(buf).ljust(32, b'\0')  # Wii sends short reports, PC/Wii U send 32 bytes
        c, q = chr(buf[0]), None
        if c == 'A':
            q = bytes([0x41, buf[1], 0xFF, 0x77])
            # Dolphin: "2nd byte ... whether to activate (0x01) or deactivate (0x00)"
            self.activate() if buf[1] else self.deactivate()
        elif c == 'J':
            q = b'J'
        elif c == 'M':
            q = bytes([0x4D, buf[1], 0x00, 0x19])
        elif c == 'Q':
            q = self.query(buf[1] & 0xF, buf[2])
        elif c == 'R':
            q = self.r_reply
        elif c == 'W':
            q = self.write(buf[1] & 0xF, buf[2], buf[3:19])
        # 'L', 'S', 'V': no interrupt reply (Dolphin)
        if c not in 'CJLS':
            log(f"portal <- {c} {buf[1:3].hex(' ')}")
        if q is not None:
            self.queries.append(q.ljust(32, b'\0'))
        if self.on_command:
            self.on_command(buf)

    def activate(self):
        if self.active:
            return
        for s in self.slots:
            if s.status & 1:
                s.queued += (ADDED, READY)
        self.active = True

    def deactivate(self):
        for s in self.slots:
            if s.queued:
                s.status = s.queued[-1]
                s.queued.clear()
            s.status &= 1
        self.active = False

    def status(self):
        st = 0
        for s in reversed(self.slots):
            if s.queued:
                s.status = s.queued.popleft()
            st = st << 2 | s.status
        r = b'S' + st.to_bytes(4, 'little') + bytes([self.counter, self.active])
        self.counter = (self.counter + 1) & 0xFF
        return r.ljust(32, b'\0')

    def next_report(self):
        return self.queries.popleft() if self.queries else self.status()

    def query(self, n, block):
        if n >= 16 or block >= 64:
            return bytes(32)
        s = self.slots[n]
        if s.status & READY:
            return bytes([0x51, 0x10 | n, block]) + s.data[block * 16:block * 16 + 16]
        return bytes([0x51, 0x01, block])

    def write(self, n, block, data):
        if not 0 <= n < 16 or not 0 <= block < 64 or len(data) != 16:
            return bytes(32)
        s = self.slots[n]
        if s.status & 1:
            s.data[block * 16:block * 16 + 16] = data
            if s.path:
                try:
                    save_dump(s.path, s.data)
                except OSError as e:
                    log(f'SAVE FAILED {s.path}: {e} (kept in memory)')
            return bytes([0x57, 0x10 | n, block])
        return bytes([0x57, 0x01, block])

    def load(self, data, path=None):
        """Dolphin LoadSkylander: reuse the slot this figure last sat in, else the lowest free one."""
        serial = int.from_bytes(data[0:4], 'little')
        found = None
        for i, s in enumerate(self.slots):
            if not s.status & 1:
                if s.last_id == serial:
                    found = i
                    break
                if found is None:
                    found = i
        if found is not None:
            s = self.slots[found]
            s.data, s.status, s.last_id, s.path = bytearray(data), ADDED, serial, path
            s.queued += (ADDED, READY)
        return found

    def remove(self, n):
        s = self.slots[n]
        s.path = None
        if s.status & READY:
            s.status = REMOVING
            s.queued += (REMOVING, REMOVED)
            return True
        return False


# --------------------------------------------------------------------------- portal speaker


class PortalSound:
    """The portal speaker's audio. Trap Team sends it as 64-byte blocks to the OUT endpoint once 'M'
    reports an audio firmware version (the virtual portal answers 0x19, like Dolphin and Cemu): 32
    samples of 16-bit mono at 8000 Hz (Cemu src/Cafe/OS/libs/nsyshid/Skylander.cpp). Each block goes
    to the real Traptanium portal's speaker when `speaker` takes it (Mirror.play), else to the PC's
    default audio device. The user's Traptanium portal answers 'M' with version 0 all the same, but
    its speaker played a test tone at real-time pace (2026-10-10)."""

    RATE, CHUNK, MAX_QUEUED = 8000, 5 * 64, 25  # 5 blocks = 20 ms per buffer; at most 0.5 s queued

    def __init__(self, speaker=None):
        self.speaker = speaker  # block -> True when the real portal played it
        self.volume = 1.0  # set from the window's slider
        self.mode = 'portal'  # Settings: 'portal' (its speaker, else the PC), 'pc' or 'off'
        self.out, self.pending, self.partial = None, [], b''
        self.lock = threading.Lock()  # PC playback: feed() and close() run on different threads
        self.blocks = self.on_portal = self.dropped = 0
        self.since = time.monotonic()

    def open(self):
        from ctypes import wintypes as w

        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [('wFormatTag', w.WORD), ('nChannels', w.WORD), ('nSamplesPerSec', w.DWORD),
                        ('nAvgBytesPerSec', w.DWORD), ('nBlockAlign', w.WORD), ('wBitsPerSample', w.WORD),
                        ('cbSize', w.WORD)]

        class WAVEHDR(ctypes.Structure):
            _fields_ = [('lpData', ctypes.c_void_p), ('dwBufferLength', w.DWORD), ('dwBytesRecorded', w.DWORD),
                        ('dwUser', ctypes.c_size_t), ('dwFlags', w.DWORD), ('dwLoops', w.DWORD),
                        ('lpNext', ctypes.c_void_p), ('reserved', ctypes.c_size_t)]

        self.WAVEHDR, winmm = WAVEHDR, ctypes.WinDLL('winmm')
        winmm.waveOutOpen.argtypes = (ctypes.POINTER(ctypes.c_void_p), w.UINT, ctypes.c_void_p,
                                      ctypes.c_size_t, ctypes.c_size_t, w.DWORD)
        for f in (winmm.waveOutPrepareHeader, winmm.waveOutWrite, winmm.waveOutUnprepareHeader):
            f.argtypes = (ctypes.c_void_p, ctypes.c_void_p, w.UINT)
        winmm.waveOutReset.argtypes = winmm.waveOutClose.argtypes = (ctypes.c_void_p,)
        self.winmm = winmm
        fmt = WAVEFORMATEX(1, 1, self.RATE, self.RATE * 2, 2, 16, 0)  # PCM, mono, 16-bit
        out = ctypes.c_void_p()
        err = winmm.waveOutOpen(ctypes.byref(out), 0xFFFFFFFF, ctypes.byref(fmt), 0, 0, 0)  # WAVE_MAPPER: default device
        if err:
            raise OSError(f'waveOutOpen failed ({err})')
        self.out = out
        log('portal sound: playing on the PC (default audio device)')

    def feed(self, block):  # USB/IP thread, without the server lock: the real portal may take ~4 ms
        self.blocks += 1
        if self.volume != 1:  # samples are 16-bit little-endian
            samples = array.array('h', block)
            block = array.array('h', (max(-32768, min(32767, int(x * self.volume))) for x in samples)).tobytes()
        if self.mode == 'off':
            return
        if self.mode == 'portal' and self.speaker and self.speaker(block):
            self.on_portal += 1
        else:
            with self.lock:
                self.pc(block)
        if time.monotonic() - self.since >= 5:  # how fast the game sends: 250 blocks per second is real time
            log(f'portal sound: {self.blocks / (time.monotonic() - self.since):.0f} blocks/s, '
                + (f'{self.on_portal} on the portal speaker' if self.on_portal == self.blocks
                   else f'{self.blocks - self.on_portal} on the PC')
                + (f', {self.dropped} dropped (more than 0.5 s queued)' if self.dropped else ''))
            self.blocks = self.on_portal = self.dropped = 0
            self.since = time.monotonic()

    def pc(self, block):  # self.lock held
        if self.out is None:
            try:
                self.open()
            except OSError as e:
                self.out = False
                log(f'portal sound: {e}; no sound on the PC')
        if not self.out:
            return
        self.partial += block
        if len(self.partial) >= self.CHUNK:
            self.play(self.partial)
            self.partial = b''

    def close(self):
        """Stop and release the audio device. Required before this object goes away: Windows reads the
        queued buffers from Python memory while it plays them."""
        with self.lock:
            if self.out:
                self.winmm.waveOutReset(self.out)  # stops playback and marks every buffer done
                for hdr, _ in self.pending:
                    self.winmm.waveOutUnprepareHeader(self.out, ctypes.byref(hdr), ctypes.sizeof(hdr))
                self.pending.clear()
                self.winmm.waveOutClose(self.out)
            self.out = False  # closed for good: pc() won't open it again

    def play(self, data):
        for hdr, buf in [p for p in self.pending if p[0].dwFlags & 1]:  # WHDR_DONE: hand back finished buffers
            self.winmm.waveOutUnprepareHeader(self.out, ctypes.byref(hdr), ctypes.sizeof(hdr))
            self.pending.remove((hdr, buf))
        if len(self.pending) >= self.MAX_QUEUED:  # the game sends faster than it plays: keep the delay short
            self.dropped += len(data) // 64
            return
        buf = ctypes.create_string_buffer(data, len(data))
        hdr = self.WAVEHDR(ctypes.cast(buf, ctypes.c_void_p), len(data), 0, 0, 0, 0, None, 0)
        self.winmm.waveOutPrepareHeader(self.out, ctypes.byref(hdr), ctypes.sizeof(hdr))
        self.winmm.waveOutWrite(self.out, ctypes.byref(hdr), ctypes.sizeof(hdr))
        self.pending.append((hdr, buf))


# --------------------------------------------------------------------------- USB/IP server


def recv_exact(conn, n):
    buf = b''
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError('client closed')
        buf += chunk
    return buf


def descriptors(config):
    i = 0
    while i < len(config) and config[i]:
        yield config[i:i + config[i]]
        i += config[i]


class Server:
    def __init__(self, portal, desc):
        self.portal, self.desc = portal, desc
        self.cv = threading.Condition()  # ponytail: one lock for portal state, pending URBs and socket writes
        self.sound = None  # PortalSound: main() turns it on

    def serve(self, listener):
        while True:
            conn, _ = listener.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def dev_info(self, with_interfaces):
        d, cfg = self.desc.device, self.desc.config
        info = (f'/portal-bridge/{BUSID}'.encode().ljust(256, b'\0') + BUSID.encode().ljust(32, b'\0')
                + struct.pack('>III', 1, 1, self.desc.speed)
                + struct.pack('>HHH', *struct.unpack_from('<HHH', d, 8))
                + bytes([d[4], d[5], d[6], cfg[5], d[17], cfg[4]]))
        if with_interfaces:
            info += b''.join(bytes([x[5], x[6], x[7], 0]) for x in descriptors(cfg) if x[1] == 4 and x[3] == 0)
        return info

    def handle(self, conn):
        try:
            _, code, _ = struct.unpack('>HHI', recv_exact(conn, 8))
            if code == 0x8005:  # OP_REQ_DEVLIST
                conn.sendall(struct.pack('>HHII', 0x0111, 0x0005, 0, 1) + self.dev_info(True))
            elif code == 0x8003:  # OP_REQ_IMPORT
                busid = recv_exact(conn, 32).split(b'\0')[0].decode()
                if busid != BUSID:
                    conn.sendall(struct.pack('>HHI', 0x0111, 0x0003, 1))
                    return
                conn.sendall(struct.pack('>HHI', 0x0111, 0x0003, 0) + self.dev_info(False))
                log('imported by', conn.getpeername())
                self.urbs(conn)
        except (ConnectionError, OSError) as e:
            log('connection closed:', e)
        finally:
            conn.close()

    def urbs(self, conn):
        cv, portal = self.cv, self.portal
        pending = deque()  # (seqnum, length) of interrupt IN URBs waiting for a report
        alive = True

        def ret_submit(seq, status=0, data=b'', actual=None):  # call with cv held
            actual = len(data) if actual is None else actual
            conn.sendall(struct.pack('>IIIIIiIIII', 3, seq, 0, 0, 0, status, actual, 0, 0xFFFFFFFF, 0)
                         + bytes(8) + data)

        def pump():
            while True:
                with cv:
                    cv.wait_for(lambda: not alive or (pending and portal.queries), timeout=STATUS_PERIOD)
                    if not alive:
                        return
                    # real portal sends 'S' reports only once activated (Dolphin's 'S' comment + your portal);
                    # Trap Team on Cemu resets forever if it gets them before 'A'
                    if pending and (portal.queries or portal.active):
                        seq, n = pending.popleft()
                        report = portal.next_report()
                        if report[0] != ord('S'):
                            log(f'portal -> {chr(report[0])} {report[1:4].hex(" ")} (seq {seq}, len {n})')
                        try:
                            ret_submit(seq, data=report[:n])
                        except OSError:
                            return

        threading.Thread(target=pump, daemon=True).start()
        try:
            while True:
                h = recv_exact(conn, 48)
                cmd, seq, _, direction, ep = struct.unpack_from('>IIIII', h)
                if cmd == 2:  # USBIP_CMD_UNLINK
                    victim = struct.unpack_from('>I', h, 20)[0]
                    with cv:
                        hit = [p for p in pending if p[0] == victim]
                        for p in hit:
                            pending.remove(p)
                        log(f'UNLINK seq {victim}', 'cancelled' if hit else 'already answered')
                        conn.sendall(struct.pack('>IIIIIi', 4, seq, 0, 0, 0, -ECONNRESET if hit else 0) + bytes(24))
                    continue
                if cmd != 1:
                    raise ConnectionError(f'unknown command {cmd:#x}')
                length = struct.unpack_from('>I', h, 24)[0]
                setup = h[40:48]
                data = recv_exact(conn, length) if direction == 0 and length else b''
                if ep != 0 and direction == 0:  # interrupt OUT carries portal audio only (Cemu/Dolphin)
                    if self.sound and len(data) == 64:
                        self.sound.feed(data)  # not under cv: the real speaker takes ~4 ms per block
                    with cv:  # answered after the portal took it, so the game sends at playback speed
                        ret_submit(seq, actual=len(data))
                    continue
                with cv:
                    if ep == 0:
                        status, reply = self.control(setup, data)
                        wlen = struct.unpack_from('<H', setup, 6)[0]
                        if direction == 1:
                            ret_submit(seq, status, reply[:min(wlen, length)])
                        else:
                            ret_submit(seq, status, actual=len(data) if status == 0 else 0)
                        cv.notify_all()
                    else:  # interrupt IN: answered by pump()
                        pending.append((seq, length))
                        cv.notify_all()
        finally:
            with cv:
                alive = False
                cv.notify_all()

    def control(self, setup, data):
        rt, req, val, idx, wlen = struct.unpack('<BBHHH', setup)
        d = self.desc
        if req == 6 and rt in (0x80, 0x81):  # GET_DESCRIPTOR
            typ, i = val >> 8, val & 0xFF
            r = {1: d.device, 2: d.config, 3: d.strings.get(i), 0x22: d.reports.get(idx)}.get(typ)
            if typ == 0x21:  # HID descriptor lives inside the config descriptor
                r = next((x for x in descriptors(d.config) if x[1] == 0x21), None)
            log(f'GET_DESCRIPTOR type={typ:#04x} index={i} len={wlen}', 'ok' if r else 'STALL')
            return (0, r) if r else (-EPIPE, b'')
        if rt == 0x21 and req == 0x09:  # HID SET_REPORT carries portal commands
            self.portal.command(data)
            return 0, b''
        if (rt, req) in ((0x00, 9), (0x01, 11), (0x02, 1), (0x23, 3), (0x21, 0x0A), (0x21, 0x0B)):
            if (rt, req) == (0x00, 9):
                log(f'SET_CONFIGURATION {val}')
            return 0, b''  # SET_CONFIGURATION/INTERFACE, CLEAR_FEATURE, port reset, SET_IDLE, SET_PROTOCOL
        if (rt, req) == (0x80, 0):
            return 0, b'\0\0'  # GET_STATUS: bus powered
        if (rt, req) == (0x80, 8):
            return 0, bytes([d.config[5]])  # GET_CONFIGURATION
        if (rt, req) == (0x81, 10):
            return 0, b'\0'  # GET_INTERFACE
        log(f'STALL control {setup.hex(" ")}')
        return -EPIPE, b''


# --------------------------------------------------------------------------- real portal


def portal_devices():
    import libusb_package
    return list(libusb_package.find(find_all=True, idVendor=VID, idProduct=PID))


class RealPortal:
    def __init__(self, dev):
        self.dev = dev
        # One of: a command (control transfer) or a speaker block (OUT write). While the speaker plays
        # back to back, commands time out (measured: 2 of 4 failed after 1 s); taking turns, 40 of 40
        # took at most 26 ms and the sound stayed real time (2026-10-10). Reads (IN) don't need it.
        self.io = threading.Lock()

    @classmethod
    def find(cls, vhci_bus=None):
        """First 1430:0150 device not on usbip-win2's bus, claimed so emulators can't use it."""
        import usb.util
        for dev in portal_devices():
            if dev.bus != vhci_bus:
                usb.util.claim_interface(dev, 0)  # WinUSB/libusbK: exclusive while we hold it
                return cls(dev)
        return None

    def send(self, *cmd):
        import usb.core
        for attempt in range(3):  # this portal stalls the first SET_REPORT after open now and then
            try:
                with self.io:
                    return self.dev.ctrl_transfer(0x21, 0x09, 0x0200, 0, bytes(cmd).ljust(32, b'\0'))
            except usb.core.USBError:
                if attempt == 2:
                    raise
                time.sleep(0.05)

    def wait(self, first, timeout=1.0):
        import usb.core
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                r = bytes(self.dev.read(0x81, 32, timeout=100))
            except usb.core.USBTimeoutError:
                continue
            if r[0] == ord(first):
                return r
        raise TimeoutError(f'no {first!r} reply from real portal')

    def present(self, seconds=0.5):
        """Slots with a figure, OR-ed over a few status reports. The counter in byte 6 steps by 1 per
        report; when another program (an emulator) also reads the portal it steps by 2 (measured).
        It also steps by 0: the portal sends the same report again, about 1 in 120 reports and often
        twice in a row when figures are placed or lifted (measured with Wash Buckler, 2026-10-10).
        Counting those as skips made placing a Swapper look like another program."""
        bits, end, last, gaps = 0, time.monotonic() + seconds, None, 0
        while time.monotonic() < end:
            r = self.wait('S')
            bits |= int.from_bytes(r[1:5], 'little')
            gaps += last is not None and (r[5] - last) & 0xFF > 1
            last = r[5]
        if gaps >= 2:
            raise OSError('another program is reading the real portal')
        return [i for i in range(16) if bits >> (2 * i) & 1]

    def read_block_once(self, slot, block):
        for _ in range(5):
            self.send(ord('Q'), 0x10 | slot, block)
            r = self.wait('Q')
            if r[1] == 0x10 | slot and r[2] == block:
                return r[3:19]
        raise ValueError(f'slot {slot} block {block}: read failed')

    def read_block0(self, slot):
        for _ in range(10):
            d = self.read_block_once(slot, 0)
            if d[0] ^ d[1] ^ d[2] ^ d[3] == d[4] and d == self.read_block_once(slot, 0):  # UID check byte
                return d
        raise ValueError(f'slot {slot}: block 0 never read cleanly')

    def read_block(self, slot, block, ref=None):
        """This portal sometimes answers a query with the right block number but the data of the
        previously queried block (measured live: ~3% of reads, runs of 1-5 blocks). So park its buffer
        on block 0 first: a stale answer then shows up as block 0's data and is asked again."""
        ref = ref or self.read_block0(slot)
        if block == 0:
            return ref
        for _ in range(10):
            for _ in range(10):  # until the buffer provably holds block 0
                if self.read_block_once(slot, 0) == ref:
                    break
            d = self.read_block_once(slot, block)
            if d != ref:
                return d
        raise ValueError(f'slot {slot} block {block}: only stale answers')

    def read_figure(self, slot):
        """All 64 blocks, most read once. A stale answer is the data of the previously queried block, which
        here is the previous block, already read right; so only an answer equal to it needs the slow, parked
        read_block (equal data is common: runs of empty blocks). Measured on the real portal 2026-10-17, 15
        reads each identical to the parked method: a Swapper half 1.88 s -> 1.15 s (73 reads instead of 128),
        Snap Shot 1.84 s -> 1.06 s, a trap (mostly empty blocks) 1.88 s -> 1.43 s."""
        ref = self.read_block0(slot)
        got, prev, todo = {0: ref}, ref, list(range(1, 64))
        while todo:
            block = todo[0]
            if not any(prev):  # after an empty block read a sector trailer (never empty): the empty block after
                block = next((b for b in todo if b % 4 == 3), block)  # it can then be told from a stale answer
            todo.remove(block)
            d = None if not any(prev) and block % 4 != 3 else self.read_block_once(slot, block)
            if d is None or d == prev:
                d = self.read_block(slot, block, ref)
            got[block] = prev = d
        return b''.join(got[b] for b in range(64))

    def write_block(self, slot, block, data):
        self.send(ord('W'), 0x10 | slot, block, *data)
        if self.wait('W')[2] != block:  # Dolphin: the reply echoes the block in byte 3
            raise ValueError(f'slot {slot} block {block}: write not acknowledged')


class Mirror:
    """Holds the real portal and mirrors its figures into free virtual slots.

    Lifting a figure empties its virtual slot. Unplugging the real portal, or another program
    taking it, keeps the copied figures loaded: that is how Dolphin and RPCS3 get your real
    figures, since they must never see the real portal (they open it too, see the module docstring).
    """

    WRITE_ATTEMPTS = 3
    RETRY_DELAY = 0.25

    def __init__(self, server, vhci_bus):
        self.server, self.vhci_bus = server, vhci_bus
        self.real = None
        self.busy = None  # (bus, address) of a real portal another program is using; left alone until unplugged
        self.slots = {}  # real slot -> (virtual slot, mirror file, uid)
        self.kept = {}  # uid -> (virtual slot, mirror file): copies kept while the real portal is away
        self.fresh = False  # first scan after claiming decides which kept copies were lifted meanwhile
        self.failed = set()  # real slots that would not read; retried after the figure is lifted
        self.jobs = queue.Queue()  # real-portal work queued by game commands (write-back, lights)
        self.write_back = False  # UI checkbox, default off; change it with set_write_back()
        self.write_epoch = 0  # disabling cancels automatic jobs, including those already in the queue
        self.retries = {}  # (uid, block) -> failed automatic write, retained if retries are exhausted
        self.known = {}  # real slot -> bytearray: what the real figure holds (read on placement, updated by writes)
        self.left = {}  # uid -> what the real figure held when it was lifted (see place())
        self.offers = queue.Queue()  # archived saved copies the window offers to restore (see add())
        self.notices = queue.Queue()  # messages the window shows as a pop-up (see lost())
        self.repairs = queue.Queue()  # uids of damaged figures just placed; the window offers to repair them
        self.speaker = False  # the claimed real portal is a Traptanium portal (see poll), which has a speaker
        self.lights = True  # Settings: the real portal's lights follow the game
        self.audio_lock = threading.Lock()  # play() writes from the USB/IP thread; release() closes the handle
        self.state = 'real portal: looking'
        server.portal.on_command = self.on_command

    def run(self):
        while True:
            try:
                self.poll()
            except OSError as e:  # unplugged, taken by another program, or any USB error
                self.lost(e)
                time.sleep(1)

    def lost(self, e):
        """The real portal stopped working: let it go, and say why and what to do."""
        dev = self.real.dev if self.real else None
        self.release()
        present = [(d.bus, d.address) for d in portal_devices()]
        # a timeout is the portal not answering (a figure moved, a hiccup), not another program:
        # claim it again instead of waiting for an unplug. pyusb's own timeout (Errno 10060) counts too.
        timeout = isinstance(e, TimeoutError) or type(e).__name__ == 'USBTimeoutError'
        if dev is not None and not timeout and (dev.bus, dev.address) in present:
            self.busy = (dev.bus, dev.address)
        if self.busy not in present:
            self.busy = None
        who = ', '.join(sorted(set(RUNNING_EMULATORS.values())))
        if self.busy and who:  # the emulator booted its game before the bridge, so the game took the real portal
            state = f'real portal: in use by {who} - see the pop-up ({e})'
            if state != self.state:
                self.notices.put(f"{who} has the real portal: its game was started before the bridge, so the game "
                                 "can't see the bridge's portal.\n\n1. Stop the game.\n2. Unplug the real portal and "
                                 "plug it back in.\n3. Start the game again.\n\nNext time, start the bridge first.")
        elif self.busy:
            state = f'real portal: in use by another program - unplug it to free it ({e})'
        elif dev is not None and (dev.bus, dev.address) in present:
            state = f'real portal: lost ({e}); claiming it again'
        else:
            state = f'real portal: unplugged ({e})'
        if state != self.state:
            log(state + (f'; {len(self.kept)} copied figure(s) stay loaded' if self.kept else ''))
        self.state = state

    def poll(self):
        if self.real is None:
            import usb.util
            devs = [(d.bus, d.address, d) for d in portal_devices() if d.bus != self.vhci_bus]
            if self.busy not in [d[:2] for d in devs]:
                self.busy = None  # the portal another program had was unplugged
            devs = [d for d in devs if d[:2] != self.busy]
            if not devs:
                if not self.busy:
                    self.state = 'real portal: not plugged in'
                time.sleep(1)
                return
            self.busy = devs[0][:2]  # until the handshake works
            usb.util.claim_interface(devs[0][2], 0)
            self.real = RealPortal(devs[0][2])
            self.real.send(ord('R'))
            r = self.real.wait('R')
            self.real.send(ord('A'), 1)
            self.real.wait('A')
            self.busy, self.fresh = None, True
            # 52 02 18 is the Traptanium portal (the user's, whose speaker played a test tone; RPCS3's
            # emulated Traptanium portal answers the same). Its 'M' version reads 0, so it can't be used.
            self.speaker = r[1:3] == b'\x02\x18'
            self.state = 'real portal: claimed' + (' (Traptanium: portal sound plays on its speaker)' if self.speaker else '')
            log(self.state)
        present = set(self.real.present(0.2))
        self.failed &= present
        for i in sorted(present - self.slots.keys() - self.failed):
            self.add(i)
        for i in self.slots.keys() - present:
            self.remove(i)
        if self.fresh:
            with self.server.cv:
                for uid, (v, path) in self.kept.items():
                    if self.server.portal.slots[v].path == path:
                        self.server.portal.remove(v)
                        log(f'{uid} not on the real portal any more: virtual slot {v + 1} emptied')
                self.kept.clear()
            self.fresh = False
        self.run_jobs()

    def add(self, i):
        try:
            data = self.real.read_figure(i)
        except (ValueError, TimeoutError) as e:  # figure moved mid-read
            log(f'real slot {i}: {e}; lift and replace the figure to retry')
            self.failed.add(i)
            return
        uid = data[:4].hex()
        try:
            self.place(i, uid, data)
        except OSError as e:  # a file (locked by OneDrive, antivirus...), not a lost portal: no USB call in place()
            log(f'real slot {i}: {uid} not copied: {e}; lift and replace the figure to retry')
            self.failed.add(i)

    def place(self, i, uid, data):
        """Back up a figure just read from real slot i and put it in the virtual portal."""
        os.makedirs(DUMPS, exist_ok=True)
        figure = data
        if not has_backup(uid):
            save_dump(dump_path(data, 'original'), data)
            log('backed up', os.path.basename(dump_path(data, 'original')))
        path = dump_path(data, 'current')  # game writes land here (and on the figure if write-back is on)
        with self.server.cv:
            self.known[i] = bytearray(data)  # what the real figure holds right now
            v, kept_path = self.kept.pop(uid, (None, None))
            if v is not None and self.server.portal.slots[v].path == kept_path:
                self.slots[i] = (v, path, uid)
                n = self.queue_sync(i) if self.write_back else 0
                changed = sum(self.server.portal.slots[v].data[b * 16:b * 16 + 16] != data[b * 16:b * 16 + 16]
                              for b in range(64))
                log(f'real slot {i}: {uid} back, still in virtual slot {v + 1}'
                    + (f'; writing {n} block(s) saved while unplugged to the real figure' if n
                       else f'; {changed} block(s) saved while unplugged are only in {os.path.basename(path)}'
                       if changed else ''))
                return
        offer = catch_up = None
        if os.path.exists(path):
            with open(path, 'rb') as f:
                previous = f.read()
            if previous != data and self.write_back and self.left.get(uid) == data and not figure_info(previous).damaged:
                # The figure is exactly as the bridge left it, so the difference is only game saves that did
                # not reach it (lifted mid-write, 2026-10-11): finish them like write-back would have.
                catch_up = data = previous
            elif previous != data:  # progress the real figure does not have: keep it, never overwrite
                old = stamped(data, 'saved')
                os.replace(path, old)
                log(f'{os.path.basename(path)} differed from the real figure; kept as {os.path.basename(old)}')
                if figure_info(previous).damaged:  # never offer to write a save the game would refuse
                    log(f'{os.path.basename(old)}: its newest save is damaged, so it is not offered for restore')
                    keep_one_backup(uid)  # keeps an intact backup if there is one
                else:
                    offer = old if changed_blocks(previous, data) else None
        save_dump(path, data)
        with self.server.cv:
            v = self.server.portal.load(data, path)
            if v is not None:
                self.slots[i] = (v, path, uid)
                n = self.queue_sync(i) if catch_up else 0
        if v is None:
            log(f'real slot {i}: virtual portal full')
            self.failed.add(i)
            return
        log(f'real slot {i} -> virtual slot {v + 1}: {uid}'
            + (f'; writing {n} block(s) the game saved that did not reach the figure' if n else ''))
        if figure_info(figure).damaged:
            name = figure_name(figure)
            log(f'real slot {i}: the save on {name} ({uid}) is damaged; the game will say "A toy on the portal has a problem"')
            self.repairs.put(uid)  # the window offers a repair, which keeps the progress, instead of a backup
            if offer:
                keep_one_backup(uid, keep=offer)  # still the figure's backup
                offer = None
        if offer:
            keep_one_backup(uid, keep=offer)
            self.offers.put(offer)  # figure registered: the window asks whether to write the copy to it

    def remove(self, i):
        with self.server.cv:
            v, path, uid = self.slots.pop(i)
            self.clear_retries(uid)
            held = self.known.pop(i, None)
            if held is not None:
                self.left[uid] = bytes(held)
            if self.server.portal.slots[v].path == path:  # skip if the user already unloaded it
                self.server.portal.remove(v)
        log(f'real slot {i} lifted: virtual slot {v + 1} emptied')

    def release(self):
        with self.server.cv:
            self.write_epoch += 1
            self.retries.clear()
            for v, path, uid in self.slots.values():
                self.kept[uid] = (v, path)
            self.slots.clear()
            self.known.clear()
        dropped = 0
        while not self.jobs.empty():
            self.jobs.get_nowait()
            dropped += 1
        if dropped:
            log(f'{dropped} queued real-portal command(s) dropped (portal gone; saves stay in the current copy)')
        with self.audio_lock:  # no speaker write may be running while the handle closes
            if self.real:
                import usb.util
                try:
                    usb.util.dispose_resources(self.real.dev)
                except Exception:
                    pass
            self.real, self.speaker = None, False

    def play(self, block):  # USB/IP thread
        """Write a 64-byte portal-sound block to the real portal's speaker (OUT endpoint 0x02). It takes
        about 4 ms, the block's playing time, which paces the game. False when there is no speaker."""
        with self.audio_lock:
            if not self.speaker:
                return False
            try:
                with self.real.io:  # a waiting command goes first: it waits for at most this one block
                    self.real.dev.write(0x02, block, timeout=100)
                return True
            except Exception as e:  # usb.core.USBError, or the device going away
                self.speaker = False  # until the portal is claimed again
                log(f'portal speaker: {e}; portal sound plays on the PC until the portal is claimed again')
                return False

    def on_command(self, buf):  # server thread, cv held: only queue work here
        c = chr(buf[0])
        if c in 'CJLM' and self.real and (c == 'M' or self.lights):  # lights, speaker on/off ('M'): like the game
            self.jobs.put(('raw', buf))
        elif c == 'W' and self.write_back:
            n, block = buf[1] & 0xF, buf[2]
            s = self.server.portal.slots[n]
            real = next((i for i, (v, path, uid) in self.slots.items() if v == n and s.path == path), None)
            if real is not None:  # else: not a figure on the connected real portal
                self.queue_block(real, block, bytes(buf[3:19]))

    def queue_block(self, i, block, data):  # cv held
        uid = self.slots[i][2]
        if not 1 <= block < 64 or block % 4 == 3 or len(data) != 16:
            return log(f'write-back {uid} block {block:#04x}: refused (invalid data block)')
        if not has_backup(uid):
            return log(f'write-back {uid}: no backup in dumps/, not written')
        self.retries.pop((uid, block), None)  # a new save supersedes any older retry
        self.jobs.put(('write', i, block, bytes(data), uid, self.write_epoch))

    def queue_sync(self, i):  # cv held
        """Queue every data block where the game's copy differs from the real figure. Write-back only
        forwards new writes, so saves made earlier (before ticking the box, or while unplugged) would
        otherwise leave a half-updated save area on the figure."""
        v = self.slots[i][0]
        game, real = self.server.portal.slots[v].data, self.known.get(i)
        blocks = changed_blocks(game, real) if real is not None else []
        for b in blocks:
            self.queue_block(i, b, bytes(game[b * 16:b * 16 + 16]))
        return len(blocks)

    def figure_data(self, uid):
        """What the figure uid on the real portal holds (read on placement, updated by writes), or None."""
        with self.server.cv:
            return next((bytes(self.known[i]) for i, s in self.slots.items() if s[2] == uid and i in self.known), None)

    def compare(self, path):
        """(uid, data blocks that differ from that figure on the real portal), or None when the file is
        no figure dump or its figure is not on the real portal."""
        with open(path, 'rb') as f:
            data = f.read()
        if len(data) != 1024:
            return None
        uid = data[:4].hex()
        with self.server.cv:
            real = next((self.known[i] for i, s in self.slots.items() if s[2] == uid and i in self.known), None)
        return (uid, len(changed_blocks(data, real))) if real is not None else None

    def saved_copies(self):
        """The backups of figures now on the real portal (one per figure, see keep_one_backup), as
        (path, 'Zook (86bc1263)', 'saved 2026-10-10 12:57:04', data blocks that differ from the figure)."""
        found = []
        for paths in backups().values():
            for path in reversed(paths):  # newest first
                r = self.compare(path)
                if r:
                    label, what = os.path.basename(path)[:-4].split(') - ', 1)
                    found.append((path, label + ')', re.sub(r'(\d\d)\.(\d\d)\.(\d\d)', r'\1:\2:\3', what), r[1]))
        return found

    def restore(self, path):
        """Check a saved copy and queue writing it onto its figure on the real portal. The Mirror thread
        does the work in restore_now(), after any game writes queued before it. Returns the figure's uid."""
        with open(path, 'rb') as f:
            data = f.read()
        if len(data) != 1024:
            raise ValueError(f'{os.path.basename(path)} is {len(data)} bytes, a .sky dump is 1024')
        uid = data[:4].hex()
        with self.server.cv:
            if not any(s[2] == uid for s in self.slots.values()):
                raise ValueError(f'figure {uid} is not on the real portal')
        if not has_backup(uid):
            raise ValueError(f'figure {uid} has no backup in dumps/, so it is not written')
        self.jobs.put(('restore', uid, data, os.path.basename(path)))
        return uid

    def write_image(self, uid, make, what):
        """Queue writing make(what the figure holds) onto figure uid on the real portal, like a restore: what
        the figure held becomes its backup first, every block is verified, and the game gets the result."""
        held = self.figure_data(uid)
        if held is None:
            raise ValueError(f'figure {uid} is not on the real portal')
        if not has_backup(uid):
            raise ValueError(f'figure {uid} has no backup in dumps/, so it is not written')
        self.jobs.put(('restore', uid, make(held), what))

    def reset(self, uid):
        """Wipe the progress on figure uid: its save blocks (8-63, not the sector trailers) become empty, as on
        a figure fresh out of the box."""
        self.write_image(uid, lambda held: bytes(held[:128]) + b''.join(
            bytes(held[b * 16:b * 16 + 16]) if b % 4 == 3 else bytes(16) for b in range(8, 64)), 'an empty save (reset)')

    def restore_now(self, uid, data, name):  # Mirror thread
        """Write a saved copy onto its figure, data blocks only, each verified. First: save what the
        figure really holds (read now) as '... - before restore <time>.sky', which becomes the figure's
        one backup (older ones, and a game copy with progress the figure lacks, go to the Recycle Bin),
        and give the game the restored data (the figure is lifted and placed again in the virtual portal
        so the game reads it)."""
        i = next((i for i, s in self.slots.items() if s[2] == uid), None)
        if i is None:
            return log(f'restore {uid}: the figure left the real portal, nothing written')
        held = self.real.read_figure(i)
        if held[:4].hex() != uid:
            return log(f'restore {uid}: another figure is in real slot {i}, nothing written')
        try:  # files only in here: an OSError is a file problem, not a lost portal
            snapshot = stamped(held, 'before restore')
            save_dump(snapshot, held)
            with self.server.cv:
                v, mirror, _ = self.slots[i]
                portal = self.server.portal
                game = portal.slots[v]
                here = game.path == mirror
                if not here and all(s.status & 1 for s in portal.slots):
                    return log(f'restore {uid}: virtual portal full, nothing written')
                if here and bytes(game.data) != data and changed_blocks(game.data, held):
                    kept = stamped(held, 'saved')
                    save_dump(kept, bytes(game.data))  # unsynced game progress: recoverable from the Recycle Bin
                    log(f'restore {uid}: the game copy had progress the figure lacks; saved as {os.path.basename(kept)}')
                save_dump(mirror, data)
                if here:
                    portal.remove(v)
                self.known[i] = bytearray(held)
                self.slots[i] = (portal.load(data, mirror), mirror, uid)
        except OSError as e:
            return log(f'restore {uid}: {e}; nothing written to the figure')
        blocks = changed_blocks(data, held)
        ok = sum(self.write_verified(i, b, data[b * 16:b * 16 + 16], uid, 'restore') for b in blocks)
        log(f'restore {uid} from {name}: {ok} of {len(blocks)} block(s) written and verified;'
            f' the figure held {os.path.basename(snapshot)} before')
        keep_one_backup(uid, keep=None if damaged(snapshot) else snapshot)  # never an intact backup for a damaged one

    def write_verified(self, i, block, data, uid, what='write-back'):  # Mirror thread
        """Write one data block to the figure in real slot i if it is still uid; True if it reads back equal."""
        if not 1 <= block < 64 or block % 4 == 3 or len(data) != 16:
            log(f'{what} {uid} block {block:#04x}: refused (invalid data block)')
            return False
        try:
            if self.real.read_block(i, 0)[:4].hex() != uid:  # figure swapped since the write was queued
                log(f'{what} {uid} block {block:#04x}: figure in real slot {i} changed, not written')
                return False
            self.real.write_block(i, block, data)
            ok = self.real.read_block(i, block) == data
        except (ValueError, TimeoutError) as e:  # the figure was lifted mid-write (Swappers, 2026-10-10)
            ok = False
            log(f'{what} {uid} block {block:#04x}: {e}')
        if ok and i in self.known:
            with self.server.cv:
                self.known[i][block * 16:block * 16 + 16] = data
        log(f'{what} {uid} block {block:#04x}: ' + ('verified' if ok else 'FAILED - real figure may differ'))
        return ok

    def set_write_back(self, on):
        with self.server.cv:
            if not on:
                self.write_epoch += 1
                self.retries.clear()
            self.write_back = on
            return sum(self.queue_sync(i) for i in self.slots) if on else 0

    def clear_retries(self, uid):  # cv held
        for key in [key for key in self.retries if key[0] == uid]:
            del self.retries[key]

    def current_write(self, i, block, data, uid, epoch):  # cv held
        if not self.write_back or epoch != self.write_epoch or i not in self.slots:
            return False
        v, path, current_uid = self.slots[i]
        game = self.server.portal.slots[v]
        return (current_uid == uid and game.path == path and game.status & 1
                and game.data[block * 16:block * 16 + 16] == data)

    def run_write(self, args, attempt=1):  # Mirror thread; never hold cv during hardware I/O
        i, block, data, uid, epoch = args
        key = (uid, block)
        with self.server.cv:
            if not self.current_write(*args):
                self.retries.pop(key, None)
                return
        ok = self.write_verified(i, block, data, uid)
        with self.server.cv:
            if ok or not self.current_write(*args):
                self.retries.pop(key, None)
                return
            due = time.monotonic() + self.RETRY_DELAY * 2 ** (attempt - 1)
            self.retries[key] = SimpleNamespace(args=args, attempt=attempt, due=due)
            if attempt == self.WRITE_ATTEMPTS:
                self.notices.put(f'Figure {uid}: block {block:#04x} could not be saved after {attempt} attempts. '
                                 'Its latest progress is in the current .sky copy. Check the portal, then turn '
                                 'write-back off and on to retry, or lift and replace the figure.')

    def sync_status(self):
        with self.server.cv:
            failed = sum(r.attempt >= self.WRITE_ATTEMPTS for r in self.retries.values())
            pending = len(self.retries) - failed
        return (f' · retrying {pending} save block(s)' if pending else '') + (f' · {failed} save block(s) FAILED' if failed else '')

    def run_jobs(self):
        while not self.jobs.empty():
            job = self.jobs.get_nowait()
            if job[0] == 'raw':
                self.real.send(*job[1])
            elif job[0] == 'restore':
                with self.server.cv:
                    self.clear_retries(job[1])  # explicit restore supersedes older automatic retries
                try:
                    self.restore_now(*job[1:])
                except (ValueError, TimeoutError) as e:  # figure moved mid-read/write
                    log(f'restore {job[1]}: {e}; stopped, see the lines above for what was written')
            else:
                self.run_write(job[1:])
        with self.server.cv:
            due = [(key, r) for key, r in self.retries.items()
                   if r.attempt < self.WRITE_ATTEMPTS and r.due <= time.monotonic()]
        for key, retry in due:
            with self.server.cv:
                if self.retries.get(key) is not retry:
                    continue
            self.run_write(retry.args, retry.attempt + 1)


def capture():
    rp = RealPortal.find()
    if rp is None:
        raise SystemExit('Real portal not found.')
    dev = rp.dev

    def get(val, idx, n, rt=0x80):
        return bytes(dev.ctrl_transfer(rt, 6, val, idx, n))

    device = get(0x0100, 0, 18)
    config = get(0x0200, 0, struct.unpack_from('<H', get(0x0200, 0, 9), 2)[0])
    langs = get(0x0300, 0, 255)
    lang = struct.unpack_from('<H', langs, 2)[0]
    idx, reports, iface = set(device[14:17]), {}, 0
    for x in descriptors(config):
        if x[1] == 2:
            idx.add(x[6])
        elif x[1] == 4:
            iface = x[2]
            idx.add(x[8])
        elif x[1] == 0x21 and x[6] == 0x22:
            reports[iface] = get(0x2200, iface, struct.unpack_from('<H', x, 7)[0], 0x81)
    strings = {0: langs, **{i: get(0x0300 | i, lang, 255) for i in sorted(idx - {0})}}
    rp.send(ord('R'))
    r_reply = rp.wait('R')
    out = {'speed': dev.speed, 'device': device.hex(), 'config': config.hex(),
           'strings': {i: s.hex() for i, s in strings.items()},
           'reports': {i: r.hex() for i, r in reports.items()}, 'r_reply': r_reply.hex()}
    with open(DESC_FILE, 'w') as f:
        json.dump(out, f, indent=1)
    log('saved', DESC_FILE)
    for k, v in out.items():
        log(f'  {k}: {v}')

    rp.send(ord('A'), 1)
    log('A reply:', rp.wait('A').hex(' '))
    os.makedirs(DUMPS, exist_ok=True)
    rename_old_dumps()
    for slot in rp.present():
        data = rp.read_figure(slot)
        path = dump_path(data, 'original')
        if os.path.exists(path):
            log(f'slot {slot}: {path} exists, kept (identical: {open(path, "rb").read() == data})')
        else:
            with open(path, 'wb') as f:
                f.write(data)
            log(f'slot {slot}: backed up to {path}')
    rp.send(ord('A'), 0)


# --------------------------------------------------------------------------- main


def load_desc():
    with open(DESC_FILE) as f:
        j = json.load(f)
    return SimpleNamespace(speed=j['speed'], device=bytes.fromhex(j['device']), config=bytes.fromhex(j['config']),
                           strings={int(i): bytes.fromhex(s) for i, s in j['strings'].items()},
                           reports={int(i): bytes.fromhex(r) for i, r in j['reports'].items()},
                           r_reply=bytes.fromhex(j['r_reply']))


def inject(pid, dll):
    """Load dll into process pid by running LoadLibraryW there on a remote thread. True if it loaded."""
    from ctypes import wintypes as w
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.restype = w.HANDLE
    k.VirtualAllocEx.restype = ctypes.c_void_p
    k.VirtualAllocEx.argtypes = (w.HANDLE, ctypes.c_void_p, ctypes.c_size_t, w.DWORD, w.DWORD)
    k.WriteProcessMemory.argtypes = (w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p)
    k.GetModuleHandleW.restype = w.HMODULE
    k.GetProcAddress.restype = ctypes.c_void_p
    k.GetProcAddress.argtypes = (w.HMODULE, ctypes.c_char_p)
    k.CreateRemoteThread.restype = w.HANDLE
    k.CreateRemoteThread.argtypes = (w.HANDLE, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p,
                                     w.DWORD, ctypes.c_void_p)
    k.WaitForSingleObject.argtypes = (w.HANDLE, w.DWORD)
    k.GetExitCodeThread.argtypes = (w.HANDLE, ctypes.POINTER(w.DWORD))
    k.VirtualFreeEx.argtypes = (w.HANDLE, ctypes.c_void_p, ctypes.c_size_t, w.DWORD)
    k.CloseHandle.argtypes = (w.HANDLE,)
    proc = k.OpenProcess(0x043A, False, pid)  # CREATE_THREAD | QUERY_INFORMATION | VM_OPERATION/READ/WRITE
    if not proc:
        raise ctypes.WinError(ctypes.get_last_error())
    path = ctypes.create_unicode_buffer(dll)
    mem = k.VirtualAllocEx(proc, None, ctypes.sizeof(path), 0x3000, 0x04)  # MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE
    done = False
    try:
        if not mem or not k.WriteProcessMemory(proc, mem, path, ctypes.sizeof(path), None):
            raise ctypes.WinError(ctypes.get_last_error())
        # kernel32 is mapped at the same address in every process until reboot
        load = k.GetProcAddress(k.GetModuleHandleW('kernel32'), b'LoadLibraryW')
        thread = k.CreateRemoteThread(proc, None, 0, load, mem, 0, None)
        if not thread:
            raise ctypes.WinError(ctypes.get_last_error())
        done = k.WaitForSingleObject(thread, 10000) == 0
        code = w.DWORD()
        k.GetExitCodeThread(thread, ctypes.byref(code))
        k.CloseHandle(thread)
        return done and code.value != 0  # low 32 bits of the module handle; 0 = LoadLibraryW failed
    finally:
        if mem and done:  # never free the path while LoadLibraryW may still be reading it
            k.VirtualFreeEx(proc, mem, 0, 0x8000)
        k.CloseHandle(proc)


def watch_emulators():
    """Load portal_hide.dll into every emulator in HIDE_FROM running while the bridge runs, before it boots a game."""
    seen, tries = set(), {}
    while True:
        out = subprocess.run(['tasklist', '/fo', 'csv', '/nh'], capture_output=True, text=True,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        rows = [r[:2] for r in csv.reader(out.splitlines()) if len(r) > 1 and r[0].lower() in HIDE_FROM]
        global RUNNING_EMULATORS
        RUNNING_EMULATORS = {pid: name for name, pid in rows}
        running = {pid for _, pid in rows}
        seen &= running  # an ended pid may come back as a new emulator
        tries = {pid: n for pid, n in tries.items() if pid in running}
        for name, pid in rows:
            if pid in seen:
                continue
            tries[pid] = tries.get(pid, 0) + 1
            try:
                ok, why = inject(int(pid), HIDE_DLL), 'DllMain refused'
            except OSError as e:  # e.g. access denied while the process is still starting or already exiting
                ok, why = False, e
            if ok:
                seen.add(pid)
                log(f'{name} (pid {pid}): real portal hidden from it while the virtual one is attached')
            elif tries[pid] >= 10:
                seen.add(pid)
                log(f'{name} (pid {pid}): portal_hide.dll did NOT load after 10 tries: {why}')
        time.sleep(1)


def newer(tag, current=VERSION):
    """True when a release tag like 'v1.3.0' is a later version than current."""
    try:
        return tuple(map(int, tag.lstrip('v').split('.'))) > tuple(map(int, current.split('.')))
    except ValueError:
        return False


def latest_release():
    """(tag, page url) of the latest GitHub release, or None when it can't be reached."""
    import urllib.request
    try:
        req = urllib.request.Request(RELEASES, headers={'User-Agent': f'PortalSync/{VERSION}'})
        with urllib.request.urlopen(req, timeout=10) as r:
            j = json.load(r)
        return j['tag_name'], j['html_url']
    except (OSError, ValueError, KeyError) as e:
        log(f'update check: {e}')
        return None


def accept_drops(hwnd, on_files):
    """Let files be dropped on a window: on_files(paths) runs for each drop (WM_DROPFILES), inside Tk's event
    handling, so it must not call Tk itself. Returns the window procedure, to keep alive with the window."""
    u, sh, P, L = ctypes.windll.user32, ctypes.windll.shell32, ctypes.c_void_p, ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(L, P, ctypes.c_uint, ctypes.c_size_t, L)
    u.SetWindowLongPtrW.restype = u.CallWindowProcW.restype = L
    u.SetWindowLongPtrW.argtypes = (P, ctypes.c_int, P)
    u.CallWindowProcW.argtypes = (L, P, ctypes.c_uint, ctypes.c_size_t, L)
    sh.DragQueryFileW.argtypes = (P, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint)
    sh.DragFinish.argtypes = (P,)
    sh.DragAcceptFiles.argtypes = (P, ctypes.c_bool)
    old = None

    def proc(h, msg, wp, lp):
        if msg != 0x233:  # WM_DROPFILES
            return u.CallWindowProcW(old, h, msg, wp, lp)
        paths = []
        for k in range(sh.DragQueryFileW(wp, 0xFFFFFFFF, None, 0)):
            buf = ctypes.create_unicode_buffer(1024)
            sh.DragQueryFileW(wp, k, buf, len(buf))
            paths.append(buf.value)
        sh.DragFinish(wp)
        on_files(paths)
        return 0

    proc = WNDPROC(proc)
    old = u.SetWindowLongPtrW(hwnd, -4, ctypes.cast(proc, P))  # GWLP_WNDPROC
    sh.DragAcceptFiles(hwnd, True)
    return proc


EMULATOR_EXES = {'Cemu': ('Cemu.exe', 'Cemu_release.exe'), 'RPCS3': ('rpcs3.exe',), 'Dolphin': ('Dolphin.exe',)}


def find_emulators():
    """{emulator: exe} found in the usual places, up to three folders deep."""
    home = os.path.expanduser('~')
    roots = [os.path.join(home, x) for x in ('Documents', 'Desktop', 'Downloads')]
    roots += [os.environ.get(x, '') for x in ('LOCALAPPDATA', 'ProgramFiles', 'ProgramFiles(x86)')]
    roots += [f'{c}:\\' for c in 'CDEFG' if os.path.isdir(f'{c}:\\')]
    found = {}
    for name, exes in EMULATOR_EXES.items():
        hits = [h for r in roots if r for exe in exes for depth in range(4)
                for h in glob.glob(os.path.join(glob.escape(r), *['*'] * depth, exe))]
        if hits:
            # the official name wins (Cemu.exe over a Cemu_release.exe hybrid build), then the newest
            found[name] = max(hits, key=lambda h: (os.path.basename(h).lower() == exes[0].lower(), os.path.getmtime(h)))
    return found


def title_of(path):
    """'Skylanders Trap Team' from 'Skylanders Trap Team [Game] [0005000010181f00]' or '... (Europe) (En,Fr).rvz'.
    For an .rpx inside a game folder, the folder's name ('.../Skylanders SuperChargers [BS5P52]/content/x.rpx')."""
    if path.lower().endswith('.rpx'):
        path = next((part for part in reversed(re.split(r'[/\\]', path)) if 'skylanders' in part.lower()), path)
    name = os.path.basename(path.rstrip('/\\'))
    name = re.sub(r'\.(wua|wud|wux|rpx|iso|wbfs|rvz|gcz|ciso|wia)$', '', name, flags=re.I)
    return re.sub(r'\s+', ' ', re.sub(r'\[.*?\]|\(.*?\)|™', '', name)).strip()


def find_games(emulators):
    """[(title, emulator, path)] of the Skylanders games each emulator knows, from its own settings:
    Cemu's GamePaths, RPCS3's games.yml, Dolphin's ISOPath folders."""
    games = []
    cemu = emulators.get('Cemu')
    if cemu:
        xml = os.path.join(os.path.dirname(cemu), 'settings.xml')
        xml = xml if os.path.exists(xml) else os.path.join(os.environ.get('APPDATA', ''), 'Cemu', 'settings.xml')
        try:
            entries = re.findall(r'<Entry>(.*?)</Entry>', open(xml, encoding='utf-8').read())
        except OSError:
            entries = []
        for e in entries:
            for path in [e] + (glob.glob(os.path.join(glob.escape(e), '*')) if os.path.isdir(e) and
                               not os.path.isdir(os.path.join(e, 'code')) else []):
                if re.search(r'\[(Update|DLC)\]', path, re.I) or 'skylanders' not in path.lower():
                    continue
                if os.path.isdir(path):
                    rpx = glob.glob(os.path.join(glob.escape(path), 'code', '*.rpx'))
                    if rpx:
                        games.append((title_of(path), 'Cemu', rpx[0]))
                elif re.search(r'\.(wua|wud|wux|rpx)$', path, re.I):
                    games.append((title_of(path), 'Cemu', path))
    rpcs3 = emulators.get('RPCS3')
    if rpcs3:
        try:
            lines = open(os.path.join(os.path.dirname(rpcs3), 'config', 'games.yml'), encoding='utf-8').read().splitlines()
        except OSError:
            lines = []
        for line in lines:
            m = re.match(r'\s*([A-Z]{4}\d{5}):\s*(.+?)\s*$', line)
            if m and 'skylanders' in m[2].lower():
                eboot = os.path.join(m[2], 'PS3_GAME', 'USRDIR', 'EBOOT.BIN')
                games.append((title_of(m[2]), 'RPCS3', eboot if os.path.exists(eboot) else m[2]))
    dolphin = emulators.get('Dolphin')
    if dolphin:
        here = os.path.dirname(dolphin)
        ini = (os.path.join(here, 'User', 'Config', 'Dolphin.ini') if os.path.exists(os.path.join(here, 'portable.txt'))
               else os.path.join(os.environ.get('APPDATA', ''), 'Dolphin Emulator', 'Config', 'Dolphin.ini'))
        try:
            folders = re.findall(r'^ISOPath\d+\s*=\s*(.+?)\s*$', open(ini, encoding='utf-8').read(), re.M)
        except OSError:
            folders = []
        for folder in folders:
            for path in glob.glob(os.path.join(glob.escape(folder), '*')):
                if 'skylanders' in path.lower() and re.search(r'\.(iso|wbfs|rvz|gcz|ciso|wia)$', path, re.I):
                    games.append((title_of(path), 'Dolphin', path))
    unique = {}
    for title, emulator, path in games:  # the same game in two folders: the first one listed
        unique.setdefault((title, emulator), path)
    return sorted((t, e, p) for (t, e), p in unique.items())


def start_game(exe, emulator, path, fullscreen=False):
    """Start an emulator with a game, and load portal_hide.dll into it at once: a game booted straight from
    the command line counts the portals sooner than one picked in the emulator's window."""
    if emulator == 'Dolphin':  # options from each emulator's --help (2026-10-17)
        args = (['-C', 'Dolphin.Display.Fullscreen=True'] if fullscreen else []) + ['-e', path]
    elif emulator == 'Cemu':
        args = (['-f'] if fullscreen else []) + ['-g', path]
    else:  # RPCS3 only goes fullscreen without its own window
        args = (['--no-gui', '--fullscreen'] if fullscreen else []) + [path]
    proc = subprocess.Popen([exe, *args], cwd=os.path.dirname(exe))
    log(f'started {os.path.basename(exe)} with {title_of(path)}')

    def hide():
        for _ in range(300):  # every 50 ms for 15 s; the 1-second watch_emulators also keeps trying
            if proc.poll() is not None:
                return
            try:
                if inject(proc.pid, HIDE_DLL):
                    return log(f'{os.path.basename(exe)} (pid {proc.pid}): real portal hidden from it (started by the bridge)')
            except OSError:
                pass
            time.sleep(0.05)
    if os.path.exists(HIDE_DLL):
        threading.Thread(target=hide, daemon=True).start()
    return proc


def dark_title(window):
    """Windows 10/11 dark title bar for a Tk window (DWMWA_USE_IMMERSIVE_DARK_MODE)."""
    try:
        on = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(int(window.wm_frame(), 16)), 20, ctypes.byref(on), 4)
    except (OSError, ValueError, AttributeError):
        pass


def take_foreground(hwnd):
    """Bring a window in front of a fullscreen game. Topmost alone is not enough: Windows keeps a background
    program from taking the foreground, and a fullscreen game (Cemu's borderless Vulkan window, 2026-10-14)
    then stays in front. Sharing the game's input queue for a moment lets SetForegroundWindow through."""
    u, k, P = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.c_void_p
    u.GetForegroundWindow.restype = P
    u.GetWindowThreadProcessId.argtypes = (P, P)
    u.SetForegroundWindow.argtypes = u.BringWindowToTop.argtypes = (P,)
    u.SetWindowPos.argtypes = (P, P, *[ctypes.c_int] * 4, ctypes.c_uint)
    front = u.GetWindowThreadProcessId(u.GetForegroundWindow(), None)
    me = k.GetCurrentThreadId()
    attached = front and front != me and u.AttachThreadInput(me, front, True)
    try:
        u.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x1 | 0x2 | 0x40)  # HWND_TOPMOST, NOSIZE|NOMOVE|SHOWWINDOW
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
    finally:
        if attached:
            u.AttachThreadInput(me, front, False)
    return u.GetForegroundWindow() == hwnd


def usbip(*args):
    r = subprocess.run([USBIP_EXE, *args], capture_output=True, text=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    out = (r.stdout + r.stderr).strip()
    log('usbip', *args, '->', r.returncode, out)
    return r.returncode, out


class App:
    # Windows 11 colours. The window uses LIGHT, or DARK when Settings has dark mode on (from the next start).
    LIGHT = dict(BG='#f3f3f3', CARD='#ffffff', LINE='#e5e5e5', TEXT='#1b1b1b', MUTED='#616161', FAINT='#8a8a8a',
                 ACCENT='#0067c0', ACCENT_HOVER='#1975c5', ACCENT_DOWN='#005ba8', SELECTED='#cce4f7', BAD='#c42b1c',
                 ODD='#fafafa', HOVER='#f5f5f5', BUTTON='#fbfbfb', PRESSED='#ededed', EDGE='#d1d1d1',
                 FIELD_EDGE='#c8c8c8', THUMB='#e0e0e0', THUMB_HOVER='#cfcfcf', DISABLED='#a0a0a0',
                 DISABLED_BG='#e0e0e0', BOX_EDGE='#8a8a8a', BOX_EDGE_HOVER='#5f5f5f')
    DARK = dict(BG='#202020', CARD='#2b2b2b', LINE='#3b3b3b', TEXT='#f2f2f2', MUTED='#b0b0b0', FAINT='#8f8f8f',
                ACCENT='#0078d4', ACCENT_HOVER='#1a86d9', ACCENT_DOWN='#006cbe', SELECTED='#204a6e', BAD='#ff99a4',
                ODD='#303030', HOVER='#383838', BUTTON='#373737', PRESSED='#2f2f2f', EDGE='#4a4a4a',
                FIELD_EDGE='#5a5a5a', THUMB='#4a4a4a', THUMB_HOVER='#5c5c5c', DISABLED='#6f6f6f',
                DISABLED_BG='#3a3a3a', BOX_EDGE='#9a9a9a', BOX_EDGE_HOVER='#c8c8c8')
    locals().update(LIGHT)  # class defaults; __init__ switches to DARK when chosen
    STATE_COLOURS = (('claimed', '#0f7b0f'), ('not plugged in', '#8a8a8a'), ('', '#c45500'))  # first match wins
    QUIET = re.compile(r'\S+ (portal [<-]|GET_DESCRIPTOR|SET_CONFIGURATION|UNLINK|portal sound: \d)')  # bridge.log only
    BAD_LINE = re.compile(r'FAILED|failed|in use|lost|error|not copied|not written|could not|did NOT', re.I)

    def __init__(self, server, mirror=None, first_start=False):
        import tkinter as tk
        from tkinter import font, ttk
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled displays
        except (AttributeError, OSError):
            pass
        self.server, self.portal, self.mirror = server, server.portal, mirror
        self.dialog, self.log_seen, self.rows, self.queue, self.done, self.current = None, 0, None, [], 0, None
        self.dropped = queue.Queue()  # files dropped on the window, loaded by refresh()
        if self.settings().get('dark'):
            self.__dict__.update(self.DARK)
        self.root = root = tk.Tk()
        root.title('PortalSync')
        root.configure(background=self.BG)
        root.minsize(760, 420)
        icon = tk.PhotoImage(width=32, height=32)  # a portal: blue ring, light centre
        for y in range(32):
            r = [(x - 15.5) ** 2 + (y - 15.5) ** 2 for x in range(32)]
            icon.put('{' + ' '.join(self.ACCENT if d < 225 and d >= 64 else '#9ccbf0' if d < 64 else self.CARD
                                    for d in r) + '}', to=(0, y))
        root.iconphoto(True, icon)
        font.nametofont('TkDefaultFont').configure(family='Segoe UI', size=10)
        self.style(ttk.Style(root), font.nametofont('TkDefaultFont').metrics('linespace'))
        if first_start:  # the welcome window first; its choice of dark mode applies to the main window at once
            root.withdraw()
            self.welcome()
            if self.settings().get('dark'):
                self.__dict__.update(self.DARK)
                root.configure(background=self.BG)
                self.style(ttk.Style(root), font.nametofont('TkDefaultFont').metrics('linespace'))
            root.deiconify()

        header = ttk.Frame(root, style='Card.TFrame', padding=(22, 16))
        header.pack(fill='x')
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text='PortalSync', style='Title.TLabel').grid(row=0, column=0, sticky='w')
        pill = ttk.Frame(header, style='Card.TFrame')
        pill.grid(row=0, column=1, rowspan=2, sticky='e')
        self.dot = ttk.Label(pill, text='●', style='Card.TLabel', font=('Segoe UI', 13))
        self.dot.pack(side='left', padx=(0, 6))
        self.real = ttk.Label(pill, style='Card.TLabel')
        self.real.pack(side='left')
        if mirror:
            ttk.Button(header, text='Settings…', command=self.open_settings).grid(
                row=0, column=2, rowspan=2, sticky='e', padx=(18, 0))
            ttk.Button(header, text='Start game…', style='Accent.TButton', command=self.open_games).grid(
                row=0, column=3, rowspan=2, sticky='e', padx=(8, 0))
        self.update_link = ttk.Label(header, style='Link.TLabel', cursor='hand2')  # shown by refresh()
        self.new_version = None  # (tag, url), set by the update check thread
        if self.settings().get('check_updates', True):
            threading.Thread(target=lambda: setattr(self, 'new_version', (latest_release() or (None,))), daemon=True).start()
        tk.Frame(root, background=self.LINE, height=1).pack(fill='x')

        body = ttk.Frame(root, padding=(16, 16, 16, 0))
        body.pack(fill='both', expand=True)
        portal = self.card(body, fill='both', expand=True)
        top = ttk.Frame(portal, style='Card.TFrame')
        top.pack(fill='x')
        ttk.Label(top, text='Virtual portal', style='Section.TLabel').pack(side='left')
        ttk.Button(top, text='Add Skylander…', command=self.open_picker).pack(side='right')

        cols = (('slot', 'SLOT', 60), ('figure', 'FIGURE', 190), ('details', 'DETAILS', 150), ('source', 'SOURCE', 170),
                ('file', 'FILE', 260))
        self.detail_cache = {}  # figure image -> 'level 11 · 2,594 gold' (decrypting every 200 ms is wasteful)
        table = ttk.Frame(portal, style='Card.TFrame')
        table.pack(fill='both', expand=True, pady=(12, 0))
        self.tree = ttk.Treeview(table, columns=[c[0] for c in cols], show='headings', height=8)
        for key, title, width in cols:
            self.tree.heading(key, text=title, anchor='w')
            self.tree.column(key, width=width, minwidth=width, anchor='w', stretch=key == 'file')
        self.tree.tag_configure('odd', background=self.ODD)
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<Button-3>', self.figure_menu)  # right-click: what you can do with that figure
        self.placeholder = ttk.Label(table, style='CardMuted.TLabel', justify='center',
                                     text='No figures yet.\nPlace one on the real portal, or load a .sky file.')
        # the choices on the Settings page (open_settings), kept in settings.json
        self.wb = tk.BooleanVar(value=self.settings().get('write_back', True))  # on unless turned off
        self.volume = tk.IntVar(value=self.settings().get('volume', 100))
        self.show_log = tk.BooleanVar(value=self.settings().get('show_log', True))
        if mirror:
            mirror.set_write_back(self.wb.get())
            log('write-back to real figures ' + ('ON' if self.wb.get() else 'off'))
        self.set_volume()
        if getattr(server, 'sound', None):
            server.sound.mode = self.settings().get('sound', 'portal')
        if mirror:
            mirror.lights = self.settings().get('lights', True)

        activity = self.card(body, fill='x', pady=(12, 0))
        self.activity = activity.master  # the card's border frame: hidden when the log is switched off
        ttk.Label(activity, text='Activity', style='Section.TLabel').pack(anchor='w')
        logbox = ttk.Frame(activity, style='Card.TFrame')
        logbox.pack(fill='x', pady=(8, 0))
        self.log = tk.Text(logbox, height=8, wrap='word', font=('Consolas', 9), relief='flat', borderwidth=0,
                           background=self.CARD, foreground=self.TEXT, highlightthickness=0, state='disabled',
                           selectbackground=self.SELECTED, selectforeground=self.TEXT)
        self.log.tag_configure('time', foreground=self.FAINT)
        self.log.tag_configure('bad', foreground=self.BAD)
        scroll = ttk.Scrollbar(logbox, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.log.pack(side='left', fill='both', expand=True)
        self.status = ttk.Label(root, style='Muted.TLabel', padding=(20, 8, 20, 10))
        self.status.pack(fill='x')
        if not self.show_log.get():
            self.activity.pack_forget()
        if self.settings().get('dark'):  # dark title bars too, for this window and every one it opens
            root.update_idletasks()
            dark_title(root)
            root.bind_class('Toplevel', '<Map>', lambda e: dark_title(e.widget), add='+')
        if not self.settings().get('tip_shown'):
            self.status.configure(text='Tip: right-click a figure to unload, edit, repair, reset or restore it.')
            self.remember(tip_shown=True)
        self.refresh()
        root.after(300, self.log.see, 'end')  # see() before the window is drawn does nothing
        root.update_idletasks()
        self.drop_proc = accept_drops(int(root.wm_frame(), 16), lambda paths: [self.dropped.put(p) for p in paths])

    def style(self, s, line):
        s.theme_use('clam')  # the only built-in theme whose colours can all be set
        C, A, flat = self.CARD, self.ACCENT, lambda c: {'background': c, 'lightcolor': c, 'darkcolor': c}
        s.configure('.', background=self.BG, foreground=self.TEXT, font=('Segoe UI', 10), bordercolor=self.LINE,
                    focuscolor=A, troughcolor=C)
        s.configure('Card.TFrame', background=C)
        s.configure('Card.TLabel', background=C)
        s.configure('CardMuted.TLabel', background=C, foreground=self.MUTED)
        s.configure('Muted.TLabel', foreground=self.MUTED)
        s.configure('Title.TLabel', background=C, font=('Segoe UI Semibold', 17))
        s.configure('Section.TLabel', background=C, font=('Segoe UI Semibold', 12))
        s.configure('Figure.TLabel', background=C, font=('Segoe UI Semibold', 15))
        s.configure('TButton', padding=(14, 5), bordercolor=self.EDGE, **flat(self.BUTTON))
        s.map('TButton', **{k: [('pressed', self.PRESSED), ('active', self.HOVER)] for k in flat('')})
        s.configure('Small.TButton', padding=(6, 0), width=0)  # as tall as an entry box and no wider than its text
        s.configure('Accent.TButton', foreground='white', bordercolor=A, **flat(A))
        accent_states = [('disabled', self.DISABLED_BG), ('pressed', self.ACCENT_DOWN), ('active', self.ACCENT_HOVER)]
        s.map('Accent.TButton', foreground=[('disabled', self.DISABLED)],
              **{k: accent_states for k in ('bordercolor', *flat(''))})
        self.box = round(line * 1.15)  # Windows 11 checkbox: clam's own is a tiny box with an X
        self.box_images = [self.checkbox(self.box, on, hover) for on in (False, True) for hover in (False, True)]
        off, off_hover, on, on_hover = self.box_images
        box = 'BoxDark.indicator' if self.BG == self.DARK['BG'] else 'Box.indicator'  # style() runs again after the welcome
        if box not in s.element_names():
            s.element_create(box, 'image', off, ('selected', 'active', on_hover), ('selected', on),
                             ('active', off_hover), width=self.box + 10, sticky='w')
        s.layout('Card.TCheckbutton', [('Checkbutton.padding', {'sticky': 'nswe', 'children': [
            (box, {'side': 'left', 'sticky': ''}),
            ('Checkbutton.focus', {'side': 'left', 'sticky': 'w', 'children': [('Checkbutton.label', {'sticky': 'nswe'})]})]})])
        s.configure('Card.TCheckbutton', background=C)
        s.map('Card.TCheckbutton', background=[('active', C)])
        s.configure('Treeview', fieldbackground=C, rowheight=int(line * 2.1), bordercolor=C, **flat(C))
        s.map('Treeview', background=[('selected', self.SELECTED)], foreground=[('selected', self.TEXT)])
        s.configure('Treeview.Heading', foreground=self.MUTED, font=('Segoe UI Semibold', 9), relief='flat',
                    padding=(6, 4), bordercolor=C, **flat(C))
        s.map('Treeview.Heading', background=[('active', C)])
        s.layout('Treeview', [('Treeview.treearea', {'sticky': 'nswe'})])  # no frame around the rows
        s.configure('TScrollbar', arrowcolor=self.MUTED, bordercolor=C, gripcount=0, arrowsize=12, **flat(self.THUMB))
        s.map('TScrollbar', **{k: [('active', self.THUMB_HOVER)] for k in flat('')})
        for kind in ('TEntry', 'TSpinbox'):
            s.configure(kind, fieldbackground=C, foreground=self.TEXT, insertcolor=self.TEXT, bordercolor=self.FIELD_EDGE,
                        lightcolor=C, darkcolor=C, arrowcolor=self.MUTED, background=self.BUTTON)
        s.configure('Search.TEntry', fieldbackground=C, bordercolor=self.FIELD_EDGE, lightcolor=C, darkcolor=C, padding=(10, 7))
        s.map('Search.TEntry', bordercolor=[('focus', A)], lightcolor=[('focus', A)])
        s.configure('Link.TLabel', background=C, foreground=A, font=('Segoe UI', 10, 'underline'))
        s.configure('Hint.TLabel', background=C, foreground=self.FAINT, font=('Segoe UI', 11))
        s.layout('Tab.TRadiobutton', [('Radiobutton.padding', {'sticky': 'nswe', 'children': [
            ('Radiobutton.label', {'sticky': 'nswe'})]})])
        s.configure('Tab.TRadiobutton', background=C, foreground=self.MUTED, padding=(12, 8), font=('Segoe UI', 10))
        s.map('Tab.TRadiobutton', foreground=[('selected', A), ('active', self.TEXT)],
              font=[('selected', ('Segoe UI Semibold', 10))], background=[('active', self.HOVER), ('selected', C)])

    def checkbox(self, n, on, hover):
        """An n x n antialiased checkbox image on the card colour: rounded box, white tick when on."""
        import tkinter as tk
        rgb = lambda c: [int(c[i:i + 2], 16) for i in (1, 3, 5)]
        fill = rgb(self.ACCENT_HOVER if hover else self.ACCENT) if on else rgb(self.HOVER if hover else self.CARD)
        edge = fill if on else rgb(self.BOX_EDGE_HOVER if hover else self.BOX_EDGE)
        card, white, r, bw = rgb(self.CARD), rgb('#ffffff'), n * 0.22, max(1.0, n / 18)
        tick = [(0.26, 0.52), (0.43, 0.68), (0.75, 0.35)]

        def seg(px, py, a, b):  # distance from (px, py) to segment ab
            (ax, ay), (bx, by) = a, b
            t = max(0, min(1, ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / ((bx - ax) ** 2 + (by - ay) ** 2)))
            return ((px - ax - t * (bx - ax)) ** 2 + (py - ay - t * (by - ay)) ** 2) ** 0.5

        img = tk.PhotoImage(width=n, height=n)
        for y in range(n):
            row = []
            for x in range(n):
                acc = [0.0, 0.0, 0.0]
                for sy in range(4):
                    for sx in range(4):
                        px, py = x + (sx + 0.5) / 4, y + (sy + 0.5) / 4
                        qx, qy = abs(px - n / 2) - (n / 2 - 0.5 - r), abs(py - n / 2) - (n / 2 - 0.5 - r)
                        d = (max(qx, 0) ** 2 + max(qy, 0) ** 2) ** 0.5 + min(max(qx, qy), 0) - r  # rounded box
                        c = (card if d > 0 else edge if d > -bw else
                             white if on and min(seg(px / n, py / n, tick[0], tick[1]),
                                                 seg(px / n, py / n, tick[1], tick[2])) < 0.07 else fill)
                        acc = [a + v / 16 for a, v in zip(acc, c)]
                row.append('#%02x%02x%02x' % tuple(round(v) for v in acc))
            img.put('{' + ' '.join(row) + '}', to=(0, y))
        return img

    def card(self, parent, **pack):
        import tkinter as tk
        from tkinter import ttk
        edge = tk.Frame(parent, background=self.LINE)  # 1 px border
        edge.pack(**pack)
        inner = ttk.Frame(edge, style='Card.TFrame', padding=(18, 14))
        inner.pack(fill='both', expand=True, padx=1, pady=1)
        return inner

    def refresh(self):  # also called every 200 ms: the mirror and server threads change things too
        m = self.mirror
        with self.server.cv:
            real = {v: i for i, (v, path, uid) in m.slots.items() if self.portal.slots[v].path == path} if m else {}
            kept = {v for v, path in m.kept.values() if self.portal.slots[v].path == path} if m else set()
            images = {n: bytes(s.data) for n, s in enumerate(self.portal.slots) if s.path}
            rows = {n: (n + 1, figure_name(s.data), '',
                        f'Real portal, slot {real[n] + 1}' if n in real else
                        'Kept (real portal unplugged)' if n in kept else 'File', os.path.basename(s.path))
                    for n, s in enumerate(self.portal.slots) if s.path}
        for n, image in images.items():  # outside the lock: decrypting takes a moment
            if image not in self.detail_cache:
                if len(self.detail_cache) > 64:
                    self.detail_cache.clear()
                self.detail_cache[image] = stats(figure_info(image))[3:]
            rows[n] = rows[n][:2] + (self.detail_cache[image],) + rows[n][3:]
        if rows != self.rows:
            self.fit_columns(self.tree, list(rows.values()), self.root, extra=70)
            picked = self.tree.selection()
            self.tree.delete(*self.tree.get_children())
            for k, (n, values) in enumerate(rows.items()):
                self.tree.insert('', 'end', iid=str(n), values=values, tags=('odd',) if k % 2 else ())
            self.tree.selection_set([i for i in picked if self.tree.exists(i)])
            if rows:
                self.placeholder.place_forget()
            else:
                self.placeholder.place(relx=0.5, rely=0.55, anchor='center')
            self.rows = rows
        while not self.dropped.empty():  # files dropped on the window (see accept_drops)
            self.load(self.dropped.get_nowait())
        if self.new_version and self.new_version[0] and newer(self.new_version[0]) and not self.update_link.winfo_ismapped():
            tag, url = self.new_version
            self.update_link.configure(text=f'PortalSync {tag.lstrip("v")} is available')
            self.update_link.bind('<Button-1>', lambda e: __import__('webbrowser').open(url))
            self.update_link.grid(row=2, column=0, columnspan=2, sticky='w', pady=(6, 0))
            log(f'PortalSync {tag} is available (this is {VERSION}): {url}')
        if m:
            colour = next(c for key, c in self.STATE_COLOURS if key in m.state)
            self.dot.configure(foreground=colour)
            self.real.configure(text=self.short_state(m.state) + m.sync_status())
            while not m.offers.empty():
                offer = m.offers.get_nowait()
                if self.settings().get('restore_popup', True):
                    self.open_restore(select=offer)
                else:
                    log(f'{os.path.basename(offer)} has progress the figure lacks: see "Restore progress…" in Settings')
            while not m.notices.empty():
                self.alert(m.notices.get_nowait())
            while not m.repairs.empty():
                self.offer_repair(m.repairs.get_nowait())
        lines = list(LOG_LINES)
        new = [line for n, line in lines if n > self.log_seen and not self.QUIET.match(line)]
        if lines:
            self.log_seen = lines[-1][0]
        if new:
            at_end = self.log.yview()[1] >= 0.999
            self.log.configure(state='normal')
            for line in new:
                self.log.insert('end', line[:8], 'time', line[8:] + '\n', 'bad' if self.BAD_LINE.search(line) else ())
            self.log.delete('1.0', f'end-{LOG_LINES.maxlen + 1}l')
            self.log.configure(state='disabled')
            if at_end:
                self.log.see('end')

    def offer_repair(self, uid):
        data = self.mirror.figure_data(uid)
        if data is None:  # lifted again already
            return
        name, info = figure_name(data), figure_info(data)
        if self.alert(f'The save on {name} is damaged, so the game will say "A toy on the portal has a problem".\n\n'
                      f'Repair it now? Its newest save gets valid checksums again and it keeps all its progress{stats(info)}. '
                      'What it holds now is kept as its backup.', ask=True):
            self.write(uid, repaired, 'a repair', f'Repairing {name} ({uid}); see the log.')

    def alert(self, msg, ask=False):
        """A warning (ask: a yes/no question, answered True for yes) in front of everything, a fullscreen game
        included; the game gets the focus back after."""
        import tkinter as tk
        from tkinter import messagebox
        ctypes.windll.user32.GetForegroundWindow.restype = ctypes.c_void_p
        game = ctypes.windll.user32.GetForegroundWindow()
        holder = tk.Toplevel(self.root)  # invisible topmost owner, so the message box opens on top
        holder.attributes('-alpha', 0.0)
        holder.attributes('-topmost', True)
        holder.geometry(f'1x1+{holder.winfo_screenwidth() // 2}+{holder.winfo_screenheight() // 3}')
        holder.update_idletasks()
        take_foreground(int(holder.wm_frame(), 16))
        answer = (messagebox.askyesno if ask else messagebox.showwarning)('PortalSync', msg, parent=holder)
        holder.destroy()
        if game:
            take_foreground(game)
        return answer

    @staticmethod
    def settings():
        try:
            with open(SETTINGS, encoding='utf-8-sig') as f:  # also when saved by Notepad (BOM)
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def say(self, msg):
        log(msg)
        self.status.configure(text=msg)

    def open_restore(self, select=None):
        """Pop-up that offers backups for the figures on the real portal one figure at a time, above every other
        window (a game may be in front). Opens by itself when a placed figure differs from its backup
        (Mirror.offers, select = that backup); the button lists every backup that differs from its figure."""
        copies = [c for c in self.mirror.saved_copies() if (c[0] == select if select else c[3])]
        seen = set()  # with several backups per figure: its newest only (saved_copies lists newest first)
        copies = [c for c in copies if not (c[1] in seen or seen.add(c[1]))]
        if select and not copies:  # figure lifted again already
            return
        if self.dialog is not None and self.dialog.winfo_exists():
            for c in copies:  # one entry per figure: a newer backup replaces a queued older one
                self.queue = [q for q in self.queue if q[1] != c[1]]
                if self.current and self.current[1] == c[1]:
                    self.current = c
                    self.show(c)
                else:
                    self.queue.append(c)
            self.update_counter()
            return self.raise_dialog()
        self.queue, self.done, self.current = copies, 0, None
        self.build_dialog()
        self.next_figure(first=True)

    def build_dialog(self):
        import tkinter as tk
        from tkinter import ttk
        d = self.dialog = tk.Toplevel(self.root)  # not transient: it must show while the main window is minimised
        d.title('Restore saved progress')
        d.configure(background=self.CARD)
        d.resizable(False, False)
        d.attributes('-topmost', True)
        body = ttk.Frame(d, style='Card.TFrame', padding=(24, 20, 24, 0))
        body.pack(fill='both', expand=True)
        head = ttk.Frame(body, style='Card.TFrame')
        head.pack(fill='x')
        ttk.Label(head, text='Restore saved progress', style='Section.TLabel').pack(side='left')
        self.counter = ttk.Label(head, style='CardMuted.TLabel')
        self.counter.pack(side='right')
        self.figure_label = ttk.Label(body, style='Figure.TLabel')
        self.figure_label.pack(anchor='w', pady=(16, 0))
        self.uid_label = ttk.Label(body, style='CardMuted.TLabel')
        self.uid_label.pack(anchor='w')
        facts = ttk.Frame(body, style='Card.TFrame')
        facts.pack(fill='x', pady=(14, 0))
        self.facts = []
        for row, name in enumerate(('Backup', 'On the figure', 'Differs from figure')):
            ttk.Label(facts, text=name, style='CardMuted.TLabel').grid(row=row, column=0, sticky='w', pady=2)
            value = ttk.Label(facts, style='Card.TLabel')
            value.grid(row=row, column=1, sticky='w', padx=(16, 0), pady=2)
            self.facts.append(value)
        self.explain = ttk.Label(body, style='CardMuted.TLabel', wraplength=440, justify='left')
        self.explain.pack(anchor='w', pady=(14, 20))
        tk.Frame(d, background=self.LINE, height=1).pack(fill='x')
        bar = ttk.Frame(d, padding=(24, 14))
        bar.pack(fill='x')
        ttk.Button(bar, text='Other file…', command=self.restore_other).pack(side='left')
        self.write_button = ttk.Button(bar, text='Write to figure', style='Accent.TButton', command=self.restore_current)
        self.write_button.pack(side='right')
        self.skip_button = ttk.Button(bar, command=self.next_figure)
        self.skip_button.pack(side='right', padx=(0, 8))
        d.bind('<Return>', lambda e: self.restore_current())
        d.bind('<Escape>', lambda e: d.destroy())

    def show(self, copy):
        path, label, when, n = copy
        name, uid = label.rsplit(' (', 1)
        self.figure_label.configure(text=name)
        self.uid_label.configure(text=f'Figure ID {uid[:-1]}')
        try:
            with open(path, 'rb') as f:
                backup = figure_info(f.read())
        except OSError:
            backup = SimpleNamespace(kind='other', damaged=False)
        real = self.mirror.figure_data(uid[:-1])
        real = figure_info(real) if real else None
        self.facts[0].configure(text=when + stats(backup) + (' (damaged save)' if backup.damaged else ''))
        self.facts[1].configure(text=('damaged save' if real.damaged else (stats(real)[3:] or 'nothing saved yet'))
                                if real else 'not on the real portal')
        self.facts[2].configure(text=f'{n} block{"s" * (n != 1)}' if n else 'nothing (same as the figure)')
        self.explain.configure(text=(
            "This backup's newest save is damaged: the game would refuse the figure, so it can't be written." if backup.damaged else
            'The save on the figure is damaged, so the game says "A toy on the portal has a problem". Writing this '
            'backup fixes it. What the figure holds now is kept as its backup.' if real and real.damaged else
            'This backup holds progress the figure does not have. Writing it replaces the progress on the figure. '
            'What the figure holds now is kept as its backup, so you can undo this here.'))
        self.write_button.state(['!disabled'] if n and not backup.damaged else ['disabled'])

    def next_figure(self, first=False):  # Skip, Close, or after a restore
        if not first:
            self.done += self.current is not None
            if not self.queue:
                return self.dialog.destroy()
        self.current = self.queue.pop(0) if self.queue else None
        if self.current:
            self.show(self.current)
        else:  # opened with the button, nothing to offer: only "Other file…"
            self.figure_label.configure(text='Nothing to restore')
            self.uid_label.configure(text='')
            for value in self.facts:
                value.configure(text='—')
            self.explain.configure(text='No backup differs from the figures on the real portal. '
                                        'To write another dump, use "Other file…".')
            self.write_button.state(['disabled'])
        self.update_counter()
        self.raise_dialog()

    def update_counter(self):
        total = self.done + (self.current is not None) + len(self.queue)
        self.counter.configure(text=f'Figure {self.done + 1} of {total}' if total > 1 else '')
        self.skip_button.configure(text='Skip' if self.queue else 'Close')

    def raise_dialog(self):
        d = self.dialog
        d.update_idletasks()
        if not hasattr(d, 'centred'):  # first show: centre on the screen, the main window may be hidden
            d.centred = True
            d.geometry(f'+{(d.winfo_screenwidth() - d.winfo_reqwidth()) // 2}'
                       f'+{(d.winfo_screenheight() - d.winfo_reqheight()) // 3}')
        d.deiconify()
        d.lift()
        d.update_idletasks()
        hwnd = int(d.wm_frame(), 16)
        ctypes.windll.user32.GetForegroundWindow.restype = ctypes.c_void_p
        game = ctypes.windll.user32.GetForegroundWindow()
        if game and game != hwnd and not hasattr(d, 'back_to'):  # the game gets the focus back when it closes
            d.back_to = game
            d.bind('<Destroy>', lambda e: e.widget is d and take_foreground(d.back_to))
        take_foreground(hwnd)
        d.focus_force()
        d.bell()

    def restore_current(self):
        if self.current and self.current[3]:
            self.restore_file(self.current[0])

    def restore_other(self):
        from tkinter import filedialog
        types = [('Skylander dumps', '*.sky'), ('All files', '*.*')]
        if self.current:  # this figure's dumps first
            types.insert(0, (f'{self.current[1]} dumps', f'*{self.current[1][-10:]}*.sky'))
        path = filedialog.askopenfilename(parent=self.dialog, initialdir=DUMPS, filetypes=types)
        if path:
            self.restore_file(path)

    def restore_file(self, path):
        from tkinter import messagebox
        name = os.path.basename(path)
        try:
            r = self.mirror.compare(path)
        except OSError as e:
            return messagebox.showerror('Not restored', str(e), parent=self.dialog)
        if r is None:
            return messagebox.showerror('Not restored', f'{name} is not a dump of a figure on the real portal. '
                                        'Place the figure first.', parent=self.dialog)
        uid, n = r
        if not n:
            return messagebox.showinfo('Nothing to write', f'{name} is the same as figure {uid}.', parent=self.dialog)
        if damaged(path):
            return messagebox.showerror('Not restored', f"{name}'s newest save is damaged: the game would refuse the "
                                        'figure, so it is not written.', parent=self.dialog)
        if not messagebox.askyesno('Write to figure?', (
                f'Write {n} block(s) from {name} to figure {uid}?\n\n'
                'This replaces the progress on the figure. What the figure holds now becomes its backup '
                f'("... ({uid}) - before restore <time>.sky"); its older backup goes to the Recycle Bin.'),
                icon='warning', parent=self.dialog):
            return
        try:
            self.mirror.restore(path)
        except (OSError, ValueError) as e:
            return messagebox.showerror('Not restored', str(e), parent=self.dialog)
        self.say(f'Restoring {name} to figure {uid}; see the log.')
        self.next_figure()

    def toggle_write_back(self):
        n = self.mirror.set_write_back(self.wb.get())
        self.remember(write_back=self.wb.get())
        self.say('write-back to real figures ' + ('ON' if self.wb.get() else 'off')
                 + (f': catching up {n} block(s) saved earlier' if n else ''))

    def load(self, path):
        try:
            path, data = open_dump(path)
        except (OSError, ValueError) as e:
            return self.say(f'Not loaded: {e}')
        with self.server.cv:
            if any(s.path and os.path.normcase(s.path) == os.path.normcase(path) for s in self.portal.slots):
                return self.say(f'{os.path.basename(path)} is already on the portal')
            n = self.portal.load(data, path)
        self.say(f'{os.path.basename(path)} -> slot {n + 1}' if n is not None else 'Portal full')
        self.refresh()

    def ask_load(self):
        from tkinter import filedialog
        for p in filedialog.askopenfilenames(initialdir=DUMPS, filetypes=[('Skylander dumps', '*.sky'), ('All files', '*.*')]):
            self.load(p)

    def open_picker(self):
        """In-app window to add a figure: the figures in dumps/ (current copy or backup) or a new blank
        figure of any Skylander in figure_names.json. The file explorer is only behind "Browse files…"."""
        import tkinter as tk
        from tkinter import ttk
        if getattr(self, 'picker', None) is not None and self.picker.winfo_exists():
            return self.picker.lift()
        self.yours = {e[1]: e for e in played()}
        self.health = {}  # uid -> (figure_info of the current copy or None, backup damaged)
        self.owned = set()  # "id:variant" of every figure in dumps/
        for name, uid, current, backup, when in self.yours.values():
            try:
                with open(current or backup, 'rb') as f:
                    head = f.read(32)
                self.owned.add(f'{struct.unpack_from("<H", head, 0x10)[0]}:{struct.unpack_from("<H", head, 0x1C)[0]}')
            except (OSError, TypeError, struct.error):
                pass
        for name, uid, current, backup, when in self.yours.values():
            try:
                with open(current, 'rb') as f:
                    info = figure_info(f.read())
            except (OSError, TypeError):
                info = None
            self.health[uid] = (info, bool(backup) and damaged(backup))
        p = self.picker = tk.Toplevel(self.root)
        p.title('Add a Skylander')
        p.configure(background=self.CARD)
        p.transient(self.root)
        p.minsize(640, 480)
        body = ttk.Frame(p, style='Card.TFrame', padding=(22, 18, 22, 14))
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='Add a Skylander', style='Section.TLabel').pack(anchor='w')

        self.query = tk.StringVar()
        box = ttk.Frame(body, style='Card.TFrame')
        box.pack(fill='x', pady=(12, 10))
        search = ttk.Entry(box, textvariable=self.query, style='Search.TEntry', font=('Segoe UI', 11))
        search.pack(fill='x')
        hint = ttk.Label(box, text='Search Skylanders', style='Hint.TLabel')
        hint.place(x=12, rely=0.5, anchor='w')
        hint.bind('<Button-1>', lambda e: search.focus_set())

        def typed(*_):
            if self.query.get():
                hint.place_forget()
            else:
                hint.place(x=12, rely=0.5, anchor='w')
            self.fill_picker()
        self.query.trace_add('write', typed)

        self.tab = tk.StringVar(value='yours' if self.yours else 'all')
        tabs = ttk.Frame(body, style='Card.TFrame')
        tabs.pack(fill='x')
        self.tab_buttons = {}
        for key in ('yours', 'all'):
            self.tab_buttons[key] = ttk.Radiobutton(tabs, variable=self.tab, value=key, style='Tab.TRadiobutton',
                                                    command=self.switch_tab)
            self.tab_buttons[key].pack(side='left', padx=(0, 4))
        self.summary = ttk.Label(body, style='CardMuted.TLabel', wraplength=740)  # All Skylanders: what you have per game
        self.summary_anchor = tk.Frame(body, background=self.LINE, height=1)
        self.summary_anchor.pack(fill='x')

        table = ttk.Frame(body, style='Card.TFrame')
        table.pack(fill='both', expand=True, pady=(6, 0))
        self.lists = {}
        for key, cols in (('yours', (('figure', 'FIGURE', 200), ('villain', 'VILLAIN', 120), ('level', 'LEVEL', 55),
                                     ('gold', 'GOLD', 70), ('uid', 'ID', 85), ('played', 'LAST PLAYED', 125),
                                     ('backup', 'BACKUP', 170))),
                          ('all', (('figure', 'FIGURE', 280), ('game', 'GAME', 140), ('element', 'ELEMENT', 90),
                                   ('owned', 'YOURS', 60)))):
            t = ttk.Treeview(table, columns=[c[0] for c in cols], show='headings', selectmode='browse')
            for c, title, width in cols:
                t.heading(c, text=title, anchor='w')
                t.column(c, width=width, minwidth=60, anchor='w', stretch=c == 'figure')
            t.tag_configure('odd', background=self.ODD)
            t.tag_configure('loaded', foreground=self.FAINT)
            t.tag_configure('broken', foreground=self.BAD)
            t.bind('<<TreeviewSelect>>', lambda e: self.picker_buttons())
            t.bind('<Double-1>', lambda e: self.pick('current' if self.tab.get() == 'yours' else 'new'))
            self.lists[key] = t
        self.pick_scroll = ttk.Scrollbar(table)
        self.pick_scroll.pack(side='right', fill='y')
        self.nothing = ttk.Label(table, style='CardMuted.TLabel')

        bar = ttk.Frame(p, padding=(22, 12))  # packed before the list, so the buttons always keep their room
        bar.pack(fill='x', side='bottom', before=body)
        tk.Frame(p, background=self.LINE, height=1).pack(fill='x', side='bottom', before=body)
        ttk.Button(bar, text='Browse files…', command=lambda: (p.destroy(), self.ask_load())).pack(side='left')
        self.actions = {'yours': ttk.Frame(bar), 'all': ttk.Frame(bar)}
        self.pick_current = ttk.Button(self.actions['yours'], text='Load current', style='Accent.TButton',
                                       command=lambda: self.pick('current'))
        self.pick_current.pack(side='right')
        self.pick_backup = ttk.Button(self.actions['yours'], text='Load backup', command=lambda: self.pick('backup'))
        self.pick_backup.pack(side='right', padx=(0, 8))
        self.pick_new = ttk.Button(self.actions['all'], text='Create new figure', style='Accent.TButton',
                                   command=lambda: self.pick('new'))
        self.pick_new.pack(side='right')
        p.bind('<Return>', lambda e: self.pick('current' if self.tab.get() == 'yours' else 'new'))
        p.bind('<Escape>', lambda e: p.destroy())
        self.switch_tab()
        sw, sh = p.winfo_screenwidth(), p.winfo_screenheight()
        w, h = min(max(780, p.winfo_reqwidth()), sw - 40), 600  # wide enough for the fitted columns
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2  # centred over the main window,
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - h) // 3)
        p.geometry(f'{w}x{h}+{min(max(0, x), sw - w)}+{min(max(0, y), sh - h)}')  # but on the screen
        search.focus_set()

    def fit_columns(self, tree, rows, window, extra):
        """Size each column to its widest text (heading included), so nothing is cut off, and widen the
        window when the columns need more room (up to the screen). extra: the window's own margins."""
        from tkinter import font
        body, head = font.nametofont('TkDefaultFont'), font.Font(family='Segoe UI Semibold', size=9)
        total = 0
        for k, col in enumerate(tree['columns']):
            w = max([head.measure(tree.heading(col, 'text'))] + [body.measure(str(r[k])) for r in rows]) + 24
            tree.column(col, width=w, minwidth=w)
            total += w
        window.update_idletasks()
        need = min(total + extra, window.winfo_screenwidth() - 40)
        if window.winfo_width() < need:
            x = max(0, min(window.winfo_x() - (need - window.winfo_width()) // 2, window.winfo_screenwidth() - need))
            height = max(window.winfo_height(), window.winfo_reqheight())  # never shorter than its contents
            window.geometry(f'{need}x{height}+{x}+{window.winfo_y()}')

    def switch_tab(self):
        for key, t in self.lists.items():
            t.pack_forget()
            self.actions[key].pack_forget()
        t = self.lists[self.tab.get()]
        t.pack(side='left', fill='both', expand=True)
        t.configure(yscrollcommand=self.pick_scroll.set)
        self.pick_scroll.configure(command=t.yview)
        self.actions[self.tab.get()].pack(side='right')
        self.fill_picker()

    def fill_picker(self):
        q = self.query.get().strip().lower()
        with self.server.cv:
            loaded = {bytes(s.data[:4]).hex() for s in self.portal.slots if s.path}
        if self.tab.get() == 'yours':
            rows = []
            for name, uid, current, backup, when in self.yours.values():
                kind = os.path.basename(backup)[:-4].split(') - ', 1)[1] if backup else 'none'
                kind = re.sub(r'(\d\d)\.(\d\d)\.\d\d(-\d+)?$', r'\1:\2', kind)  # 'saved 2026-10-11 13:12'
                info, backup_bad = self.health[uid]
                label = name + (f' "{info.nickname}"' if info and info.nickname else '')
                label += ' (damaged save)' if info and info.damaged else ' (on the portal)' if uid in loaded else ''
                rows.append((uid, (label, info.villains[0] if info and info.villains else '', info.level if info and info.level else '', f'{info.gold:,}' if info and info.gold is not None else '',
                                   uid, time.strftime('%Y-%m-%d %H:%M', time.localtime(when)),
                                   kind + (' (damaged)' if backup_bad else '')), uid in loaded))
        else:
            rows = [(key, (name, *DETAILS.get(key, ('', '')), '✓' if key in self.owned else ''), False)
                    for key, name in sorted(NAMES.items(), key=lambda kv: kv[1].lower())]
        shown = [r for r in rows if not q or q in r[0] or any(q in str(v).lower() for v in r[1][:3])]
        t = self.lists[self.tab.get()]
        if not getattr(t, 'fitted', False):
            t.fitted = True
            self.fit_columns(t, [r[1] for r in rows], self.picker, extra=60)
        t.delete(*t.get_children())
        for n, (iid, values, on) in enumerate(shown):
            info = self.health.get(iid, (None,))[0] if self.tab.get() == 'yours' else None
            tags = (('odd',) if n % 2 else ()) + (('loaded',) if on else ()) + (('broken',) if info and info.damaged else ())
            t.insert('', 'end', iid=iid, values=values, tags=tags)
        if shown:
            t.selection_set(shown[0][0])
            self.nothing.place_forget()
        else:
            self.nothing.configure(text=f"No Skylander matches '{self.query.get().strip()}'." if q else
                                   'No figures yet. Place one on the real portal, or create one under All Skylanders.')
            self.nothing.place(relx=0.5, rely=0.4, anchor='center')
        if self.tab.get() == 'all':  # how much of each game's figures you have
            games = {}
            for key, (game, _) in DETAILS.items():
                games.setdefault(game, [0, 0])[1] += 1
                games[game][0] += key in self.owned
            self.summary.configure(text=f'You have {sum(v[0] for v in games.values())} of {len(DETAILS)}:  ' + '   '.join(
                f'{g} {have}/{total}' for g, (have, total) in games.items()))
            self.summary.pack(anchor='w', pady=(6, 0), before=self.summary_anchor)
        else:
            self.summary.pack_forget()
        self.tab_buttons['yours'].configure(text=f'Your Skylanders ({len(self.yours)})')
        self.tab_buttons['all'].configure(text=f'All Skylanders ({len(NAMES)})')
        self.picker_buttons()

    def picker_buttons(self):
        sel = self.lists[self.tab.get()].selection()
        e = self.yours.get(sel[0]) if sel and self.tab.get() == 'yours' else None
        on = e is not None and 'loaded' in self.lists['yours'].item(e[1], 'tags')
        info, backup_bad = self.health.get(e[1], (None, False)) if e else (None, False)
        self.pick_current.state(['!disabled'] if e and e[2] and not on and not (info and info.damaged) else ['disabled'])
        self.pick_backup.state(['!disabled'] if e and e[3] and not on and not backup_bad else ['disabled'])
        self.pick_new.state(['!disabled'] if sel and self.tab.get() == 'all' else ['disabled'])

    def pick(self, what):
        sel = self.lists[self.tab.get()].selection()
        if not sel:
            return
        if what == 'new':
            fid, var = map(int, sel[0].split(':'))
            data = new_figure(fid, var)
            path = dump_path(data, 'current')
            try:
                os.makedirs(DUMPS, exist_ok=True)
                save_dump(path, data)
            except OSError as e:
                return self.say(f'Not created: {e}')
            log(f'created new figure {figure_name(data)} ({data[:4].hex()})')
        else:
            e = self.yours[sel[0]]
            path = e[2] if what == 'current' else e[3]
            if not path or 'loaded' in self.lists['yours'].item(sel[0], 'tags'):
                return
        self.picker.destroy()
        self.load(path)

    def popup(self, x, y, items):
        """A menu in the window's own style at screen position x, y (Windows' own menus can't be styled).
        items: (label, command, enabled) tuples, None for a line between groups. It closes on a choice, on
        Escape, and on a click anywhere else."""
        import tkinter as tk
        if getattr(self, 'menu', None) is not None and self.menu.winfo_exists():
            self.menu.destroy()
        m = self.menu = tk.Toplevel(self.root, background=self.LINE)  # the 1 px border
        m.overrideredirect(True)
        m.attributes('-topmost', True)
        inner = tk.Frame(m, background=self.CARD, padx=4, pady=4)
        inner.pack(padx=1, pady=1)

        def close(event=None):
            if m.winfo_exists():
                m.grab_release()
                m.destroy()

        def choose(command):
            close()
            command()
        for item in items:
            if item is None:
                tk.Frame(inner, background=self.LINE, height=1).pack(fill='x', padx=6, pady=4)
                continue
            label, command, enabled = item
            w = tk.Label(inner, text=label, anchor='w', background=self.CARD, padx=14, pady=6, font=('Segoe UI', 10),
                         foreground=self.TEXT if enabled else self.DISABLED, cursor='hand2' if enabled else '')
            w.pack(fill='x')
            if enabled:
                w.bind('<Enter>', lambda e, w=w: w.configure(background=self.HOVER))
                w.bind('<Leave>', lambda e, w=w: w.configure(background=self.CARD))
                w.bind('<ButtonRelease-1>', lambda e, c=command: choose(c))
        m.update_idletasks()
        x = min(x, m.winfo_screenwidth() - m.winfo_reqwidth())
        y = y if y + m.winfo_reqheight() <= m.winfo_screenheight() else y - m.winfo_reqheight()
        m.geometry(f'+{x}+{y}')
        m.bind('<Escape>', close)
        m.bind('<FocusOut>', close)  # clicked another program
        m.bind('<Button-1>', lambda e: close() if not (0 <= e.x_root - m.winfo_rootx() < m.winfo_width() and
                                                       0 <= e.y_root - m.winfo_rooty() < m.winfo_height()) else None)
        m.focus_force()
        m.grab_set()  # a click elsewhere in this program comes here first, and closes the menu

    @staticmethod
    def short_state(state):
        """'Portal connected' for 'real portal: claimed (Traptanium: ...)'; the full state is in the log."""
        state = re.split(r' \(| - ', state.removeprefix('real portal: '))[0]
        return {'claimed': 'Portal connected', 'looking': 'Looking for the portal'}.get(state, f'Portal {state}')

    def open_settings(self):
        """The Settings page: write-back, portal speaker volume, the activity log, and the actions for all figures."""
        import tkinter as tk
        from tkinter import ttk
        if getattr(self, 'settings_window', None) is not None and self.settings_window.winfo_exists():
            return self.settings_window.lift()
        d = self.settings_window = tk.Toplevel(self.root)
        d.title('Settings')
        d.configure(background=self.CARD)
        d.transient(self.root)
        d.resizable(False, False)
        body = ttk.Frame(d, style='Card.TFrame', padding=(24, 20, 24, 8))
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='Settings', style='Section.TLabel').pack(anchor='w')
        columns = ttk.Frame(body, style='Card.TFrame')
        columns.pack(fill='both', expand=True)
        left, right = (ttk.Frame(columns, style='Card.TFrame') for _ in range(2))
        left.pack(side='left', fill='y', anchor='n')
        right.pack(side='left', fill='y', anchor='n', padx=(32, 0))
        col = left  # Saving, Sound and Portal on the left; Emulators, Window and All figures on the right

        def section(title):
            tk.Frame(col, background=self.LINE, height=1).pack(fill='x', pady=(14, 10))
            ttk.Label(col, text=title, style='Card.TLabel', font=('Segoe UI Semibold', 10)).pack(anchor='w', pady=(0, 6))

        def line(help=None, pad=8):
            """A row in the current column, ending in a ? with the explanation when there is one."""
            r = ttk.Frame(col, style='Card.TFrame')
            r.pack(fill='x', pady=(pad, 0))
            if help:
                r.help = self.help_icon(r, help)
            return r

        def done(r):  # the ? right after what the row explains
            if hasattr(r, 'help'):
                r.help.pack(side='left', padx=(6, 0))

        def check(text, var, command, help=None, pad=8):
            r = line(help, pad)
            ttk.Checkbutton(r, text=text, variable=var, style='Card.TCheckbutton', command=command).pack(side='left')
            done(r)

        def label(text, help=None, pad=10):
            r = line(help, pad)
            ttk.Label(r, text=text, style='Card.TLabel').pack(side='left')
            done(r)
            return r
        if self.mirror:
            section('Saving')
            check('Write saves to real figures', self.wb, self.toggle_write_back, pad=0,
                  help='Game saves also go onto the figure on the real portal, not only into its copy on the PC. '
                       'Every block is read back to check it.')
            restore_popup = tk.BooleanVar(value=self.settings().get('restore_popup', True))
            check('Ask to put newer progress back on a figure', restore_popup,
                  lambda: self.remember(restore_popup=restore_popup.get()),
                  help='When you place a figure that is behind its saved copy (because you played with "Write saves '
                       'to real figures" off), a pop-up offers to write that progress onto the figure. Off: only the '
                       'log mentions it, and "Restore progress…" still has it.')

            r = label('Backups per figure', help='How many older copies of each figure are kept. The newest intact '
                                                 'ones stay; older ones go to the Recycle Bin.')
            kept = tk.StringVar(value=str(self.settings().get('backups', 1)))

            def set_kept(*_):
                global BACKUPS_KEPT
                if kept.get().isdigit() and 1 <= int(kept.get()) <= 10:
                    BACKUPS_KEPT = int(kept.get())
                    self.remember(backups=BACKUPS_KEPT)
            kept.trace_add('write', set_kept)
            ttk.Spinbox(r, from_=1, to=10, textvariable=kept, width=4, state='readonly').pack(side='left', padx=(12, 0))

            folder = tk.StringVar(value=self.settings().get('dumps') or DUMPS)
            moving = lambda: os.path.normcase(os.path.normpath(folder.get() or DUMPS)) != os.path.normcase(DUMPS)
            r = label('Figures folder', help=lambda: 'Where your figure files are kept. ' + (
                'Your files move to the new folder the next time PortalSync starts.' if moving() else
                'Pick another folder (for example on OneDrive) and they move there on the next start.'))

            def set_folder(*_):
                path = os.path.normpath(folder.get().strip().strip('"')) if folder.get().strip() else ''
                if not path or os.path.normcase(path) == os.path.normcase(DUMPS):
                    self.remember(dumps=DUMPS, dumps_move_from=None)
                else:
                    self.remember(dumps=path, dumps_move_from=DUMPS)

            def pick_folder():
                from tkinter import filedialog
                path = filedialog.askdirectory(parent=d, title='Folder for your figure files', initialdir=DUMPS)
                if path:
                    folder.set(os.path.normpath(path))
                    set_folder()
            ttk.Button(r, text='Browse…', style='Small.TButton', command=pick_folder).pack(side='right', fill='y')
            box = ttk.Entry(r, textvariable=folder, width=36)
            box.pack(side='right', fill='x', expand=True, padx=(12, 8))
            box.bind('<FocusOut>', set_folder)
            box.bind('<Return>', set_folder)

            section('Sound')
            r = label('Portal speaker volume', pad=0)
            percent = ttk.Label(r, style='CardMuted.TLabel', width=5, anchor='e')
            percent.pack(side='right')
            scale = ttk.Scale(r, from_=0, to=100, variable=self.volume, length=200,
                              command=lambda v: (self.set_volume(), percent.configure(text=f'{self.volume.get()} %')))
            scale.pack(side='right', padx=(12, 6))
            scale.bind('<ButtonRelease-1>', lambda e: self.remember(volume=self.volume.get()))
            percent.configure(text=f'{self.volume.get()} %')
            r = label('Trap portal sound on', help='Where Trap Team plays the sounds of trapped villains. The portal '
                                                   'speaker needs a Traptanium Portal; without one the sound plays on the PC.')
            sound = tk.StringVar(value=self.settings().get('sound', 'portal'))

            def set_sound():
                if getattr(self.server, 'sound', None):
                    self.server.sound.mode = sound.get()
                self.remember(sound=sound.get())
            for value, text in (('off', 'Off'), ('pc', 'PC speakers'), ('portal', 'Portal speaker')):
                ttk.Radiobutton(r, text=text, value=value, variable=sound, style='Tab.TRadiobutton',
                                command=set_sound).pack(side='right')

            section('Portal')
            lights = tk.BooleanVar(value=self.settings().get('lights', True))

            def set_lights():
                self.mirror.lights = lights.get()
                self.remember(lights=lights.get())
            check('Lights', lights, set_lights, pad=0, help="The real portal's lights follow the game, as on a console.")

            col = right
            section('Emulators')
            self.emulator_rows(col, d)
            full = tk.BooleanVar(value=self.settings().get('fullscreen', False))
            check('Start games in fullscreen', full, lambda: self.remember(fullscreen=full.get()),
                  help='Start game opens the game fullscreen. RPCS3 then starts without its own window.')

        col = right
        section('Window')
        check('Show log', self.show_log, self.toggle_log, pad=0,
              help='The activity list at the bottom of the main window. Everything stays in bridge.log either way.')
        dark = tk.BooleanVar(value=self.settings().get('dark', False))
        check('Dark mode', dark, lambda: self.remember(dark=dark.get()), help='Used from the next start.')
        updates = tk.BooleanVar(value=self.settings().get('check_updates', True))
        check('Check for a new version at start', updates, lambda: self.remember(check_updates=updates.get()),
              help='Asks GitHub for the latest release, and shows a link in the main window when there is a newer one.')

        if self.mirror:
            section('All figures')
            r = line(help='Back up: every figure file into one zip you choose. Restore: write a backup onto a figure '
                          'on the real portal.', pad=0)
            ttk.Button(r, text='Back up all figures…', command=self.backup_all).pack(side='left')
            ttk.Button(r, text='Restore progress…', command=self.open_restore).pack(side='left', padx=(8, 0))
            done(r)
        bar = ttk.Frame(d, padding=(24, 14), style='Card.TFrame')
        bar.pack(fill='x')

        def close():
            d.focus_set()  # a path being typed is saved by its box losing the focus
            d.update()
            d.destroy()
        ttk.Button(bar, text='Close', style='Accent.TButton', command=close).pack(side='right')
        d.bind('<Escape>', lambda e: close())
        d.protocol('WM_DELETE_WINDOW', close)
        d.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - d.winfo_reqwidth()) // 2  # over the main window,
        y = self.root.winfo_rooty() + 60
        d.geometry(f'+{min(max(0, x), d.winfo_screenwidth() - d.winfo_reqwidth())}'  # but on the screen
                   f'+{min(max(0, y), d.winfo_screenheight() - d.winfo_reqheight() - 40)}')

    def help_icon(self, parent, text):
        """A small ? that shows `text` (or text() when it is a function) after a moment's hover, or on a click."""
        import tkinter as tk
        c = tk.Canvas(parent, width=18, height=18, background=self.CARD, highlightthickness=0, cursor='question_arrow')
        c.create_oval(2, 2, 16, 16, outline=self.FAINT)
        c.create_text(9, 9, text='?', fill=self.MUTED, font=('Segoe UI Semibold', 8))
        tip = {'win': None, 'job': None}

        def show():
            hide()
            w = tip['win'] = tk.Toplevel(c, background=self.LINE)  # the 1 px border
            w.overrideredirect(True)
            w.attributes('-topmost', True)
            tk.Label(w, text=text() if callable(text) else text, background=self.CARD, foreground=self.TEXT,
                     font=('Segoe UI', 9), wraplength=320, justify='left', padx=10, pady=7).pack(padx=1, pady=1)
            w.update_idletasks()
            x = min(c.winfo_rootx() + 24, w.winfo_screenwidth() - w.winfo_reqwidth() - 8)
            w.geometry(f'+{x}+{c.winfo_rooty() - 6}')

        def hide(*_):
            if tip['job']:
                c.after_cancel(tip['job'])
                tip['job'] = None
            if tip['win'] is not None and tip['win'].winfo_exists():
                tip['win'].destroy()
            tip['win'] = None
        c.bind('<Enter>', lambda e: tip.__setitem__('job', c.after(500, show)))
        c.bind('<Leave>', hide)
        c.bind('<Button-1>', lambda e: hide() if tip['win'] else show())
        c.bind('<Destroy>', hide)
        return c

    def emulator_rows(self, parent, dialog):
        """A path box and a Browse… button for Cemu, RPCS3 and Dolphin; a change is saved when the box is left."""
        import tkinter as tk
        from tkinter import ttk
        emulators = self.emulators()
        grid = ttk.Frame(parent, style='Card.TFrame')
        grid.pack(fill='x')

        def save(name, var):
            path = var.get().strip().strip('"')
            emulators.pop(name, None) if not path else emulators.__setitem__(name, os.path.normpath(path))
            self.remember(emulators=emulators)

        def browse(name, var):
            from tkinter import filedialog
            exe = filedialog.askopenfilename(parent=dialog, title=f'Where is {name}?', filetypes=[('Programs', '*.exe')])
            if exe:
                var.set(os.path.normpath(exe))
                save(name, var)
        for row, name in enumerate(EMULATOR_EXES):
            var = tk.StringVar(value=emulators.get(name, ''))
            ttk.Label(grid, text=name, style='Card.TLabel', width=8).grid(row=row, column=0, sticky='w', pady=3)
            box = ttk.Entry(grid, textvariable=var, width=48)
            box.grid(row=row, column=1, sticky='we', padx=(4, 8), pady=3)
            box.bind('<FocusOut>', lambda e, n=name, v=var: save(n, v))
            box.bind('<Return>', lambda e, n=name, v=var: save(n, v))
            ttk.Button(grid, text='Browse…', style='Small.TButton',
                       command=lambda n=name, v=var: browse(n, v)).grid(row=row, column=2, pady=3, sticky='ns')
        grid.columnconfigure(1, weight=1)

    def welcome(self):
        """First start: the main choices in one window, before the main window is built (so dark mode applies
        at once). Everything can be changed later in Settings."""
        import tkinter as tk
        from tkinter import ttk
        d = tk.Toplevel(self.root)
        d.title('Welcome to PortalSync')
        d.configure(background=self.CARD)
        d.resizable(False, False)
        body = ttk.Frame(d, style='Card.TFrame', padding=(28, 22, 28, 8))
        body.pack(fill='both')
        ttk.Label(body, text='Welcome to PortalSync', style='Title.TLabel').pack(anchor='w')
        ttk.Label(body, text='A few choices to start with. You can change them later in Settings.',
                  style='CardMuted.TLabel').pack(anchor='w', pady=(4, 0))

        def row(label, help=None):
            tk.Frame(body, background=self.LINE, height=1).pack(fill='x', pady=12)
            r = ttk.Frame(body, style='Card.TFrame')
            r.pack(fill='x')
            ttk.Label(r, text=label, style='Card.TLabel', font=('Segoe UI Semibold', 10)).pack(side='left')
            if help:
                self.help_icon(r, help).pack(side='left', padx=(6, 0))
            return r
        theme = tk.StringVar(value='dark' if self.settings().get('dark') else 'light')
        r = row('Look')
        for value, text in (('dark', 'Dark'), ('light', 'Light')):
            ttk.Radiobutton(r, text=text, value=value, variable=theme, style='Tab.TRadiobutton').pack(side='right')
        wb = tk.BooleanVar(value=self.settings().get('write_back', True))
        r = row('Write saves to real figures', 'Game saves also go onto the figure on the real portal, not only into '
                                               'its copy on the PC. Every block is read back to check it.')
        ttk.Checkbutton(r, variable=wb, style='Card.TCheckbutton').pack(side='right')
        sound = tk.StringVar(value=self.settings().get('sound', 'portal'))
        r = row('Trap portal sound on', 'Where Trap Team plays the sounds of trapped villains. The portal speaker '
                                        'needs a Traptanium Portal; without one the sound plays on the PC.')
        for value, text in (('off', 'Off'), ('pc', 'PC speakers'), ('portal', 'Portal speaker')):
            ttk.Radiobutton(r, text=text, value=value, variable=sound, style='Tab.TRadiobutton').pack(side='right')
        lights = tk.BooleanVar(value=self.settings().get('lights', True))
        r = row('Lights', "The real portal's lights follow the game, as on a console.")
        ttk.Checkbutton(r, variable=lights, style='Card.TCheckbutton').pack(side='right')
        updates = tk.BooleanVar(value=self.settings().get('check_updates', True))
        r = row('Check for a new version at start', 'Asks GitHub for the latest release, and shows a link in the main '
                                                    'window when there is a newer one.')
        ttk.Checkbutton(r, variable=updates, style='Card.TCheckbutton').pack(side='right')
        row('Emulators', 'Used by Start game. Found by themselves where possible; type another path or pick '
                         'the exe with Browse.')
        self.emulator_rows(body, d)

        def done():
            d.focus_set()  # an emulator path being typed is saved by its box losing the focus
            d.update()
            self.remember(dark=theme.get() == 'dark', write_back=wb.get(), sound=sound.get(), lights=lights.get(),
                          check_updates=updates.get(), setup_done=True)
            d.destroy()
        bar = ttk.Frame(d, padding=(28, 16), style='Card.TFrame')
        bar.pack(fill='x')
        ttk.Button(bar, text='Start', style='Accent.TButton', command=done).pack(side='right')
        d.protocol('WM_DELETE_WINDOW', done)
        d.bind('<Return>', lambda e: done())
        d.update_idletasks()
        d.geometry(f'+{(d.winfo_screenwidth() - d.winfo_reqwidth()) // 2}+{(d.winfo_screenheight() - d.winfo_reqheight()) // 3}')
        d.focus_force()
        self.root.wait_window(d)

    def toggle_log(self):
        if self.show_log.get():
            self.activity.pack(fill='x', pady=(12, 0))
            self.log.see('end')
        else:
            self.activity.pack_forget()
        self.remember(show_log=self.show_log.get())

    def figure_menu(self, event):
        """Right-click on the slot table: what can be done with that figure."""
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        with self.server.cv:
            real = any(v == int(row) and self.portal.slots[v].path == path
                       for v, path, u in (self.mirror.slots.values() if self.mirror else ()))
        items = [('Unload', self.unload_selected, True)]
        if self.mirror:  # these write to the figure, so it must be on the real portal
            items += [None, ('Edit gold, nickname, level…', self.edit_selected, real),
                      ('Repair damaged save…', self.repair_selected, real),
                      ('Reset to a new figure…', self.reset_selected, real),
                      ('Restore progress…', self.restore_selected_figure, real)]
        self.popup(event.x_root, event.y_root, items)

    def restore_selected_figure(self):
        """The restore pop-up for the right-clicked figure only, on its backup."""
        from tkinter import messagebox
        picked = self.selected_real('Restore progress')
        if not picked:
            return
        uid, data, name = picked
        backup = (backups(uid).get(uid) or [None])[-1]
        if backup is None:
            return messagebox.showinfo('Restore progress', f'{name} has no backup yet.', parent=self.root)
        self.open_restore(select=backup)
        if self.dialog is None or not self.dialog.winfo_exists():
            messagebox.showinfo('Restore progress', f"{name}'s backup is the same as the figure: nothing to restore.",
                                parent=self.root)

    def selected_real(self, what):
        """(uid, data, name) of the figure selected in the slot table if it is on the real portal, else None."""
        from tkinter import messagebox
        n = next(iter(map(int, self.tree.selection())), None)
        with self.server.cv:
            uid = next((u for i, (v, path, u) in self.mirror.slots.items()
                        if v == n and self.portal.slots[v].path == path), None)
        if uid is None:
            messagebox.showinfo(what, 'Select a figure that is on the real portal first.', parent=self.root)
            return None
        data = self.mirror.figure_data(uid)
        return uid, data, figure_name(data)

    def write(self, uid, make, what, done):
        from tkinter import messagebox
        try:
            self.mirror.write_image(uid, make, what)
        except ValueError as e:
            return messagebox.showerror('Not written', str(e), parent=self.root)
        self.say(done)

    def repair_selected(self):
        from tkinter import messagebox
        picked = self.selected_real('Repair figure')
        if not picked:
            return
        uid, data, name = picked
        info = figure_info(data)
        if not info.damaged:
            return messagebox.showinfo('Nothing to repair', f'The save on {name} is not damaged.', parent=self.root)
        if messagebox.askyesno('Repair figure?', (
                f'Repair {name}{stats(info)}?\n\nIts newest save gets valid checksums again and keeps all its progress, '
                'as SuperChargers did for Snap Shot. What it holds now is kept as its backup.'), parent=self.root):
            self.write(uid, repaired, 'a repair', f'Repairing {name} ({uid}); see the log.')

    def edit_selected(self):
        import tkinter as tk
        from tkinter import messagebox, ttk
        picked = self.selected_real('Edit figure')
        if not picked:
            return
        uid, data, name = picked
        info = figure_info(data)
        if info.kind != 'skylander':
            return messagebox.showinfo('Edit figure', f'{name} has no level, gold or nickname to edit '
                                       '(only Skylanders do; a new figure needs one game save first).', parent=self.root)
        d = tk.Toplevel(self.root)
        d.title('Edit figure')
        d.configure(background=self.CARD)
        d.transient(self.root)
        d.resizable(False, False)
        body = ttk.Frame(d, style='Card.TFrame', padding=(24, 20))
        body.pack(fill='both')
        ttk.Label(body, text=name, style='Figure.TLabel').grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 12))
        nick, gold, level = tk.StringVar(value=info.nickname), tk.StringVar(value=info.gold), tk.StringVar(value=info.level or '')
        ttk.Label(body, text='Nickname', style='Card.TLabel').grid(row=1, column=0, sticky='w', pady=4)
        ttk.Entry(body, textvariable=nick, width=24).grid(row=1, column=1, sticky='w', padx=(16, 0))
        ttk.Label(body, text='Gold (0-65000)', style='Card.TLabel').grid(row=2, column=0, sticky='w', pady=4)
        ttk.Spinbox(body, from_=0, to=65000, increment=100, textvariable=gold, width=10).grid(row=2, column=1, sticky='w', padx=(16, 0))
        ttk.Label(body, text='Level (1-20)', style='Card.TLabel').grid(row=3, column=0, sticky='w', pady=4)
        box = ttk.Spinbox(body, from_=1, to=20, textvariable=level, width=10)
        box.grid(row=3, column=1, sticky='w', padx=(16, 0))
        if info.level is None:
            box.state(['disabled'])  # Imaginators and Swapper bottom halves keep no level here

        def save():
            try:
                g = int(gold.get())
                lv = int(level.get()) if info.level is not None else None
                if not 0 <= g <= 65000 or (lv is not None and not 1 <= lv <= 20) or len(nick.get()) > 15:
                    raise ValueError
            except ValueError:
                return messagebox.showerror('Edit figure', 'Gold 0-65000, level 1-20, nickname up to 15 letters.', parent=d)
            changes = {k: v for k, v, old in (('gold', g, info.gold), ('nickname', nick.get(), info.nickname),
                                              ('level', lv, info.level)) if v != old}
            d.destroy()
            if changes:
                self.write(uid, lambda held: edited(held, **changes), 'edits (' + ', '.join(changes) + ')',
                           f'Writing {", ".join(changes)} to {name} ({uid}); see the log.')
        bar = ttk.Frame(d, padding=(24, 0, 24, 18), style='Card.TFrame')
        bar.pack(fill='x')
        ttk.Button(bar, text='Write to figure', style='Accent.TButton', command=save).pack(side='right')
        ttk.Button(bar, text='Cancel', command=d.destroy).pack(side='right', padx=(0, 8))
        ttk.Label(body, style='CardMuted.TLabel', wraplength=320, justify='left', text=(
            'Saved the way the game saves: the previous save stays on the figure as its fallback, and what the '
            'figure holds now is kept as its backup.')).grid(row=4, column=0, columnspan=2, sticky='w', pady=(12, 0))

    def backup_all(self):
        import zipfile
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension='.zip', filetypes=[('Zip files', '*.zip')],
                                            initialfile=time.strftime('Skylanders backup %Y-%m-%d.zip'))
        if not path:
            return
        try:
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
                names = [n for n in os.listdir(DUMPS) if n.endswith('.sky')]
                for n in names:
                    z.write(os.path.join(DUMPS, n), n)
        except OSError as e:
            return self.say(f'Backup failed: {e}')
        self.say(f'{len(names)} figure files backed up to {path}')

    def set_volume(self):
        if getattr(self.server, 'sound', None):
            self.server.sound.volume = self.volume.get() / 100

    def remember(self, **values):
        """Save choices into settings.json, keeping the others. Read first: opening the file for writing empties
        it, and reading inside the open wiped every other choice (until 2026-10-17)."""
        merged = {**self.settings(), **values}
        try:
            with open(SETTINGS + '.tmp', 'w') as f:
                json.dump(merged, f)
            os.replace(SETTINGS + '.tmp', SETTINGS)  # never a half-written file
        except OSError as e:
            log(f'choice not remembered: {e}')

    def emulators(self):
        """{emulator: exe} from settings.json; looked for on the first use (find_emulators)."""
        emulators = self.settings().get('emulators') or {}
        if not emulators:
            if hasattr(self, 'status'):  # not yet there on the first start (welcome window)
                self.say('Looking for Cemu, RPCS3 and Dolphin…')
                self.root.update()
            emulators = find_emulators()
            self.remember(emulators=emulators)
        return emulators

    def open_games(self):
        """Start a Skylanders game from here, so the bridge is always running first."""
        import tkinter as tk
        from tkinter import ttk
        emulators = self.emulators()
        d = tk.Toplevel(self.root)
        d.title('Start a game')
        d.configure(background=self.CARD)
        d.transient(self.root)
        body = ttk.Frame(d, style='Card.TFrame', padding=(22, 18))
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='Start a game', style='Section.TLabel').pack(anchor='w')
        tree = ttk.Treeview(body, columns=('game', 'emulator', 'path'), show='headings', height=7, selectmode='browse')
        for c, title in (('game', 'GAME'), ('emulator', 'EMULATOR'), ('path', 'FILE')):
            tree.heading(c, text=title, anchor='w')
        tree.pack(fill='both', expand=True, pady=(10, 0))
        none = ttk.Label(body, style='CardMuted.TLabel', text='No Skylanders games found. Check your emulators in Settings.')

        def fill():
            tree.delete(*tree.get_children())
            games = find_games(emulators)
            for k, (title, emulator, path) in enumerate(games):
                tree.insert('', 'end', iid=path, values=(title, emulator, path), tags=('odd',) if k % 2 else ())
            tree.tag_configure('odd', background=self.ODD)
            last = self.settings().get('last_game')
            if tree.get_children():
                tree.selection_set(last if last in tree.get_children() else tree.get_children()[0])
            if not games:
                none.pack(anchor='w', pady=(10, 0))
            self.fit_columns(tree, games or [('', '', '')], d, extra=60)  # last: the window fits everything above

        def start():
            sel = tree.selection()
            if not sel:
                return
            title, emulator, path = tree.item(sel[0], 'values')
            self.remember(last_game=path)
            d.destroy()
            try:
                start_game(emulators[emulator], emulator, path, self.settings().get('fullscreen', False))
            except (OSError, KeyError) as e:
                self.say(f'Could not start {emulator}: {e}')
        bar = ttk.Frame(d, padding=(22, 0, 22, 16), style='Card.TFrame')
        bar.pack(fill='x', side='bottom', before=body)  # laid out first: the buttons always stay visible
        ttk.Button(bar, text='Start', style='Accent.TButton', command=start).pack(side='right')
        ttk.Button(bar, text='Cancel', command=d.destroy).pack(side='right', padx=(0, 8))
        tree.bind('<Double-1>', lambda e: start())
        d.bind('<Return>', lambda e: start())
        d.bind('<Escape>', lambda e: d.destroy())
        fill()

    def reset_selected(self):
        from tkinter import messagebox
        picked = self.selected_real('Reset figure')
        if not picked:
            return
        uid, data, name = picked
        if not messagebox.askyesno('Reset figure?', (
                f'Erase all progress on {name}{stats(figure_info(data))}?\n\n'
                'It becomes a figure fresh out of the box: the game asks for a name and it starts again at '
                'level 1. What it holds now is kept as its backup ("before restore"), so "Restore progress…" '
                'can undo this.'), icon='warning', parent=self.root):
            return
        try:
            self.mirror.reset(uid)
        except ValueError as e:
            return messagebox.showerror('Not reset', str(e), parent=self.root)
        self.say(f'Resetting {name} ({uid}); see the log.')

    def unload_selected(self):
        for n in map(int, self.tree.selection()):
            with self.server.cv:
                name = os.path.basename(self.portal.slots[n].path or '')
                self.portal.remove(n)
            if name:
                self.say(f'{name} removed from slot {n + 1}')
        self.refresh()


def main():
    global LOG_FILE
    try:  # first: a second copy must stop before it touches the running one's log or dumps
        listener = socket.create_server((HOST, TCP_PORT))
    except OSError:
        raise SystemExit(f'Port {TCP_PORT} is in use. Is PortalSync already running?')
    LOG_FILE = open(os.path.join(HERE, 'bridge.log'), 'w')
    global DUMPS, BACKUPS_KEPT
    prefs = App.settings()
    DUMPS = prefs.get('dumps') or DUMPS
    BACKUPS_KEPT = prefs.get('backups', 1)
    if prefs.get('dumps_move_from'):  # the figures folder was changed in Settings
        n = move_dumps(prefs['dumps_move_from'], DUMPS)
        log(f'{n} figure file(s) moved from {prefs["dumps_move_from"]} to {DUMPS}')
        with open(SETTINGS, 'w') as f:
            json.dump({**prefs, 'dumps_move_from': None}, f)
    rename_old_dumps()
    for uid in backups():
        keep_one_backup(uid)
    desc = load_desc()
    server = Server(Portal(desc.r_reply), desc)
    server.sound = PortalSound()
    threading.Thread(target=server.serve, args=(listener,), daemon=True).start()
    log(f'USB/IP server on {HOST}:{TCP_PORT}')
    before = {(d.bus, d.address) for d in portal_devices()}
    rc, out = usbip('attach', '-r', HOST, '-b', BUSID)
    if rc != 0:
        raise SystemExit('attach failed')
    port = re.search(r'port\D*(\d+)', out)
    try:
        vhci_bus = None  # the virtual portal is the 1430:0150 that appears after attach
        for _ in range(50):
            vhci_bus = next((d.bus for d in portal_devices() if (d.bus, d.address) not in before), None)
            if vhci_bus is not None:
                break
            time.sleep(0.1)
        mirror = None
        if vhci_bus is None:
            log('virtual portal not visible to libusb: real portal mirroring off')
        else:
            log(f'virtual portal on libusb bus {vhci_bus}; mirroring real portal from other buses')
            mirror = Mirror(server, vhci_bus)
            server.sound.speaker = mirror.play
            if os.path.exists(HIDE_DLL):  # first, so lost() knows which emulators run when the portal is busy
                threading.Thread(target=watch_emulators, daemon=True).start()
            else:
                log('portal_hide.dll not built: emulators will also see the real portal')
            threading.Thread(target=mirror.run, daemon=True).start()
        app = App(server, mirror, first_start=not App.settings().get('setup_done'))
        for p in sys.argv[1:]:
            app.load(p)
        for sig in (signal.SIGINT, signal.SIGBREAK):  # Ctrl+C / console closed: leave the UI, then detach
            signal.signal(sig, lambda *_: app.root.quit())

        def tick():  # every 200 ms: show mirror changes; also lets Python run signal handlers while Tk waits
            app.root.after(200, tick)  # first, so an error in refresh() doesn't stop the updates
            app.refresh()
        tick()
        app.root.mainloop()
    finally:
        if port:
            usbip('detach', '-p', port.group(1))
        listener.close()
        with server.cv:  # feed() runs under cv
            server.sound.close()


if __name__ == '__main__':
    try:
        capture() if sys.argv[1:] == ['capture'] else main()
    except Exception as e:  # PortalSync.exe has no console: say why it stopped instead of vanishing
        import traceback
        log(traceback.format_exc())
        if not sys.stdout:
            ctypes.windll.user32.MessageBoxW(None, f'PortalSync stopped:\n{e}\n\nDetails are in bridge.log.',
                                             'PortalSync', 0x10)
        raise
    except SystemExit as e:
        if isinstance(e.code, str) and not sys.stdout:
            ctypes.windll.user32.MessageBoxW(None, f'PortalSync stopped: {e.code}', 'PortalSync', 0x10)
        raise
