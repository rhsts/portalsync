"""Loopback check: a fake USB/IP client talks to Server/Portal. Run: python test_portalsync.py"""
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

import portalsync as pb
from portalsync import Portal, Server, recv_exact

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
            with open(os.path.join(d, name), 'wb') as f:
                f.write(figure('86bc1263'))
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
        self.fail = {}  # block -> how many more writes of it time out
        self.io = threading.Lock()

    def present(self, seconds):
        return list(self.figures)

    def read_block(self, i, block):
        return bytes(self.figures[i][block * 16:block * 16 + 16])

    def read_figure(self, i):
        return bytes(self.figures[i])

    def write_block(self, i, block, data):
        if self.fail.get(block):
            self.fail[block] -= 1
            raise TimeoutError(f'block {block}: simulated failure')
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
        job = m.restore(offer)
        assert (job.uid, job.state) == ('86bc1263', 'queued')
        m.poll()  # the restore job
        assert job.state == 'completed' and m.finished.get_nowait() is job
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


def restore_setup(d):
    """(portal, mirror, restored data, path of a saved copy) for Zook on real slot 2; restore retries don't sleep."""
    pb.DUMPS = d
    fake_bin(d)
    server = Server(Portal(DESC.r_reply), DESC)
    m = pb.Mirror(server, vhci_bus=3)
    m.RETRY_DELAY = 0
    m.real = FakeReal({2: bytearray(figure('86bc1263'))})
    m.poll()
    new = bytearray(figure('86bc1263'))
    new[80:96], new[96:112], new[144:160] = bytes([9]) * 16, bytes([8]) * 16, bytes([7]) * 16  # blocks 5, 6, 9
    path = os.path.join(d, 'progress.sky')
    with open(path, 'wb') as f:
        f.write(new)
    return server.portal, m, bytes(new), path


def restore_retries():
    """A restore retries a block that fails, works with write-back off, and the window hears what happened."""
    with tempfile.TemporaryDirectory() as d:
        portal, m, new, path = restore_setup(d)
        assert not m.write_back  # explicit restores write directly
        m.real.fail = {6: 2}  # transient: the third attempt works
        job = m.restore(path)
        m.poll()
        assert (job.state, job.written, job.total, job.message) == ('completed', 3, 3, '')
        assert bytes(m.real.figures[2]) == new == bytes(portal.slots[0].data) and m.finished.get_nowait() is job
        said = []
        ui = SimpleNamespace(say=said.append, alert=lambda msg, ask=False: said.append(msg), mirror=m)
        pb.App.restore_ended(ui, job)
        assert said == ['Restored progress.sky to figure 86bc1263: 3 block(s) written and verified.']
        job.done = 'Repaired.'
        pb.App.restore_ended(ui, job)
        assert said[-1] == 'Repaired.'


def restore_incomplete():
    """A block that keeps failing ends incomplete; the before-restore copy stays the backup and a retry reuses it."""
    with tempfile.TemporaryDirectory() as d:
        portal, m, new, path = restore_setup(d)
        zook = figure('86bc1263')
        before = lambda: [f for f in os.listdir(d) if ' - before restore ' in f]
        m.real.fail = {6: 99}
        job = m.restore(path)
        m.poll()
        assert (job.state, job.written, job.total, job.message) == ('incomplete', 2, 3, '')
        assert pb.backups('86bc1263')['86bc1263'] == [job.snapshot] and open(job.snapshot, 'rb').read() == zook
        said, answers = [], [True]
        ui = SimpleNamespace(say=said.append, alert=lambda msg, ask=False: said.append(msg) or answers.pop(), mirror=m)
        assert m.finished.get_nowait() is job
        pb.App.restore_ended(ui, job)
        assert said[0].startswith('Writing progress.sky to figure 86bc1263 is incomplete: 2 of 3 blocks were written')
        assert f'({os.path.basename(job.snapshot)})' in said[0] and said[1] == 'Writing progress.sky to figure 86bc1263…'
        m.real.fail = {}
        m.poll()  # the retry the Yes queued
        retry = m.finished.get_nowait()
        assert retry is not job and (retry.state, retry.written, retry.total) == ('completed', 1, 1)
        assert retry.snapshot == job.snapshot and before() == [os.path.basename(job.snapshot)]  # no half-written backup
        assert pb.backups('86bc1263')['86bc1263'] == [job.snapshot] and bytes(m.real.figures[2]) == new


def restore_swapped():
    """A figure replaced before or during a restore never gets written."""
    other = figure('aabbccdd')
    with tempfile.TemporaryDirectory() as d:
        portal, m, new, path = restore_setup(d)
        job = m.restore(path)
        m.real.figures[2] = bytearray(other)  # swapped before the job ran
        m.poll()
        assert job.state == 'cancelled' and 'another figure' in job.message and bytes(m.real.figures[2]) == other
        said = []
        pb.App.restore_ended(SimpleNamespace(say=said.append), job)
        assert said == ['Not written to figure 86bc1263: another figure is in real slot 2']
    with tempfile.TemporaryDirectory() as d:
        portal, m, new, path = restore_setup(d)
        real, read = m.real, m.real.read_block

        def read_then_swap(i, block):  # swapped right after block 5 was verified
            r = read(i, block)
            if block == 5:
                real.figures[i] = bytearray(other)
            return r
        real.read_block = read_then_swap
        job = m.restore(path)
        m.poll()
        assert (job.state, job.written, job.total) == ('incomplete', 1, 3) and 'replaced' in job.message
        assert bytes(real.figures[2]) == other  # nothing written to the wrong figure


