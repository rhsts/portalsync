"""Loopback check: a fake USB/IP client talks to Server/Portal. Run: python test_portal_bridge.py"""
import socket
import struct
import threading
import time
from types import SimpleNamespace

import itertools
import os
import shutil
import subprocess
import sys
import tempfile

import portal_bridge as pb
from portal_bridge import Portal, Server, recv_exact

DEVICE = bytes.fromhex('120100020000004030145001000101020001')
CONFIG = bytes.fromhex('0902290001010080fa' '090400000203000000' '092111010001221d00' '0705810340000107050203400001')
DESC = SimpleNamespace(speed=2, device=DEVICE, config=CONFIG, strings={0: bytes.fromhex('04030904')},
                       reports={0: bytes(29)}, r_reply=bytes.fromhex('52021b').ljust(32, b'\0'))


def submit(c, seq, ep, direction, length, setup=bytes(8), data=b''):
    c.sendall(struct.pack('>IIIIIIIIII', 1, seq, 0x10001, direction, ep, 0, length, 0, 0xFFFFFFFF, 0) + setup + data)


def ret(c):
    h = recv_exact(c, 48)
    cmd, seq, _, _, _, status, actual = struct.unpack_from('>IIIIIiI', h)
    data = recv_exact(c, actual) if cmd == 3 and actual and seq % 2 else b''  # odd seq = IN in this test
    return cmd, seq, status, data


def figure(uid, fid=25):
    """A 1024-byte figure image: uid in block 0, character id at 0x10 (25 = Zook in figure_names.json)."""
    data = bytearray(1024)
    data[0:4] = bytes.fromhex(uid)
    data[0x10:0x12] = fid.to_bytes(2, 'little')
    return bytes(data)


def fake_bin(d):
    """Send recycle() into d/bin instead of the real Recycle Bin; returns that folder."""
    trash = os.path.join(d, 'bin')
    os.makedirs(trash, exist_ok=True)
    pb.recycle = lambda p: shutil.move(p, os.path.join(trash, os.path.basename(p))) is not None
    return trash


def one_backup():
    """keep_one_backup keeps the newest backup ('original' counts as oldest) and bins the rest."""
    with tempfile.TemporaryDirectory() as d:
        pb.DUMPS = d
        trash = fake_bin(d)
        names = ['Zook (86bc1263) - original.sky', 'Zook (86bc1263) - saved 2026-10-10 10.18.32.sky',
                 'Zook (86bc1263) - before restore 2026-10-10 12.57.04.sky', 'Zook (86bc1263) - saved 2026-10-10 12.57.04-2.sky',
                 'Zook (86bc1263) - current.sky', 'Zook (86bc1263) - loaded copy.sky', 'Trigger Happy (427dbe01) - original.sky']
        for name in names:
            open(os.path.join(d, name), 'wb').close()
        assert [os.path.basename(p) for p in pb.backups()['86bc1263']] == [names[0], names[1], names[2], names[3]]
        for uid in pb.backups():
            pb.keep_one_backup(uid)
        assert sorted(f for f in os.listdir(d) if f != 'bin') == sorted([names[3], names[4], names[5], names[6]])
        assert sorted(os.listdir(trash)) == sorted(names[:3])


