"""portal-bridge: software Skylanders Portal of Power exported over USB/IP to usbip-win2.
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
Add Skylander (button): search the figures in dumps/ and load one's current copy or backup, or create
a new blank figure of any Skylander in figure_names.json (as Dolphin's "Create Skylander" does).

Portal sound (Trap Team's villain and trap sounds): played on the real Traptanium portal's speaker,
or on the PC's default audio device when there is none or the portal is away, see PortalSound.

Game writes go straight back into the loaded file.

Portal logic is ported from Dolphin Source/Core/Core/IOS/USB/Emulated/Skylanders/Skylander.cpp
(cross-checked with RPCS3 rpcs3/Emu/Io/Skylander.cpp). USB/IP framing follows the Linux
Documentation/usb/usbip_protocol.rst.
"""
import csv
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

HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))  # PortalBridge.exe: its folder
DESC_FILE = os.path.join(HERE, 'portal_descriptors.json')
DUMPS = os.path.join(HERE, 'dumps')
SETTINGS = os.path.join(HERE, 'settings.json')  # choices made in the window, kept between runs
VID, PID = 0x1430, 0x0150
HOST, TCP_PORT, BUSID = '127.0.0.1', 3240, '1-1'
USBIP_EXE = r'C:\Program Files\USBip\usbip.exe'
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
        if sys.stdout:  # None in PortalBridge.exe (no console)
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


def keep_one_backup(uid, keep=None):
    """At most one backup per figure: keep `keep` (default: the newest) and move the others to the Recycle Bin."""
    paths = backups(uid).get(uid, [])
    keep = keep or (paths[-1] if paths else None)
    for path in paths:
        if os.path.normcase(path) != os.path.normcase(keep):
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
        if n >= 16 or block >= 64:
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
        if self.speaker and self.speaker(block):
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
        ref = self.read_block0(slot)
        return b''.join(self.read_block(slot, block, ref) for block in range(64))

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
        self.known = {}  # real slot -> bytearray: what the real figure holds (read on placement, updated by writes)
        self.left = {}  # uid -> what the real figure held when it was lifted (see place())
        self.offers = queue.Queue()  # archived saved copies the window offers to restore (see add())
        self.notices = queue.Queue()  # messages the window shows as a pop-up (see lost())
        self.speaker = False  # the claimed real portal is a Traptanium portal (see poll), which has a speaker
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
            if previous != data and self.write_back and self.left.get(uid) == data:
                # The figure is exactly as the bridge left it, so the difference is only game saves that did
                # not reach it (lifted mid-write, 2026-10-11): finish them like write-back would have.
                catch_up = data = previous
            elif previous != data:  # progress the real figure does not have: keep it, never overwrite
                old = stamped(data, 'saved')
                os.replace(path, old)
                log(f'{os.path.basename(path)} differed from the real figure; kept as {os.path.basename(old)}')
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
        if offer:
            keep_one_backup(uid, keep=offer)
            self.offers.put(offer)  # figure registered: the window asks whether to write the copy to it

    def remove(self, i):
        with self.server.cv:
            v, path, uid = self.slots.pop(i)
            held = self.known.pop(i, None)
            if held is not None:
                self.left[uid] = bytes(held)
            if self.server.portal.slots[v].path == path:  # skip if the user already unloaded it
                self.server.portal.remove(v)
        log(f'real slot {i} lifted: virtual slot {v + 1} emptied')

    def release(self):
        with self.server.cv:
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
        if c in 'CJLM' and self.real:  # lights and the speaker's on/off ('M') follow the game on the real portal
            self.jobs.put(('raw', buf))
        elif c == 'W' and self.write_back:
            n, block = buf[1] & 0xF, buf[2]
            s = self.server.portal.slots[n]
            real = next((i for i, (v, path, uid) in self.slots.items() if v == n and s.path == path), None)
            if real is not None:  # else: not a figure on the connected real portal
                self.queue_block(real, block, bytes(buf[3:19]))

    def queue_block(self, i, block, data):  # cv held
        uid = self.slots[i][2]
        if not has_backup(uid):
            return log(f'write-back {uid}: no backup in dumps/, not written')
        if block == 0 or block % 4 == 3:  # manufacturer block and sector trailers (keys) are never written
            return log(f'write-back {uid} block {block:#04x}: refused (not a data block)')
        self.jobs.put(('write', i, block, data, uid))

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
        keep_one_backup(uid, keep=snapshot)

    def write_verified(self, i, block, data, uid, what='write-back'):  # Mirror thread
        """Write one data block to the figure in real slot i if it is still uid; True if it reads back equal."""
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
            self.write_back = on
            return sum(self.queue_sync(i) for i in self.slots) if on else 0

    def run_jobs(self):
        while not self.jobs.empty():
            job = self.jobs.get_nowait()
            if job[0] == 'raw':
                self.real.send(*job[1])
            elif job[0] == 'restore':
                try:
                    self.restore_now(*job[1:])
                except (ValueError, TimeoutError) as e:  # figure moved mid-read/write
                    log(f'restore {job[1]}: {e}; stopped, see the lines above for what was written')
            else:
                self.write_verified(*job[1:])


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