def restore_order():
    """Queued game writes never land over a restore: older ones run first, stale retries and data are dropped."""
    with tempfile.TemporaryDirectory() as d:
        portal, m, new, path = restore_setup(d)
        game_write = lambda block, b: portal.command(bytes([ord('W'), 0x10, block]) + bytes([b]) * 16)
        m.set_write_back(True)
        game_write(5, 1)  # queued before the restore: runs first, so the snapshot holds it
        job = m.restore(path)
        m.poll()
        assert job.state == 'completed' and open(job.snapshot, 'rb').read()[80:96] == bytes([1]) * 16
        assert bytes(m.real.figures[2]) == new

        old = m.real.figures[2][:] = figure('86bc1263')  # as if never restored, so each restore has work to do
        m.RETRY_DELAY = 1000
        m.real.fail = {6: 1}
        game_write(6, 2)
        m.poll()  # the write fails: a retry waits
        assert m.retries
        job = m.restore(path)
        for r in m.retries.values():
            r.due = 0  # due by the time the restore ran
        m.poll()
        assert (job.state, job.written) == ('completed', 3) and not m.retries and bytes(m.real.figures[2]) == new

        m.real.figures[2][:] = old
        job = m.restore(path)
        game_write(5, 3)  # old data queued after the restore was asked, before it ran
        m.poll()
        assert (job.state, job.written) == ('completed', 3)
        assert bytes(m.real.figures[2]) == new == bytes(portal.slots[0].data)


def restore_unsaved():
    """A game copy whose last save failed: the restore keeps those bytes as a copy, never rescues them over the
    restored file, and leaves one slot for the figure."""
    with tempfile.TemporaryDirectory() as d:
        portal, m, new, path = restore_setup(d)
        s = portal.slots[0]
        s.data[160:176], s.unsaved, s.attempt = bytes([5]) * 16, 'simulated: disk full', 2  # block 10, only in memory
        mirror_file = s.path

        def no_rescue(slot):
            raise AssertionError('rescue would write the old game bytes over the restored file')
        portal.rescue = no_rescue
        job = m.restore(path)
        m.poll()
        assert job.state == 'completed' and open(mirror_file, 'rb').read() == new
        loaded = [x for x in portal.slots if x.path == mirror_file]
        assert len(loaded) == 1 and loaded[0].unsaved is None and bytes(loaded[0].data) == new
        binned = [open(os.path.join(d, 'bin', f), 'rb').read() for f in os.listdir(os.path.join(d, 'bin'))]
        assert any(b[160:176] == bytes([5]) * 16 for b in binned)  # the unsaved progress: recoverable, not lost


def restore_cancel():
    """cancel_pending_restores ends queued restores as cancelled and keeps other jobs in order; so does a lost portal."""
    with tempfile.TemporaryDirectory() as d:
        portal, m, new, path = restore_setup(d)
        m.set_write_back(True)
        portal.command(b'C\xff\x00\x00')  # light
        job = m.restore(path)
        portal.command(bytes([ord('W'), 0x10, 5]) + bytes([1]) * 16)
        m.cancel_pending_restores('PortalSync is closing')
        assert (job.state, job.message) == ('cancelled', 'PortalSync is closing') and m.finished.get_nowait() is job
        assert [j[0] for j in m.jobs.queue] == ['raw', 'write']
        m.poll()
        assert m.real.figures[2][80:96] == bytes([1]) * 16 and m.real.figures[2][96:112] == bytes(16)  # no restore
        job = m.restore(path)
        m.release()
        assert job.state == 'cancelled' and m.finished.get_nowait() is job and m.jobs.empty()


def cancelled_wait():
    """RealPortal.wait ends at once when cancelled, instead of after its timeout."""
    class Dev:
        def read(self, ep, n, timeout):
            time.sleep(0.01)
            return b'S'.ljust(32, b'\0')  # never the awaited reply
    stop = threading.Event()
    rp = pb.RealPortal(Dev(), stop.is_set)
    threading.Timer(0.1, stop.set).start()
    t = time.monotonic()
    try:
        rp.wait('Q', timeout=5)
        assert False, 'no reply expected'
    except TimeoutError as e:
        assert 'closing' in str(e) and time.monotonic() - t < 1


def stop_setup(d, blocking_write):
    """restore_setup with a restore running in the Mirror thread, stuck in blocking_write; returns
    (mirror, job, what happened: ('release', thread alive?) entries)."""
    portal, m, new, path = restore_setup(d)
    started, events = threading.Event(), []
    m.real.write_block = lambda i, block, data: (started.set(), blocking_write(m))
    release = m.release
    m.release = lambda: (events.append(('release', m.thread.is_alive())), release())
    job = m.restore(path)
    m.thread = threading.Thread(target=m.run, daemon=True)
    m.thread.start()
    assert started.wait(5)
    return m, job, events