def names():
    """Readable dump names, and the one-time rename of the old <uid>.* names."""
    assert pb.figure_name(figure('86bc1263')) == 'Zook'
    variant = bytearray(figure('86bc1263'))
    variant[0x1C:0x1E] = (0x7777).to_bytes(2, 'little')
    assert pb.figure_name(variant) == 'Zook (variant 0x7777)' and pb.figure_name(figure('86bc1263', 9999)) == 'Figure 9999'
    with tempfile.TemporaryDirectory() as d:
        pb.DUMPS = d
        old = ['86bc1263.sky', '86bc1263.mirror.sky', '86bc1263.play.sky', '86bc1263.mirror.20261010-125704.sky',
               '86bc1263.mirror.20261010-125704-2.sky', '86bc1263.before-restore.20261010-131453.sky', '86bc1263.play.20261010-125704.sky']
        for n, name in enumerate(old):
            with open(os.path.join(d, name), 'wb') as f:
                f.write(figure('86bc1263')[:-1] + bytes([n]))  # each file distinct
        with open(os.path.join(d, 'Zook (86bc1263) - original.sky.keep'), 'w'):
            pass  # not a dump: untouched
        pb.rename_old_dumps()
        assert sorted(os.listdir(d)) == sorted([
            '86bc1263.play.20261010-125704.sky',  # no such old kind: left alone
            'Zook (86bc1263) - original.sky.keep', 'Zook (86bc1263) - original.sky', 'Zook (86bc1263) - current.sky',
            'Zook (86bc1263) - loaded copy.sky', 'Zook (86bc1263) - saved 2026-10-10 12.57.04.sky',
            'Zook (86bc1263) - saved 2026-10-10 12.57.04-2.sky', 'Zook (86bc1263) - before restore 2026-10-10 13.14.53.sky'])
        assert open(os.path.join(d, 'Zook (86bc1263) - current.sky'), 'rb').read()[-1] == 1  # contents moved along
        with open(os.path.join(d, '86bc1263.sky'), 'wb') as f:
            f.write(figure('86bc1263'))
        pb.rename_old_dumps()  # the new name exists: never overwritten
        assert os.path.exists(os.path.join(d, '86bc1263.sky'))
        assert open(os.path.join(d, 'Zook (86bc1263) - original.sky'), 'rb').read()[-1] == 0


def files():
    """Game writes land in the loaded file; a backup in dumps/ is never written."""
    with tempfile.TemporaryDirectory() as d:
        pb.DUMPS = d
        data = figure('0a0b0c0d')
        backup = os.path.join(d, 'Zook (0a0b0c0d) - original.sky')
        with open(backup, 'wb') as f:
            f.write(data)
        path, loaded = pb.open_dump(backup)
        assert path.endswith('Zook (0a0b0c0d) - loaded copy.sky') and loaded == data
        portal = Portal(DESC.r_reply)
        n = portal.load(loaded, path)
        portal.write(n, 5, bytes([7]) * 16)
        assert open(path, 'rb').read()[80:96] == bytes([7]) * 16
        assert open(backup, 'rb').read() == data
        assert pb.open_dump(backup)[1][80:96] == bytes([7]) * 16  # reload keeps progress
        other = os.path.join(d, 'sub', 'Zook (0a0b0c0d) - original.sky')  # same name outside dumps/: written directly
        os.makedirs(os.path.dirname(other))
        with open(other, 'wb') as f:
            f.write(bytes(1000))
        try:
            pb.open_dump(other)
            assert False, 'wrong size accepted'
        except ValueError:
            pass


class FakeReal:
    def __init__(self, figures):
        self.figures, self.dev, self.sent = figures, None, []  # real slot -> bytearray(1024)
        self.io = threading.Lock()

    def present(self, seconds):
        return list(self.figures)

    def read_block(self, i, block):
        return bytes(self.figures[i][block * 16:block * 16 + 16])

    def read_figure(self, i):
        return bytes(self.figures[i])

    def write_block(self, i, block, data):
        self.figures[i][block * 16:block * 16 + 16] = data

    def send(self, *cmd):
        self.sent.append(bytes(cmd))