def usbip(*args):
    r = subprocess.run([USBIP_EXE, *args], capture_output=True, text=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    out = (r.stdout + r.stderr).strip()
    log('usbip', *args, '->', r.returncode, out)
    return r.returncode, out


class App:
    # Windows 11 light colours
    BG, CARD, LINE, TEXT, MUTED = '#f3f3f3', '#ffffff', '#e5e5e5', '#1b1b1b', '#616161'
    ACCENT, ACCENT_HOVER, ACCENT_DOWN, SELECTED, BAD = '#0067c0', '#1975c5', '#005ba8', '#cce4f7', '#c42b1c'
    STATE_COLOURS = (('claimed', '#0f7b0f'), ('not plugged in', '#8a8a8a'), ('', '#c45500'))  # first match wins
    QUIET = re.compile(r'\S+ (portal [<-]|GET_DESCRIPTOR|SET_CONFIGURATION|UNLINK|portal sound: \d)')  # bridge.log only
    BAD_LINE = re.compile(r'FAILED|failed|in use|lost|error|not copied|not written|could not|did NOT', re.I)

    def __init__(self, server, mirror=None):
        import tkinter as tk
        from tkinter import font, ttk
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled displays
        except (AttributeError, OSError):
            pass
        self.server, self.portal, self.mirror = server, server.portal, mirror
        self.dialog, self.log_seen, self.rows, self.queue, self.done, self.current = None, 0, None, [], 0, None
        self.root = root = tk.Tk()
        root.title('Portal Bridge')
        root.configure(background=self.BG)
        root.minsize(760, 640)
        icon = tk.PhotoImage(width=32, height=32)  # a portal: blue ring, light centre
        for y in range(32):
            r = [(x - 15.5) ** 2 + (y - 15.5) ** 2 for x in range(32)]
            icon.put('{' + ' '.join(self.ACCENT if d < 225 and d >= 64 else '#9ccbf0' if d < 64 else self.CARD
                                    for d in r) + '}', to=(0, y))
        root.iconphoto(True, icon)
        font.nametofont('TkDefaultFont').configure(family='Segoe UI', size=10)
        self.style(ttk.Style(root), font.nametofont('TkDefaultFont').metrics('linespace'))

        header = ttk.Frame(root, style='Card.TFrame', padding=(22, 16))
        header.pack(fill='x')
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text='Portal Bridge', style='Title.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Label(header, text='One real Skylanders portal for Cemu, RPCS3 and Dolphin',
                  style='CardMuted.TLabel').grid(row=1, column=0, sticky='w')
        pill = ttk.Frame(header, style='Card.TFrame')
        pill.grid(row=0, column=1, rowspan=2, sticky='e')
        self.dot = ttk.Label(pill, text='●', style='Card.TLabel', font=('Segoe UI', 13))
        self.dot.pack(side='left', padx=(0, 6))
        self.real = ttk.Label(pill, style='Card.TLabel')
        self.real.pack(side='left')
        tk.Frame(root, background=self.LINE, height=1).pack(fill='x')

        body = ttk.Frame(root, padding=(16, 16, 16, 0))
        body.pack(fill='both', expand=True)
        portal = self.card(body, fill='both', expand=True)
        top = ttk.Frame(portal, style='Card.TFrame')
        top.pack(fill='x')
        ttk.Label(top, text='Virtual portal', style='Section.TLabel').pack(side='left')
        self.count = ttk.Label(top, style='CardMuted.TLabel')
        self.count.pack(side='left', padx=(10, 0), pady=(3, 0))
        if mirror:
            ttk.Button(top, text='Restore saved progress…', command=self.open_restore).pack(side='right')
        ttk.Button(top, text='Unload', command=self.unload_selected).pack(side='right', padx=(0, 8))
        ttk.Button(top, text='Add Skylander…', command=self.open_picker).pack(side='right', padx=(0, 8))

        cols = (('slot', 'SLOT', 60), ('figure', 'FIGURE', 190), ('source', 'SOURCE', 170), ('file', 'FILE', 260))
        table = ttk.Frame(portal, style='Card.TFrame')
        table.pack(fill='both', expand=True, pady=(12, 0))
        self.tree = ttk.Treeview(table, columns=[c[0] for c in cols], show='headings', height=8)
        for key, title, width in cols:
            self.tree.heading(key, text=title, anchor='w')
            self.tree.column(key, width=width, minwidth=width, anchor='w', stretch=key == 'file')
        self.tree.tag_configure('odd', background='#fafafa')
        self.tree.pack(fill='both', expand=True)
        self.placeholder = ttk.Label(table, style='CardMuted.TLabel', justify='center',
                                     text='No figures yet.\nPlace one on the real portal, or load a .sky file.')
        if mirror:
            tk.Frame(portal, background=self.LINE, height=1).pack(fill='x', pady=(12, 10))
            self.wb = tk.BooleanVar(value=self.settings().get('write_back', True))  # on unless turned off
            mirror.set_write_back(self.wb.get())
            log('write-back to real figures ' + ('ON' if self.wb.get() else 'off'))
            self.wb_box = ttk.Checkbutton(portal, text='Write saves to real figures', variable=self.wb,
                                          style='Card.TCheckbutton', command=self.toggle_write_back)
            self.wb_box.pack(anchor='w')
            ttk.Label(portal, style='CardMuted.TLabel', text='Game saves also go onto the figure on the real '
                      'portal. Every block is read back to check it.').pack(anchor='w', padx=(self.box + 1, 0))

        activity = self.card(body, fill='x', pady=(12, 0))
        ttk.Label(activity, text='Activity', style='Section.TLabel').pack(anchor='w')
        logbox = ttk.Frame(activity, style='Card.TFrame')
        logbox.pack(fill='x', pady=(8, 0))
        self.log = tk.Text(logbox, height=8, wrap='word', font=('Consolas', 9), relief='flat', borderwidth=0,
                           background=self.CARD, foreground=self.TEXT, highlightthickness=0, state='disabled',
                           selectbackground=self.SELECTED, selectforeground=self.TEXT)
        self.log.tag_configure('time', foreground='#8a8a8a')
        self.log.tag_configure('bad', foreground=self.BAD)
        scroll = ttk.Scrollbar(logbox, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.log.pack(side='left', fill='both', expand=True)
        self.status = ttk.Label(root, style='Muted.TLabel', padding=(20, 8, 20, 10))
        self.status.pack(fill='x')
        self.refresh()
        root.after(300, self.log.see, 'end')  # see() before the window is drawn does nothing

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
        s.configure('TButton', padding=(14, 5), bordercolor='#d1d1d1', **flat('#fbfbfb'))
        s.map('TButton', **{k: [('pressed', '#ededed'), ('active', '#f5f5f5')] for k in flat('')})
        s.configure('Accent.TButton', foreground='white', bordercolor=A, **flat(A))
        accent_states = [('disabled', '#e0e0e0'), ('pressed', self.ACCENT_DOWN), ('active', self.ACCENT_HOVER)]
        s.map('Accent.TButton', foreground=[('disabled', '#9a9a9a')],
              **{k: accent_states for k in ('bordercolor', *flat(''))})
        self.box = round(line * 1.15)  # Windows 11 checkbox: clam's own is a tiny box with an X
        self.box_images = [self.checkbox(self.box, on, hover) for on in (False, True) for hover in (False, True)]
        off, off_hover, on, on_hover = self.box_images
        s.element_create('Box.indicator', 'image', off, ('selected', 'active', on_hover), ('selected', on),
                         ('active', off_hover), width=self.box + 10, sticky='w')
        s.layout('Card.TCheckbutton', [('Checkbutton.padding', {'sticky': 'nswe', 'children': [
            ('Box.indicator', {'side': 'left', 'sticky': ''}),
            ('Checkbutton.focus', {'side': 'left', 'sticky': 'w', 'children': [('Checkbutton.label', {'sticky': 'nswe'})]})]})])
        s.configure('Card.TCheckbutton', background=C)
        s.map('Card.TCheckbutton', background=[('active', C)])
        s.configure('Treeview', fieldbackground=C, rowheight=int(line * 2.1), bordercolor=C, **flat(C))
        s.map('Treeview', background=[('selected', self.SELECTED)], foreground=[('selected', self.TEXT)])
        s.configure('Treeview.Heading', foreground=self.MUTED, font=('Segoe UI Semibold', 9), relief='flat',
                    padding=(6, 4), bordercolor=C, **flat(C))
        s.map('Treeview.Heading', background=[('active', C)])
        s.layout('Treeview', [('Treeview.treearea', {'sticky': 'nswe'})])  # no frame around the rows
        s.configure('TScrollbar', arrowcolor=self.MUTED, bordercolor=C, gripcount=0, arrowsize=12, **flat('#e0e0e0'))
        s.map('TScrollbar', **{k: [('active', '#cfcfcf')] for k in flat('')})
        s.configure('Search.TEntry', fieldbackground=C, bordercolor='#c8c8c8', lightcolor=C, darkcolor=C, padding=(10, 7))
        s.map('Search.TEntry', bordercolor=[('focus', A)], lightcolor=[('focus', A)])
        s.configure('Hint.TLabel', background=C, foreground='#8a8a8a', font=('Segoe UI', 11))
        s.layout('Tab.TRadiobutton', [('Radiobutton.padding', {'sticky': 'nswe', 'children': [
            ('Radiobutton.label', {'sticky': 'nswe'})]})])
        s.configure('Tab.TRadiobutton', background=C, foreground=self.MUTED, padding=(12, 8), font=('Segoe UI', 10))
        s.map('Tab.TRadiobutton', foreground=[('selected', A), ('active', self.TEXT)],
              font=[('selected', ('Segoe UI Semibold', 10))], background=[('active', '#f5f5f5'), ('selected', C)])

    def checkbox(self, n, on, hover):
        """An n x n antialiased checkbox image on the card colour: rounded box, white tick when on."""
        import tkinter as tk
        rgb = lambda c: [int(c[i:i + 2], 16) for i in (1, 3, 5)]
        fill = rgb(self.ACCENT_HOVER if hover else self.ACCENT) if on else rgb('#f5f5f5' if hover else '#ffffff')
        edge = fill if on else rgb('#5f5f5f' if hover else '#8a8a8a')
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
            rows = {n: (n + 1, figure_name(s.data),
                        f'Real portal, slot {real[n] + 1}' if n in real else
                        'Kept (real portal unplugged)' if n in kept else 'File', os.path.basename(s.path))
                    for n, s in enumerate(self.portal.slots) if s.path}
        if rows != self.rows:
            picked = self.tree.selection()
            self.tree.delete(*self.tree.get_children())
            for k, (n, values) in enumerate(rows.items()):
                self.tree.insert('', 'end', iid=str(n), values=values, tags=('odd',) if k % 2 else ())
            self.tree.selection_set([i for i in picked if self.tree.exists(i)])
            self.count.configure(text=f'{len(rows)} of 16 slots in use')
            if rows:
                self.placeholder.place_forget()
            else:
                self.placeholder.place(relx=0.5, rely=0.55, anchor='center')
            self.rows = rows
        if m:
            colour = next(c for key, c in self.STATE_COLOURS if key in m.state)
            self.dot.configure(foreground=colour)
            self.real.configure(text=m.state[0].upper() + m.state[1:])
            while not m.offers.empty():
                self.open_restore(select=m.offers.get_nowait())
            while not m.notices.empty():
                from tkinter import messagebox
                messagebox.showwarning('Portal Bridge', m.notices.get_nowait(), parent=self.root)
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

    @staticmethod
    def settings():
        try:
            with open(SETTINGS) as f:
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
        for row, name in enumerate(('Backup', 'Differs from figure')):
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
        self.facts[0].configure(text=when)
        self.facts[1].configure(text=f'{n} block{"s" * (n != 1)}' if n else 'nothing (same as the figure)')
        self.explain.configure(text=(
            'This backup holds progress the figure does not have. Writing it replaces the progress on the figure. '
            'What the figure holds now is kept as its backup, so you can undo this here.'))
        self.write_button.state(['!disabled'] if n else ['disabled'])

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
        if not d.winfo_viewable():  # first show: centre on the screen, the main window may be hidden
            d.geometry(f'+{(d.winfo_screenwidth() - d.winfo_reqwidth()) // 2}'
                       f'+{(d.winfo_screenheight() - d.winfo_reqheight()) // 3}')
        d.deiconify()
        d.lift()
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
        try:
            with open(SETTINGS, 'w') as f:
                json.dump({**self.settings(), 'write_back': self.wb.get()}, f)
        except OSError as e:
            log(f'choice not remembered: {e}')
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
        tk.Frame(body, background=self.LINE, height=1).pack(fill='x')

        table = ttk.Frame(body, style='Card.TFrame')
        table.pack(fill='both', expand=True, pady=(6, 0))
        self.lists = {}
        for key, cols in (('yours', (('figure', 'FIGURE', 210), ('uid', 'ID', 90), ('played', 'LAST PLAYED', 130),
                                     ('backup', 'BACKUP', 200))),
                          ('all', (('figure', 'FIGURE', 380), ('kind', 'FIGURE ID', 200)))):
            t = ttk.Treeview(table, columns=[c[0] for c in cols], show='headings', selectmode='browse')
            for c, title, width in cols:
                t.heading(c, text=title, anchor='w')
                t.column(c, width=width, minwidth=60, anchor='w', stretch=c == 'figure')
            t.tag_configure('odd', background='#fafafa')
            t.tag_configure('loaded', foreground='#8a8a8a')
            t.bind('<<TreeviewSelect>>', lambda e: self.picker_buttons())
            t.bind('<Double-1>', lambda e: self.pick('current' if self.tab.get() == 'yours' else 'new'))
            self.lists[key] = t
        self.pick_scroll = ttk.Scrollbar(table)
        self.pick_scroll.pack(side='right', fill='y')
        self.nothing = ttk.Label(table, style='CardMuted.TLabel')

        tk.Frame(p, background=self.LINE, height=1).pack(fill='x')
        bar = ttk.Frame(p, padding=(22, 12))
        bar.pack(fill='x')
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
        w, h = 780, 600
        p.geometry(f'{w}x{h}+{self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2}'
                   f'+{self.root.winfo_rooty() + max(0, (self.root.winfo_height() - h) // 3)}')
        search.focus_set()

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
                rows.append((uid, (name + (' (on the portal)' if uid in loaded else ''), uid,
                                   time.strftime('%Y-%m-%d %H:%M', time.localtime(when)), kind), uid in loaded))
        else:
            rows = [(key, (name, 'id {} · variant {}'.format(*key.split(':'))), False)
                    for key, name in sorted(NAMES.items(), key=lambda kv: kv[1].lower())]
        shown = [r for r in rows if not q or q in r[1][0].lower() or q in r[0]]
        t = self.lists[self.tab.get()]
        t.delete(*t.get_children())
        for n, (iid, values, on) in enumerate(shown):
            t.insert('', 'end', iid=iid, values=values, tags=(('odd',) if n % 2 else ()) + (('loaded',) if on else ()))
        if shown:
            t.selection_set(shown[0][0])
            self.nothing.place_forget()
        else:
            self.nothing.configure(text=f"No Skylander matches '{self.query.get().strip()}'." if q else
                                   'No figures yet. Place one on the real portal, or create one under All Skylanders.')
            self.nothing.place(relx=0.5, rely=0.4, anchor='center')
        self.tab_buttons['yours'].configure(text=f'Your Skylanders ({len(self.yours)})')
        self.tab_buttons['all'].configure(text=f'All Skylanders ({len(NAMES)})')
        self.picker_buttons()

    def picker_buttons(self):
        sel = self.lists[self.tab.get()].selection()
        e = self.yours.get(sel[0]) if sel and self.tab.get() == 'yours' else None
        on = e is not None and 'loaded' in self.lists['yours'].item(e[1], 'tags')
        self.pick_current.state(['!disabled'] if e and e[2] and not on else ['disabled'])
        self.pick_backup.state(['!disabled'] if e and e[3] and not on else ['disabled'])
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
        raise SystemExit(f'Port {TCP_PORT} is in use. Is Portal Bridge already running?')
    LOG_FILE = open(os.path.join(HERE, 'bridge.log'), 'w')
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
        app = App(server, mirror)
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
    except Exception as e:  # PortalBridge.exe has no console: say why it stopped instead of vanishing
        import traceback
        log(traceback.format_exc())
        if not sys.stdout:
            ctypes.windll.user32.MessageBoxW(None, f'Portal Bridge stopped:\n{e}\n\nDetails are in bridge.log.',
                                             'Portal Bridge', 0x10)
        raise
    except SystemExit as e:
        if isinstance(e.code, str) and not sys.stdout:
            ctypes.windll.user32.MessageBoxW(None, f'Portal Bridge stopped: {e.code}', 'Portal Bridge', 0x10)
        raise