def stop_running_restore():
    """stop() ends a running restore through its error path, then lets go of USB once the thread ended."""
    def until_cancelled(m):  # like RealPortal.wait
        while not m.stopping:
            time.sleep(0.01)
        raise TimeoutError('PortalSync is closing')
    real_devices, pb.portal_devices = pb.portal_devices, lambda: []
    try:
        with tempfile.TemporaryDirectory() as d:
            m, job, events = stop_setup(d, until_cancelled)
            m.STOP_TIMEOUT = 5
            assert m.stop() is True and not m.thread.is_alive()
            assert (job.state, job.message) == ('cancelled', 'PortalSync is closing') and m.finished.get_nowait() is job
            assert events == [('release', False)] and m.real is None
    finally:
        pb.portal_devices = real_devices


def stop_timeout():
    """A thread that does not stop in time: stop() says so and leaves the USB handle alone."""
    go = threading.Event()
    with tempfile.TemporaryDirectory() as d:
        m, job, events = stop_setup(d, lambda m: go.wait(5))  # ignores the cancel
        m.STOP_TIMEOUT = 0.2
        assert m.stop() is False and events == [] and m.real is not None
        go.set()
        m.thread.join(5)
        assert not m.thread.is_alive() and events == []


def stop_refuses():
    """After stop(): game writes queue nothing, restores are refused, and queued write-backs are reported."""
    with tempfile.TemporaryDirectory() as d:
        portal, m, new, path = restore_setup(d)
        game_write = lambda block, b: portal.command(bytes([ord('W'), 0x10, block]) + bytes([b]) * 16)
        m.set_write_back(True)
        game_write(5, 1)
        game_write(6, 1)
        m.thread = SimpleNamespace(join=lambda timeout: None, is_alive=lambda: True)  # still busy: slots stay
        seen = len(pb.LOG_LINES)
        assert m.stop() is False and m.jobs.empty() and m.slots
        assert any('2 game save block(s) not written' in line for _, line in list(pb.LOG_LINES)[seen:])
        game_write(7, 1)
        assert m.jobs.empty()
        for call in (lambda: m.restore(path), lambda: m.write_image('86bc1263', bytes, 'x')):
            try:
                call()
                assert False, 'work accepted while closing'
            except ValueError as e:
                assert str(e) == 'PortalSync is closing'


def shutting_down():
    """shutdown(): a failing step doesn't skip the rest; progress the disk refuses at exit is rescued."""
    real_save, real_unsaved, real_usbip = pb.save_dump, pb.UNSAVED, pb.usbip
    try:
        with tempfile.TemporaryDirectory() as d:
            pb.DUMPS, pb.UNSAVED = d, os.path.join(d, 'rescue')
            path = os.path.join(d, 'Zook (0a0b0c0d) - current.sky')

            def save(p, data):
                if p == path:
                    raise OSError(28, 'No space left on device')
                real_save(p, data)
            pb.save_dump = save
            server = Server(Portal(DESC.r_reply), DESC)
            closed, calls = [], []
            server.sound = SimpleNamespace(close=lambda: closed.append('sound'))
            n = server.portal.load(figure('0a0b0c0d'), path)
            server.portal.write(n, 5, bytes([7]) * 16)
            assert server.portal.slots[n].unsaved

            def broken():
                raise OSError('simulated')
            pb.usbip = lambda *a: calls.append(a) or (0, '')
            pb.shutdown(server, None, pb.re.search(r'port\D*(\d+)', 'port: 1'), SimpleNamespace(close=broken))
            assert calls == [('detach', '-p', '1')] and closed == ['sound']
            rescued = os.listdir(pb.UNSAVED)
            assert len(rescued) == 1 and open(os.path.join(pb.UNSAVED, rescued[0]), 'rb').read()[80:96] == bytes([7]) * 16
            assert server.portal.slots[n].unsaved is None
            assert any('could not save every figure' in line for _, line in pb.LOG_LINES)
    finally:
        pb.save_dump, pb.UNSAVED, pb.usbip = real_save, real_unsaved, real_usbip


def incomplete_kept():
    """After an incomplete restore, lifting and placing the figure again keeps its before-restore snapshot, until
    a restore of it completes."""
    with tempfile.TemporaryDirectory() as d:
        portal, m, new, path = restore_setup(d)
        m.real.fail = {6: 99}
        job = m.restore(path)
        m.poll()
        assert job.state == 'incomplete' and m.unfinished == {'86bc1263': job.snapshot}
        half = m.real.figures.pop(2)
        m.poll()  # lifted
        m.real.figures[2] = half
        m.poll()  # placed again: the restored game copy differs from the figure, so it is archived
        assert os.path.exists(job.snapshot) and len(pb.backups('86bc1263')['86bc1263']) == 2
        m.real.fail = {}
        retry = m.restore(path)
        m.poll()
        assert retry.state == 'completed' and not m.unfinished


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
    """Busy real portal while an emulator runs: its game booted before PortalSync. Say what to do."""
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