def mirror():
    """Mirror: backup, write-back, lights, kept copies on unplug, no data overwritten."""
    with tempfile.TemporaryDirectory() as d:
        pb.DUMPS = d
        trash = fake_bin(d)
        server = Server(Portal(DESC.r_reply), DESC)
        portal, m = server.portal, pb.Mirror(server, vhci_bus=3)
        zook = figure('86bc1263')
        backup, mirror_file = os.path.join(d, 'Zook (86bc1263) - original.sky'), os.path.join(d, 'Zook (86bc1263) - current.sky')
        game_write = lambda block, b: portal.command(bytes([ord('W'), 0x10, block]) + bytes([b]) * 16)
        m.real = FakeReal({2: bytearray(zook)})
        m.poll()
        s = portal.slots[0]
        assert s.path == mirror_file and bytes(s.data) == zook and open(backup, 'rb').read() == zook

        game_write(5, 9)  # write-back off: mirror file only (the live bug: a save just before ticking the box)
        assert open(mirror_file, 'rb').read()[80:96] == bytes([9]) * 16 and m.jobs.empty()
        assert m.set_write_back(True) == 1  # ticking the box catches up block 5
        game_write(6, 7)
        game_write(7, 7)  # sector trailer: refused
        portal.command(b'C\xff\x00\x00')  # light: forwarded
        m.poll()
        real = m.real.figures[2]
        assert real[80:96] == bytes([9]) * 16 and real[96:112] == bytes([7]) * 16 and real[112:128] == bytes(16)
        assert bytes(real) == open(mirror_file, 'rb').read()[:112] + bytes(16) + open(mirror_file, 'rb').read()[128:]
        assert m.real.sent and m.real.sent[0][:4] == b'C\xff\x00\x00'
        assert open(backup, 'rb').read() == zook and m.set_write_back(True) == 0  # nothing left to catch up

        m.set_write_back(False)
        game_write(9, 4)  # write-back off again: figure lacks block 9
        m.real.figures = {}  # lifted
        m.poll()
        assert s.path is None and s.status == pb.REMOVING
        m.real.figures = {2: bytearray(real)}  # placed again: mirror file has block 9 the figure lacks -> archived
        m.poll()
        archived = [f for f in os.listdir(d) if f.startswith('Zook (86bc1263) - saved ')]
        assert s.path == mirror_file and len(archived) == 1  # the newest backup; the original went to the bin
        assert os.listdir(trash) == [os.path.basename(backup)] and open(os.path.join(trash, os.path.basename(backup)), 'rb').read() == zook
        assert open(os.path.join(d, archived[0]), 'rb').read()[144:160] == bytes([4]) * 16

        m.release()  # unplugged: copy stays loaded
        assert s.path == mirror_file and '86bc1263' in m.kept and m.real is None
        game_write(8, 3)  # played while unplugged: mirror file only
        m.set_write_back(True)
        m.real, m.fresh = FakeReal({4: bytearray(real)}), True  # plugged back, figure in another real slot
        m.poll()
        assert m.slots == {4: (0, mirror_file, '86bc1263')} and not m.kept
        assert sum(1 for x in portal.slots if x.path) == 1  # no duplicate
        assert m.real.figures[4][128:144] == bytes([3]) * 16  # save made while unplugged written on return
        m.release()
        m.real, m.fresh = FakeReal({}), True  # plugged back without the figure: copy removed
        m.poll()
        assert s.path is None and not m.kept