def unsaved_progress():
    """A dump that can't be written: the game's write is still acked, the slot is marked unsaved and retried with
    backoff, and its progress is never dropped (its own file, else a rescue copy, else it stays loaded)."""
    real_save, real_unsaved = pb.save_dump, pb.UNSAVED
    try:
        with tempfile.TemporaryDirectory() as d:
            pb.DUMPS, pb.UNSAVED = d, os.path.join(d, 'rescue')
            path = os.path.join(d, 'Zook (0a0b0c0d) - current.sky')
            broken = {path}

            def save(p, data):
                if p in broken:
                    raise OSError(28, 'No space left on device')
                real_save(p, data)
            pb.save_dump = save
            portal = Portal(DESC.r_reply)
            n = portal.load(figure('0a0b0c0d'), path)
            s = portal.slots[n]
            assert portal.write(n, 5, bytes([7]) * 16) == bytes([0x57, 0x10 | n, 5])  # acked as saved
            assert s.unsaved and s.attempt == 1 and s.data[80:96] == bytes([7]) * 16
            word, text = pb.unsaved_state(s)
            assert word == '⚠ Not saved to disk' and 'Zook (0a0b0c0d) - current.sky' in text and 'No space' in text
            assert portal.notices.get_nowait() == text and portal.notices.empty()  # one pop-up

            t = s.due  # 1 s after the failure, then 2, 4… at most 30
            portal.retry_saves(t - 0.1)
            assert s.attempt == 1
            portal.retry_saves(t)
            assert s.attempt == 2 and s.due == t + 2
            for _ in range(8):
                t = s.due
                portal.retry_saves(t)
            assert s.attempt == 10 and s.due == t + 30 and portal.notices.empty()  # capped; retries say nothing

            portal.write(n, 6, bytes([8]) * 16)
            broken.clear()
            portal.retry_saves(s.due)
            assert s.unsaved is None and s.attempt == 0 and pb.unsaved_state(s) is None
            assert open(path, 'rb').read() == bytes(s.data) and s.data[96:112] == bytes([8]) * 16

            broken.add(path)  # lifted while the disk still fails: a rescue copy
            portal.write(n, 6, bytes([9]) * 16)
            portal.notices.get_nowait()
            assert portal.remove(n) and s.path is None and s.unsaved is None
            rescued = os.listdir(pb.UNSAVED)
            assert len(rescued) == 1 and rescued[0].startswith('Zook (0a0b0c0d) - current 20')
            assert open(os.path.join(pb.UNSAVED, rescued[0]), 'rb').read()[96:112] == bytes([9]) * 16
            assert os.path.join(pb.UNSAVED, rescued[0]) in portal.notices.get_nowait()

            n = portal.load(figure('0a0b0c0d'), path)  # the rescue fails too: nothing let go
            s = portal.slots[n]
            portal.write(n, 6, bytes([10]) * 16)
            open(os.path.join(d, 'file'), 'w').close()
            pb.UNSAVED = os.path.join(d, 'file', 'rescue')
            assert not portal.remove(n) and s.path == path and s.status & 1 and s.unsaved
            assert s.data[96:112] == bytes([10]) * 16
            done = portal.rescue_unsaved()
            assert len(done) == 1 and done[0][0] == 'Zook' and isinstance(done[0][1], OSError) and s.unsaved

            pb.UNSAVED = os.path.join(d, 'rescue')
            done = portal.rescue_unsaved()
            assert done[0][0] == 'Zook' and open(done[0][1], 'rb').read() == bytes(s.data) and s.unsaved is None
            assert portal.rescue_unsaved() == [] and len(os.listdir(pb.UNSAVED)) == 2
    finally:
        pb.save_dump, pb.UNSAVED = real_save, real_unsaved


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
    """inject() gets portal_hide.dll into another process, the way PortalSync does it to Cemu."""
    if not os.environ.get('PORTALSYNC_TEST_INJECT'):  # injects a DLL into another process: opt-in only
        return print('injection not checked (set PORTALSYNC_TEST_INJECT=1 to check it)')
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
    pb.seal(d, a, 0x110)  # checksum types 1-3, and 6 for the second area
    return pb.crypt(bytes(d), encrypt=True)


def reading(figure_info):
    """Level, gold and nickname from a figure's encrypted save; a damaged figure gets a repair offered, and
    damaged saves are never offered or written."""
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
        assert m.repairs.get_nowait() == '1a2b3c4d' and m.offers.empty() and m.notices.empty()  # a repair, not a backup

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

    # repair keeps the progress and makes the checksums valid; edits keep the previous save as fallback
    good = saved_figure('1a2b3c4d', (33000, 10203), 1234, 'raf')
    broken = bytearray(pb.crypt(good))
    broken[0xD0] ^= 1  # block 13: covered by checksum type 3
    broken = pb.crypt(bytes(broken), encrypt=True)
    assert pb.figure_info(broken).damaged
    fixed = pb.figure_info(pb.repaired(broken))
    assert not fixed.damaged and (fixed.level, fixed.gold, fixed.nickname) == (11, 1234, 'raf')
    for level in (1, 10, 11, 15, 20):
        e = pb.figure_info(pb.edited(good, gold=65000, nickname='Zooky', level=level))
        assert (e.level, e.gold, e.nickname, e.damaged, e.old_damaged) == (level, 65000, 'Zooky', False, False)
    e = pb.crypt(pb.edited(good, gold=5))
    assert e[0x240 + 9] == 2 and struct.unpack_from('<H', e, 0x243)[0] == 5  # new save in the older area
    assert struct.unpack_from('<H', e, 0x83)[0] == 1234  # the previous save is still there
    assert pb.figure_info(pb.edited(good, gold=5)).level == 11  # untouched fields stay

    # volume: 16-bit samples scaled and clipped
    sound = pb.PortalSound(speaker=lambda block: played.append(block) or True)
    played = []
    sound.volume = 0.5
    sound.feed(struct.pack('<4h', 1000, -1000, 32767, -32768) + bytes(56))
    assert struct.unpack_from('<4h', played[0]) == (500, -500, 16383, -16384)
    sound.volume = 2
    sound.feed(struct.pack('<2h', 30000, -30000) + bytes(60))
    assert struct.unpack_from('<2h', played[1]) == (32767, -32768)

    # games: found from each emulator's own settings
    with tempfile.TemporaryDirectory() as d:
        cemu, rpcs3, dolphin = (os.path.join(d, x) for x in ('cemu', 'rpcs3', 'dolphin'))
        game = os.path.join(d, 'roms', 'Skylanders Trap Team [Game] [0005000010181f00]')
        os.makedirs(os.path.join(game, 'code'))
        open(os.path.join(game, 'code', 'tfbGame_cafe.rpx'), 'w').close()
        os.makedirs(os.path.join(d, 'roms', 'Skylanders Trap Team [Update] [0005000e10181f00]', 'code'))
        os.makedirs(cemu)
        open(os.path.join(cemu, 'settings.xml'), 'w').write(f'<GamePaths><Entry>{os.path.join(d, "roms")}</Entry></GamePaths>')
        os.makedirs(os.path.join(rpcs3, 'config'))
        open(os.path.join(rpcs3, 'config', 'games.yml'), 'w').write('BLES01689: F:/ps3roms/Skylanders - Giants (Europe) (En,Fr)/\n')
        os.makedirs(os.path.join(dolphin, 'User', 'Config'))
        open(os.path.join(dolphin, 'portable.txt'), 'w').close()
        wii = os.path.join(d, 'wii')
        os.makedirs(wii)
        open(os.path.join(wii, "Skylanders - Spyro's Adventure (Rev 3).rvz"), 'w').close()
        open(os.path.join(dolphin, 'User', 'Config', 'Dolphin.ini'), 'w').write(f'[General]\nISOPath0 = {wii}\nISOPaths = 1\n')
        games = pb.find_games({'Cemu': os.path.join(cemu, 'Cemu.exe'), 'RPCS3': os.path.join(rpcs3, 'rpcs3.exe'),
                               'Dolphin': os.path.join(dolphin, 'Dolphin.exe')})
        assert [(t, e) for t, e, p in games] == [('Skylanders - Giants', 'RPCS3'), ("Skylanders - Spyro's Adventure", 'Dolphin'),
                                                 ('Skylanders Trap Team', 'Cemu')], games
        assert games[2][2].endswith('tfbGame_cafe.rpx')

    assert pb.newer('v1.3.0', '1.2.0') and pb.newer('v1.10.0', '1.9.9') and not pb.newer('v1.2.0', '1.2.0')
    assert not pb.newer('v1.1.1', '1.2.0') and not pb.newer('nightly', '1.2.0')


def stale_reads():
    """read_figure gets every block right on a portal that answers with the previously queried block's data
    now and then, in runs (also stuck for several queries), as the user's portal does."""
    import random
    rng = random.Random(7)

    class Stale(pb.RealPortal):
        def __init__(self, data):
            self.data, self.loaded, self.stuck = data, 0, 0

        def read_block_once(self, slot, block):
            if self.stuck == 0 and rng.random() < 0.1:
                self.stuck = rng.randint(1, 5)
            if self.stuck:
                self.stuck -= 1
            else:
                self.loaded = block
            return self.data[self.loaded * 16:self.loaded * 16 + 16]

    for kind in range(40):
        data = bytearray(rng.randbytes(1024) if kind % 2 else bytes(1024))  # random, or mostly empty
        data[0:5] = bytes.fromhex('1a2b3c4d') + bytes([0x1a ^ 0x2b ^ 0x3c ^ 0x4d])
        for b in rng.sample(range(2, 64), 20):  # runs of equal blocks (block 1, the header, never equals block 0)
            data[b * 16:b * 16 + 16] = data[(b - 1) * 16:b * 16]
        assert Stale(bytes(data)).read_figure(0) == bytes(data)