def restoring():
    """One backup per figure; restoring writes it to the figure, and what the figure held becomes the backup."""
    with tempfile.TemporaryDirectory() as d:
        pb.DUMPS = d
        trash = fake_bin(d)
        server = Server(Portal(DESC.r_reply), DESC)
        portal, m = server.portal, pb.Mirror(server, vhci_bus=3)
        zook = figure('86bc1263')
        read = lambda *p: open(os.path.join(*p), 'rb').read()
        kept = lambda: [os.path.basename(p) for p in pb.backups('86bc1263').get('86bc1263', [])]
        m.real = FakeReal({2: bytearray(zook)})
        m.poll()
        assert kept() == ['Zook (86bc1263) - original.sky']
        portal.command(bytes([ord('W'), 0x10, 5]) + bytes([9]) * 16)  # write-back off: current copy only
        m.real.figures = {}
        m.poll()  # lifted
        m.real.figures = {2: bytearray(zook)}
        m.poll()  # placed again: the current copy had progress the figure lacks -> saved, offered, the only backup
        offer = m.offers.get_nowait()
        progress = read(offer)
        assert kept() == [os.path.basename(offer)] and os.listdir(trash) == ['Zook (86bc1263) - original.sky']
        (path, label, what, n), = m.saved_copies()
        assert (path, label, n) == (offer, 'Zook (86bc1263)', 1) and what.startswith('saved ') and ':' in what

        portal.command(bytes([ord('W'), 0x10, 6]) + bytes([4]) * 16)  # unsynced game progress (write-back off)
        assert m.restore(offer) == '86bc1263'
        m.poll()  # the restore job
        s = portal.slots[0]
        snapshot, = kept()
        assert snapshot.startswith('Zook (86bc1263) - before restore ') and read(d, snapshot) == zook  # undo
        binned = [read(trash, f) for f in os.listdir(trash)]
        assert progress in binned and any(b[96:112] == bytes([4]) * 16 for b in binned)  # recoverable, not lost
        assert bytes(s.data) == progress == read(s.path) == bytes(m.real.figures[2])
        assert pb.REMOVING in s.queued and list(s.queued)[-2:] == [pb.ADDED, pb.READY]  # lifted + placed for the game

        source = os.path.join(d, 'progress.sky')
        with open(source, 'wb') as f:
            f.write(progress)
        m.set_write_back(True)
        portal.command(bytes([ord('W'), 0x10, 6]) + bytes([5]) * 16)  # a write still queued when restore is asked
        m.restore(source)
        m.poll()  # the write runs first, then the restore puts block 6 back
        snapshot, = kept()
        assert bytes(m.real.figures[2]) == progress and read(d, snapshot)[96:112] == bytes([5]) * 16
        m.set_write_back(False)

        m.restore(source)
        m.real.figures = {}
        m.poll()  # lifted before the job ran: nothing written, backup unchanged
        assert kept() == [snapshot]

        for bad in (os.path.join(d, 'short.sky'), os.path.join(d, 'other.sky')):
            with open(bad, 'wb') as f:
                f.write(bytes(10) if 'short' in bad else bytes.fromhex('0a0b0c0d') + bytes(1020))
            try:
                m.restore(bad)
                assert False, f'{bad} restored'
            except ValueError:
                pass


def sound():
    """Portal speaker blocks go to the PC's audio in 20 ms buffers (all zeros here: nothing is heard)."""
    s = pb.PortalSound()
    for _ in range(12):
        s.feed(bytes(64))
    if not s.out:
        return print('no audio device: portal sound not checked')
    assert len(s.pending) == 2 and len(s.partial) == 2 * 64  # 10 blocks queued as 2 buffers, 2 waiting
    s.MAX_QUEUED = 0  # as if 0.5 s were already queued
    for _ in range(3):
        s.feed(bytes(64))
    assert s.dropped == 5 and len(s.pending) <= 2
    s.close()  # without it Windows plays freed buffers: the process crashed here once
    s.feed(bytes(64))
    assert s.out is False and not s.pending


def speaker():
    """Portal sound goes to the Traptanium portal's speaker; without one, or after a failed write, to the PC."""
    server = Server(Portal(DESC.r_reply), DESC)
    m, played = pb.Mirror(server, vhci_bus=3), []

    class Dev:  # the real portal's OUT endpoint
        def write(self, ep, data, timeout):
            played.append((ep, bytes(data)))
            return len(data)

    m.real = FakeReal({})
    m.real.dev = Dev()
    assert not m.play(bytes(64))  # not a Traptanium portal (R reply): no speaker
    m.speaker = True
    assert m.play(bytes([1]) * 64) and played == [(2, bytes([1]) * 64)]
    s = pb.PortalSound(speaker=m.play)
    s.feed(bytes(64))
    assert s.on_portal == 1 and s.out is None  # the PC's audio device is never opened
    server.portal.command(b'M\x01')  # the game turns the speaker on: passed to the real portal
    assert m.jobs.get_nowait() == ('raw', b'M\x01'.ljust(32, b'\0'))
    m.real.dev = None  # the portal went away mid-sound
    assert not m.play(bytes(64)) and not m.speaker  # from now on the PC plays it
    m.release()
    assert m.real is None and not m.speaker


def lifted_mid_write():
    """A figure lifted while write-back writes to it fails that block only; the portal is kept."""
    with tempfile.TemporaryDirectory() as d:
        pb.DUMPS = d
        server = Server(Portal(DESC.r_reply), DESC)
        portal, m = server.portal, pb.Mirror(server, vhci_bus=3)
        m.real = FakeReal({5: bytearray(figure('703b00ab'))})
        m.poll()
        m.set_write_back(True)

        def gone(i, block, data):
            raise TimeoutError("no 'W' reply from real portal")  # what the live log showed
        m.real.write_block = gone
        portal.command(bytes([ord('W'), 0x10, 0x24]) + bytes([1]) * 16)
        m.poll()  # must not raise: run() would drop the portal as "in use by another program"
        assert m.real is not None and m.slots and m.jobs.empty()

        fig = m.real.figures.pop(5)  # lifted with the save unwritten
        m.poll()
        del m.real.write_block
        m.real.figures[5] = fig  # placed again unchanged: the save is finished, no restore offer
        m.poll()
        assert fig[0x24 * 16:0x25 * 16] == bytes([1]) * 16 and m.offers.empty()
        assert not [f for f in os.listdir(d) if ' - saved ' in f]


def creating():
    """New blank figures (Add Skylander → All Skylanders) and the list of figures already played."""
    assert pb.crc16(b'123456789') == 0x29B1  # CRC-16/CCITT-FALSE check value
    with tempfile.TemporaryDirectory() as d:
        pb.DUMPS = d
        f = pb.new_figure(25, 0)
        assert len(f) == 1024 and pb.figure_name(f) == 'Zook'
        assert int.from_bytes(f[0x1E:0x20], 'little') == pb.crc16(f[:0x1E])
        assert f[4] == f[0] ^ f[1] ^ f[2] ^ f[3] and f[5:8] == b'\x81\x01\x0f'
        assert f[48:64] == bytes(6) + bytes.fromhex('0f0f0f69') + bytes(6)  # keys zero, as the real portal reads them
        assert all(f[s * 64 + 48:s * 64 + 64] == bytes(6) + bytes.fromhex('7f0f0869') + bytes(6) for s in range(1, 16))
        assert all(not any(f[b * 16:b * 16 + 16]) for b in range(2, 64) if b % 4 != 3)  # empty save area

        def touch(name, when):
            path = os.path.join(d, name)
            open(path, 'wb').write(bytes(1024))
            os.utime(path, (when, when))
        touch('Zook (86bc1263) - current.sky', 200)
        touch('Zook (86bc1263) - original.sky', 50)
        touch('Zook (86bc1263) - saved 2026-10-10 12.57.04.sky', 100)
        touch('Zook (86bc1263) - loaded copy.sky', 150)
        touch('Kaos (9f697a0a) - current.sky', 300)
        touch('notes.txt', 400)
        found = pb.played()
        assert [e[1] for e in found] == ['9f697a0a', '86bc1263'] and found[0][3] is None
        assert found[1][2].endswith('Zook (86bc1263) - current.sky') and found[1][3].endswith('saved 2026-10-10 12.57.04.sky')


def game_first():
    """Busy real portal while an emulator runs: its game booted before the bridge. Say what to do."""
    m = pb.Mirror(Server(Portal(DESC.r_reply), DESC), vhci_bus=3)
    portal = SimpleNamespace(bus=1, address=21)
    real_devices, pb.portal_devices = pb.portal_devices, lambda: [portal]
    try:
        m.real = FakeReal({})
        m.real.dev = portal
        pb.RUNNING_EMULATORS = {'23236': 'Cemu.exe'}
        m.lost(TimeoutError("no 'R' reply from real portal"))  # what the live log showed
        assert m.busy is None and m.state.startswith('real portal: lost')  # a timeout alone: claim again
        m.busy = (1, 21)  # set by poll() until the handshake works, as in the live run
        m.real = FakeReal({})
        m.real.dev = portal
        m.lost(TimeoutError("no 'R' reply from real portal"))
        assert m.state.startswith('real portal: in use by Cemu.exe') and 'Start the game again' in m.notices.get_nowait()
        m.lost(TimeoutError("no 'R' reply from real portal"))
        assert m.notices.empty()  # once per state, not every second
        pb.RUNNING_EMULATORS = {}
        m.real, m.state = FakeReal({}), ''
        m.real.dev = portal
        m.lost(OSError('another program is reading the real portal'))
        assert m.state.startswith('real portal: in use by another program') and m.notices.empty()
        m.busy, m.real, m.state = None, FakeReal({}), ''
        m.real.dev = portal
        pb.RUNNING_EMULATORS = {'420': 'Cemu.exe'}

        class USBTimeoutError(OSError):  # pyusb's, as raised while the speaker played (Errno 10060)
            pass
        m.lost(USBTimeoutError(10060, 'Operation timed out'))
        assert m.busy is None and m.state.startswith('real portal: lost') and m.notices.empty()  # no false pop-up
    finally:
        pb.portal_devices, pb.RUNNING_EMULATORS = real_devices, {}