def settings_logic():
    """What the Settings page changes: backups per figure, sound mode, lights, fullscreen start, figures folder."""
    with tempfile.TemporaryDirectory() as d:
        settings, pb.SETTINGS = pb.SETTINGS, os.path.join(d, 'settings.json')
        try:  # saving one choice keeps the others
            pb.App.remember(pb.App, volume=40)
            pb.App.remember(pb.App, dark=True)
            pb.App.remember(pb.App, volume=60)
            assert pb.App.settings() == {'volume': 60, 'dark': True}
        finally:
            pb.SETTINGS = settings
        pb.DUMPS = d
        trash = fake_bin(d)
        image = figure('86bc1263')
        names = ['Zook (86bc1263) - original.sky'] + [f'Zook (86bc1263) - saved 2026-10-1{k} 10.00.00.sky' for k in range(4)]
        for name in names:
            pb.save_dump(os.path.join(d, name), image)
        pb.BACKUPS_KEPT = 3
        try:
            pb.keep_one_backup('86bc1263')
        finally:
            pb.BACKUPS_KEPT = 1
        assert sorted(os.listdir(trash)) == sorted(names[:2])  # the three newest stay

        src, dst = os.path.join(d, 'old'), os.path.join(d, 'new')
        os.makedirs(src)
        os.makedirs(dst)
        for name in ('A (11111111) - current.sky', 'B (22222222) - current.sky', 'notes.txt'):
            open(os.path.join(src, name), 'w').write(name)
        open(os.path.join(dst, 'A (11111111) - current.sky'), 'w').write('already there')
        assert pb.move_dumps(src, dst) == 2
        assert sorted(os.listdir(dst)) == ['A (11111111) - current-2.sky', 'A (11111111) - current.sky', 'B (22222222) - current.sky']
        assert os.listdir(src) == ['notes.txt'] and open(os.path.join(dst, 'A (11111111) - current.sky')).read() == 'already there'

    played = []
    sound = pb.PortalSound(speaker=lambda block: played.append(block) or True)
    sound.pc = lambda block: played.append(b'pc')
    sound.feed(bytes(64))
    sound.mode = 'pc'
    sound.feed(bytes(64))
    sound.mode = 'off'
    sound.feed(bytes(64))
    assert played == [bytes(64), b'pc']

    server = Server(Portal(DESC.r_reply), DESC)
    m = pb.Mirror(server, vhci_bus=3)
    m.real = FakeReal({})
    m.on_command(b'C\xff\x00\x00')
    m.lights = False
    m.on_command(b'C\x00\xff\x00')
    m.on_command(b'M\x01')
    assert [j[1][:2] for j in list(m.jobs.queue)] == [b'C\xff', b'M\x01']  # lights off: colour dropped, speaker kept

    calls = []
    real_popen, pb.subprocess.Popen = pb.subprocess.Popen, lambda args, cwd: calls.append(args) or SimpleNamespace(poll=lambda: 0, pid=0)
    try:
        for emulator in ('Cemu', 'Dolphin', 'RPCS3', 'shadPS4'):
            pb.start_game(r'C:\emu\x.exe', emulator, r'F:\game', fullscreen=True)
        pb.start_game(r'C:\emu\x.exe', 'Cemu', r'F:\game')
    finally:
        pb.subprocess.Popen = real_popen
    assert [c[1:] for c in calls] == [['-f', '-g', r'F:\game'], ['-C', 'Dolphin.Display.Fullscreen=True', '-e', r'F:\game'],
                                      ['--no-gui', '--fullscreen', r'F:\game'], ['-f', 'true', '-g', r'F:\game'],
                                      ['-g', r'F:\game']]
    with tempfile.TemporaryDirectory() as d:  # a PS4 game's title, from a param.sfo laid out as the PS4 does
        keys, data = b'TITLE\0TITLE_ID\0\0\0', b'Skylanders Imaginators\0\0CUSA05158\0\0\0'
        sfo = (struct.pack('<IIIII', 0x46535000, 0x101, 20 + 32, 20 + 32 + len(keys), 2)
               + struct.pack('<HHIII', 0, 0x204, 23, 24, 0) + struct.pack('<HHIII', 6, 0x204, 10, 12, 24) + keys + data)
        open(os.path.join(d, 'param.sfo'), 'wb').write(sfo)
        assert pb.sfo_title(os.path.join(d, 'param.sfo')) == 'Skylanders Imaginators'
        assert pb.sfo_title(os.path.join(d, 'missing.sfo')) is None