def contention():
    """Only skipped counter values mean another program reads the portal; a repeated report does not."""
    rp = pb.RealPortal(None)

    def reports(counters):
        it = iter(counters)
        return lambda first: bytes([ord('S'), 1, 0, 0, 0, next(it) & 0xFF, 1])  # slot 0 holds a figure

    rp.wait = reports(itertools.chain([5, 5, 6, 6, 7], itertools.count(8)))  # repeats, as measured
    assert rp.present(0.05) == [0]
    rp.wait = reports(itertools.count(0, 2))  # every other report taken by someone else
    try:
        rp.present(0.05)
        assert False, 'contention not noticed'
    except OSError:
        pass


def file_trouble():
    """A file error while copying a figure skips that figure; the real portal is not treated as gone."""
    with tempfile.TemporaryDirectory() as d:
        pb.DUMPS = os.path.join(d, 'blocked')
        open(pb.DUMPS, 'w').close()  # dumps/ can't be created
        m = pb.Mirror(Server(Portal(DESC.r_reply), DESC), vhci_bus=3)
        m.real = FakeReal({0: bytearray(figure('86bc1263'))})
        m.poll()
        assert m.failed == {0} and m.real is not None and not m.slots


CHILD = """import ctypes, sys, time
ctypes.WinDLL('setupapi')  # portal_hide.dll patches it, like in Cemu which imports it
k = ctypes.WinDLL('kernel32')
k.GetModuleHandleW.restype = ctypes.c_void_p
print('ready', flush=True)
end = time.time() + 10
while time.time() < end and not k.GetModuleHandleW('portal_hide.dll'):
    time.sleep(0.05)
sys.exit(0 if k.GetModuleHandleW('portal_hide.dll') else 1)
"""