def extras():
    """1.5.0: Swapper halves, Imaginators names, daily zips, progress history, the update installer."""
    import io, zipfile
    assert pb.figure_key(figure('00000001', 2015)[:0x1C] + (8192).to_bytes(2, 'little')) == '2015:8192'
    assert pb.partner('2015:8192') == '1015:8192' and pb.partner('1015:8192') == '2015:8192' and pb.partner('25:0') is None
    assert pb.NAMES['613:20480'] == 'Golden Queen' and pb.NAMES['682:20999'] == 'Air Creation Crystal (Lantern)'
    assert pb.DETAILS['689:22038'] == ['Imaginators', 'Light'] and '613:0' not in pb.DETAILS  # Cemu's variant: name only
    crystal = bytearray(saved_figure('1dc2de67'))  # a played Creation Crystal: never read as a (damaged) Skylander
    crystal[0x10:0x12], crystal[0x1C:0x1E] = (682).to_bytes(2, 'little'), (20994).to_bytes(2, 'little')
    info = pb.figure_info(bytes(crystal))
    assert info.kind == 'other' and not info.damaged and not info.nickname
    with tempfile.TemporaryDirectory() as d:
        pb.DUMPS = d
        trash = fake_bin(d)
        queen = bytearray(figure('3aae28ee', 613))
        queen[0x1C:0x1E] = (20480).to_bytes(2, 'little')
        open(os.path.join(d, 'Golden Queen (variant 0x5000) (3aae28ee) - current.sky'), 'wb').write(queen)
        pb.rename_old_dumps()
        assert sorted(os.listdir(d)) == ['Golden Queen (3aae28ee) - current.sky', 'bin']

        os.makedirs(os.path.join(d, 'daily backups'))
        for day in ('2026-10-01', '2026-10-02', '2026-10-03'):  # older daily zips beyond `keep` are recycled
            open(os.path.join(d, 'daily backups', f'Skylanders {day}.zip'), 'wb').close()
        made = pb.daily_zip(keep=2)
        assert made and zipfile.ZipFile(made).namelist() == ['Golden Queen (3aae28ee) - current.sky']
        assert sorted(os.listdir(os.path.join(d, 'daily backups'))) == ['Skylanders 2026-10-03.zip', os.path.basename(made)]
        assert sorted(os.listdir(trash)) == ['Skylanders 2026-10-01.zip', 'Skylanders 2026-10-02.zip']
        assert pb.daily_zip(keep=2) is None  # once a day

        pb.PROGRESS_LAST.clear()
        zook = saved_figure('86bc1263', (33000, 10203), gold=100)
        for gold, when in ((100, 1.8e9), (100, 1.8e9 + 60), (250, 1.8e9 + 120)):  # the same level and gold twice: one line
            z = saved_figure('86bc1263', (33000, 10203), gold=gold)
            pb.record_progress(z, pb.figure_info(z), when)
        open(os.path.join(d, 'Zook (86bc1263) - saved 2026-10-01 10.00.00.sky'), 'wb').write(
            saved_figure('86bc1263', (24800, 0), gold=50))
        assert [r[1:] for r in pb.progress_history('86bc1263')] == [(9, 50), (11, 100), (11, 250)]
        assert pb.progress_history('86bc1263')[0][0] == '2026-10-01 10:00'
        pb.record_progress(zook, pb.figure_info(zook))  # back to 100 gold: a new line
        assert len(pb.progress_rows()) == 3
        older, newer = (pb.figure_info(saved_figure('86bc1263', xp, gold=g)) for xp, g in (((24800, 0), 50), ((33000, 10203), 100)))
        assert pb.compare_progress(older, newer) == 'less progress: level 9 (the figure: 11)'
        assert pb.compare_progress(newer, older) == 'more progress: level 11 (the figure: 9)'
        richer = pb.figure_info(saved_figure('86bc1263', (33000, 10203), gold=400))
        assert pb.compare_progress(richer, newer) == 'same level, +300 gold'
        assert pb.compare_progress(newer, newer, same=True) == 'same as the figure'

        import urllib.request  # the update installer: see updating()
        pics = io.BytesIO()  # Get pictures: only '<id>_<variant>.png' names, and never outside the folder
        with zipfile.ZipFile(pics, 'w') as zf:
            for name in ('thumbnails/25_0.png', '../../evil_1.png', 'thumbnails/ATTRIBUTION.md', 'x.exe', '613_20480.png'):
                zf.writestr(name, b'png')
        saved, urllib.request.urlopen = urllib.request.urlopen, lambda req, timeout: io.BytesIO(pics.getvalue())
        try:
            shelf = os.path.join(d, 'pictures')
            assert pb.download_pictures('https://example.com/t.zip', shelf) == 2
            assert sorted(os.listdir(shelf)) == ['25_0.png', '613_20480.png'] and not os.path.exists(os.path.join(d, 'evil_1.png'))
        finally:
            urllib.request.urlopen = saved