def injection():
    """inject() gets portal_hide.dll into another process, the way the bridge does it to Cemu."""
    if not os.path.exists(pb.HIDE_DLL):
        return print('portal_hide.dll not built: injection not checked')
    child = subprocess.Popen([sys.executable, '-I', '-c', CHILD], stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == 'ready'
    assert pb.inject(child.pid, pb.HIDE_DLL)
    assert child.wait(10) == 0


def saved_figure(uid, level_xp=(33000, 10203), gold=1234, nickname='raf'):
    """An encrypted figure image with one valid save area, as a game writes it (Zook, id 25)."""
    d = bytearray(pb.new_figure(25, 0))
    d[0:4] = bytes.fromhex(uid)
    d[4] = d[0] ^ d[1] ^ d[2] ^ d[3]
    struct.pack_into('<H', d, 0x1E, pb.crc16(d[:0x1E]))
    a = 0x80
    d[a:a + 3] = level_xp[0].to_bytes(3, 'little')
    struct.pack_into('<H', d, a + 3, gold)
    d[a + 9] = 1  # area counter
    d[a + 0x20:a + 0x20 + 2 * len(nickname)] = nickname.encode('utf-16-le')
    struct.pack_into('<H', d, 0x110 + 3, level_xp[1])
    d[0x112] = 1
    (_, t1), (_, t2), (_, t3) = pb.area_checksums(d, a)
    struct.pack_into('<HH', d, a + 0x0A, t3, t2)
    struct.pack_into('<H', d, a + 0x0E, pb.area_checksums(d, a)[0][1])  # type 1 covers types 2 and 3
    return pb.crypt(bytes(d), encrypt=True)


def reading(figure_info):
    """Level, gold and nickname from a figure's encrypted save, and damaged saves never offered or written."""
    pb.figure_info = figure_info
    with tempfile.TemporaryDirectory() as d:
        pb.DUMPS = d
        trash = fake_bin(d)
        good = saved_figure('1a2b3c4d')
        assert pb.crypt(pb.crypt(good, encrypt=True)) == good  # round trip
        info = pb.figure_info(good)
        assert (info.kind, info.level, info.gold, info.nickname, info.damaged) == ('skylander', 11, 1234, 'raf', False)
        assert [pb.figure_info(saved_figure('1a2b3c4d', (xp, 0))).level for xp in (0, 999, 1000, 32999, 33000)] == [1, 1, 2, 9, 10]
        assert pb.figure_info(saved_figure('1a2b3c4d', (33000, 63500))).level == 15
        broken = bytearray(good)
        broken[0xD0] ^= 1  # a byte in block 13, covered by checksum type 3
        assert pb.figure_info(bytes(broken)).damaged
        trap = bytearray(broken)
        struct.pack_into('<H', trap, 0x10, 210)
        assert pb.figure_info(bytes(trap)).kind == 'trap' and not pb.figure_info(bytes(trap)).damaged  # traps: no save checks
        assert pb.figure_info(pb.new_figure(25, 0)).kind == 'new'
        t = bytearray(pb.new_figure(212, 0x3006))  # an Air trap: Bad Juju trapped last, Buzzer Beak before
        t[0x81], t[0x89], t[0x90], t[0xD0] = 2, 1, 37, 12
        info = pb.figure_info(pb.crypt(bytes(t), encrypt=True))
        assert info.kind == 'trap' and info.villains == ['Bad Juju', 'Buzzer Beak'] and pb.stats(info) == ' · holds Bad Juju'
        assert pb.figure_info(pb.new_figure(212, 0x3006)).villains == []  # empty trap

        # backups: the newest intact one is kept
        old, new = os.path.join(d, 'Zook (1a2b3c4d) - original.sky'), os.path.join(d, 'Zook (1a2b3c4d) - saved 2026-10-16 10.00.00.sky')
        pb.save_dump(old, good)
        pb.save_dump(new, bytes(broken))
        pb.keep_one_backup('1a2b3c4d')
        assert os.path.exists(old) and os.listdir(trash) == [os.path.basename(new)]

        # a damaged figure placed: its intact backup that differs is offered
        server = Server(Portal(DESC.r_reply), DESC)
        m = pb.Mirror(server, vhci_bus=3)
        pb.save_dump(old, saved_figure('1a2b3c4d', gold=999))
        m.real = FakeReal({0: bytearray(broken)})
        m.poll()
        assert m.offers.get_nowait() == old and m.notices.empty()
        # no intact backup: a pop-up says so
        other = bytearray(broken)
        other[0:4] = bytes.fromhex('5e6f7a8b')
        m.real.figures = {0: bytearray(broken), 1: other}
        m.poll()
        assert m.offers.empty() and 'damaged' in m.notices.get_nowait()

        # reset: the save blocks become empty, header and trailers stay, what the figure held is the backup
        zook = saved_figure('86bc1263')
        pb.save_dump(os.path.join(d, 'Zook (86bc1263) - original.sky'), zook)
        m.real.figures = {2: bytearray(zook)}
        m.poll()
        m.reset('86bc1263')
        m.poll()
        fig = bytes(m.real.figures[2])
        assert fig[:128] == zook[:128] and pb.figure_info(fig).kind == 'new'
        assert all(fig[b * 16:b * 16 + 16] == (zook[b * 16:b * 16 + 16] if b % 4 == 3 else bytes(16)) for b in range(8, 64))
        undo = [f for f in os.listdir(d) if f.startswith('Zook (86bc1263) - before restore')]
        assert len(undo) == 1 and open(os.path.join(d, undo[0]), 'rb').read() == zook

    assert pb.newer('v1.3.0', '1.2.0') and pb.newer('v1.10.0', '1.9.9') and not pb.newer('v1.2.0', '1.2.0')
    assert not pb.newer('v1.1.1', '1.2.0') and not pb.newer('nightly', '1.2.0')


def main():
    reading(pb.figure_info)
    pb.figure_info = lambda data: SimpleNamespace(kind='other', level=None, gold=None, nickname='', damaged=False,
                                                  old_damaged=False)  # the other tests' saves are made-up bytes
    names()
    one_backup()
    files()
    mirror()
    restoring()
    sound()
    speaker()
    lifted_mid_write()
    creating()
    game_first()
    contention()
    file_trouble()
    injection()
    portal = Portal(DESC.r_reply)
    figure = bytes(range(256)) * 4
    assert portal.load(figure) == 0
    lst = socket.create_server(('127.0.0.1', 0))
    threading.Thread(target=Server(portal, DESC).serve, args=(lst,), daemon=True).start()
    addr = lst.getsockname()

    c = socket.create_connection(addr)
    c.sendall(struct.pack('>HHI', 0x0111, 0x8005, 0))
    assert struct.unpack('>HHII', recv_exact(c, 12)) == (0x0111, 5, 0, 1)
    info = recv_exact(c, 0x138 + 4)
    assert info[256:259] == b'1-1' and struct.unpack_from('>HH', info, 0x12C) == (0x1430, 0x0150)
    assert info[-4:] == b'\x03\x00\x00\x00'  # HID interface

    c = socket.create_connection(addr)
    c.sendall(struct.pack('>HHI', 0x0111, 0x8003, 0) + b'1-1'.ljust(32, b'\0'))
    assert struct.unpack('>HHI', recv_exact(c, 8))[2] == 0
    recv_exact(c, 0x138)

    submit(c, 1, 0, 1, 18, struct.pack('<BBHHH', 0x80, 6, 0x0100, 0, 18))
    assert ret(c) == (3, 1, 0, DEVICE)
    submit(c, 3, 0, 1, 255, struct.pack('<BBHHH', 0x80, 6, 0x0300 | 0xEE, 0x0409, 255))
    assert ret(c)[2] == -32  # unknown string -> STALL
    submit(c, 17, 0, 1, 1000, struct.pack('<BBHHH', 0x80, 6, 0x0200, 0, 1000))  # RPCS3 asks for 1000 bytes
    assert ret(c) == (3, 17, 0, CONFIG)
    submit(c, 18, 2, 0, 32, data=bytes(32))  # RPCS3 sends 32-byte interrupt OUT (audio) to endpoint 2
    assert ret(c) == (3, 18, 0, b'')

    report = lambda seq, *cmd: submit(c, seq, 0, 0, 32, struct.pack('<BBHHH', 0x21, 9, 0x0200, 0, 32),
                                      bytes(cmd).ljust(32, b'\0'))
    report(2, ord('R'))
    assert ret(c) == (3, 2, 0, b'')
    submit(c, 5, 1, 1, 32)
    assert ret(c)[3] == DESC.r_reply

    submit(c, 15, 1, 1, 32)  # inactive portal sends no 'S' reports, so this read stays pending
    time.sleep(0.05)
    c.sendall(struct.pack('>IIIIII', 2, 16, 0x10001, 0, 0, 15) + bytes(24))
    assert ret(c) == (4, 16, -104, b'')

    report(4, ord('A'), 1)
    ret(c)
    submit(c, 7, 1, 1, 32)
    assert ret(c)[3][:4] == bytes([0x41, 1, 0xFF, 0x77])
    submit(c, 9, 1, 1, 32)
    s = ret(c)[3]
    assert s[0] == ord('S') and s[1] & 3 == 3 and s[6] == 1  # slot 0 ADDED, portal active

    report(6, ord('Q'), 0x10, 2)
    ret(c)
    submit(c, 11, 1, 1, 32)
    assert ret(c)[3][:19] == bytes([ord('Q'), 0x10, 2]) + figure[32:48]

    report(8, ord('W'), 0x10, 2, *range(16))
    ret(c)
    submit(c, 13, 1, 1, 32)
    assert ret(c)[3][:3] == bytes([ord('W'), 0x10, 2]) and portal.slots[0].data[32:48] == bytes(range(16))
    c.close()
    time.sleep(0.2)  # let the server thread log the disconnect before interpreter shutdown
    print('ok')


if __name__ == '__main__':
    main()