def updating():
    """install_update: only the allowlisted files, checked and size-capped before writing, all or none (a failure
    mid-swap puts the old files back); recover_update undoes a swap cut off by a crash. User data is never touched."""
    import io, json, urllib.request, zipfile
    url = pb.DOWNLOADS + 'v9.0.0/PortalSync-v9.0.0.zip'
    release = {n: b'MZnew ' + n.encode() for n in pb.REQUIRED_FILES} | {'README.md': b'new readme'}

    def zipped(files, sizes=None):
        z = io.BytesIO()
        with zipfile.ZipFile(z, 'w') as zf:
            for name, body in files.items():
                zf.writestr(name, body)
        if sizes:  # lie about unpacked sizes in the central directory, the way a hostile zip could
            zf = zipfile.ZipFile(z, 'a')
            for info in zf.infolist():
                info.file_size = sizes.get(info.filename, info.file_size)
            zf._didModify = True  # rewrite the central directory on close
            zf.close()
        return z.getvalue()

    def tree(folder):
        out = {}
        for root, _, names in os.walk(folder):
            for n in names:
                out[os.path.relpath(os.path.join(root, n), folder)] = open(os.path.join(root, n), 'rb').read()
        return out

    user = {'settings.json': b'{"mine": 1}', os.path.join('dumps', 'a.sky'): b'sky', 'portalsync.log': b'log',
            os.path.join('thumbnails', '25_0.png'): b'png'}
    old = {n: b'old ' + n.encode() for n in pb.REQUIRED_FILES}
    saved_open, saved_replace, saved_recycle = urllib.request.urlopen, os.replace, pb.recycle
    with tempfile.TemporaryDirectory() as d:
        def fresh():
            app = tempfile.mkdtemp(dir=d)
            for name, body in (old | user).items():
                os.makedirs(os.path.dirname(os.path.join(app, name)), exist_ok=True)
                open(os.path.join(app, name), 'wb').write(body)
            return app

        def refused(data, app, why):
            urllib.request.urlopen = lambda req, timeout: io.BytesIO(data)
            before = tree(app)
            try:
                pb.install_update(url, app)
                raise AssertionError(f'installed despite {why}')
            except ValueError as e:
                assert why in str(e), e
            assert tree(app) == before, why  # nothing written

        fake_bin(d)
        try:
            app = fresh()
            try:
                pb.install_update('https://example.com/PortalSync-v9.zip', app)
                raise AssertionError('accepted a zip from elsewhere')
            except ValueError:
                pass
            refused(zipped({n: b for n, b in release.items() if n != 'figure_names.json'}), app, 'figure_names.json')
            refused(zipped(release | {'PortalSync.exe': b'not a program'}), app, 'not a Windows program')
            refused(zipped(release | {'PortalSync/PortalSync.exe': b'MZ2'}), app, 'more than one PortalSync.exe')
            refused(b'not a zip', app, 'not a valid zip')
            refused(zipped(release, {'figure_names.json': pb.UPDATE_FILE_MAX + 1}), app, 'too large (figure_names.json)')
            refused(zipped(release, {'README.md': 99}), app, 'not the size it claims')  # file_size lies high
            refused(zipped(release, {'README.md': 2}), app, 'not a valid zip')  # lies low: the CRC check catches it
            cap, pb.UPDATE_MAX = pb.UPDATE_MAX, 100  # a download over the cap
            try:
                refused(zipped(release), app, 'larger than')
            finally:
                pb.UPDATE_MAX = cap

            # success: every file replaced, the old ones as .old, user data and evil names ignored
            evil = {'../x': b'x', 'settings.json': b'{}', 'dumps/a.sky': b'evil', 'PortalSync/LICENSE.txt': b'?'}
            urllib.request.urlopen = lambda req, timeout: io.BytesIO(zipped(
                {f'PortalSync/{n}': b for n, b in release.items()} | evil))
            assert pb.install_update(url, app) == sorted(release)
            want = user | release | {n + '.old': b for n, b in old.items()}
            assert tree(app) == want, sorted(tree(app))
            assert not os.path.exists(os.path.join(d, 'x'))
            assert pb.install_update(url, app) == sorted(release)  # again: the earlier .old files go to the bin first
            assert tree(app) == want | {'README.md.old': b'new readme'} | {n + '.old': b for n, b in release.items()}

            # a failure on the 3rd file mid-swap: all old files back, no journal, no .new
            app = fresh()
            urllib.request.urlopen = lambda req, timeout: io.BytesIO(zipped(release))

            def flaky(src, dst):
                if src.endswith('portal_descriptors.json.new'):
                    raise PermissionError('in use')
                return saved_replace(src, dst)
            os.replace = flaky
            try:
                pb.install_update(url, app)
                raise AssertionError('the swap failure was hidden')
            except OSError as e:
                assert 'in use' in str(e) and 'the old version is kept' in str(e), e
            finally:
                os.replace = saved_replace
            assert tree(app) == old | user, sorted(tree(app))

            # a crash after 2 files were swapped and the 3rd half-way: recover_update puts the old ones back
            app = fresh()
            assert pb.recover_update(app) is None  # no journal: nothing to do
            exe, dll, desc = (os.path.join(app, n) for n in pb.REQUIRED_FILES[:3])
            for path in (exe, dll):
                os.replace(path, path + '.old')
                open(path, 'wb').write(b'new')
            os.replace(desc, desc + '.old')  # cut off before its .new took its place
            for n in pb.REQUIRED_FILES[2:] + ('README.md',):
                open(os.path.join(app, n + '.new'), 'wb').write(b'new')
            plan = {n: 'replace' for n in pb.REQUIRED_FILES} | {'README.md': 'add'}
            json.dump({'files': plan}, open(os.path.join(app, pb.UPDATE_JOURNAL), 'w'))
            assert pb.recover_update(app) == sorted(pb.REQUIRED_FILES[:3])
            assert tree(app) == old | user, sorted(tree(app))
        finally:
            urllib.request.urlopen, os.replace, pb.recycle = saved_open, saved_replace, saved_recycle


def setup_checking():
    # The setup checker reads the portal's driver from the devices Windows lists (the virtual portal is HID too).
    real = pb.portal_pnp
    try:
        cases = {'driver done': [('libusbk devices', 'USB\\VID_1430&PID_0150\\6&1')],
                 'no driver yet': [('HIDClass', 'USB\\VID_1430&PID_0150\\6&1')],
                 'only the virtual one': [('HIDClass', 'USB\\VID_1430&PID_0150\\2&9')], 'nothing': []}
        got = {}
        for name, devices in cases.items():
            pb.portal_pnp = lambda devices=devices: devices
            got[name] = [ok for _, ok, _ in pb.setup_checks(True, {})][1:3]  # plugged in, driver
        assert got == {'driver done': [True, True], 'no driver yet': [False, None],
                       'only the virtual one': [False, None], 'nothing': [False, None]}, got
        pb.portal_pnp = lambda: [('HIDClass', 'x')]
        assert [ok for _, ok, _ in pb.setup_checks(False, {})][1:3] == [True, False]  # before attaching: needs Zadig
    finally:
        pb.portal_pnp = real


def main():
    reading(pb.figure_info)
    setup_checking()
    extras()
    updating()
    pb.figure_info = lambda data: SimpleNamespace(kind='other', level=None, gold=None, nickname='', damaged=False,
                                                  old_damaged=False)  # the other tests' saves are made-up bytes
    names()
    one_backup()
    files()
    mirror()
    restoring()
    restore_retries()
    restore_incomplete()
    restore_swapped()
    restore_order()
    restore_unsaved()
    restore_cancel()
    cancelled_wait()
    stop_running_restore()
    stop_timeout()
    stop_refuses()
    shutting_down()
    incomplete_kept()
    sound()
    speaker()
    lifted_mid_write()
    stale_reads()
    settings_logic()
    creating()
    game_first()
    contention()
    file_trouble()
    unsaved_progress()
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
