"""Hardware-free regressions: python -m unittest -v test_save_safety."""
import builtins
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import portalsync as pb
from test_portalsync import DESC, FakeReal, saved_figure

REAL_RECYCLE = pb.recycle  # setUp swaps it for a fake


class SaveSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 0.0
        self.recycled = []
        self.enterContext(patch.object(pb, 'DUMPS', self.temp.name))
        self.enterContext(patch.object(pb, 'UNFINISHED', os.path.join(self.temp.name, 'unfinished restores.json')))
        self.enterContext(patch.object(pb, 'log'))
        self.enterContext(patch.object(pb, 'recycle', side_effect=self.recycle))
        self.enterContext(patch.object(pb.time, 'monotonic', side_effect=lambda: self.now))
        self.uid = '1a2b3c4d'
        self.image = saved_figure(self.uid)

    def recycle(self, path):
        self.recycled.append(os.path.basename(path))
        os.rename(path, path + '.recycled')
        return True

    def backups(self, newest):
        old = pb.dump_path(self.image, 'original')
        new = pb.dump_path(self.image, 'saved 2026-10-17 12.00.00')
        pb.save_dump(old, self.image)
        pb.save_dump(new, newest)
        return old, new

    def mirror(self):
        server = pb.Server(pb.Portal(DESC.r_reply), DESC)
        m = pb.Mirror(server, 3)
        m.real = FakeReal({0: bytearray(self.image)})
        m.poll()
        m.set_write_back(True)
        return server.portal, m

    def write(self, portal, value, block=5):
        portal.command(bytes([ord('W'), 0x10, block]) + bytes([value]) * 16)

    @staticmethod
    def game_polls(portal):
        """The game reads status reports until it has seen every figure lifted and placed."""
        while any(s.queued for s in portal.slots):
            portal.status()

    def test_empty_backup_cannot_replace_valid_original(self):
        old, new = self.backups(b'')
        pb.keep_one_backup(self.uid)
        self.assertTrue(os.path.exists(old))
        self.assertEqual(open_bytes(old), self.image)
        self.assertNotIn(os.path.basename(old), self.recycled)
        self.assertTrue(pb.damaged(new))  # missing or invalid, never known-good

    def test_invalid_sizes_and_explicit_keep_preserve_valid_backup(self):
        for size in (1, 1023, 1025):
            with self.subTest(size=size):
                old, new = self.backups(self.image[:size] if size < 1024 else self.image + b'x')
                pb.keep_one_backup(self.uid, keep=new)
                self.assertEqual(open_bytes(old), self.image)
                # Remove only the test's previously recycled placeholder before the next case.
                if os.path.exists(new + '.recycled'):
                    os.remove(new + '.recycled')

    def test_unreadable_backup_cannot_replace_valid_original(self):
        old, new = self.backups(self.image)
        real_open = builtins.open
        def guarded_open(path, *args, **kwargs):
            if path == new:
                raise PermissionError('simulated unreadable backup')
            return real_open(path, *args, **kwargs)
        with patch('builtins.open', side_effect=guarded_open):
            pb.keep_one_backup(self.uid)
        self.assertEqual(open_bytes(old), self.image)

    def test_no_valid_backup_preserves_all_recovery_candidates(self):
        old, new = self.backups(b'')
        pb.save_dump(old, b'partial')
        pb.keep_one_backup(self.uid)
        self.assertEqual(self.recycled, [])
        self.assertTrue(os.path.exists(old) and os.path.exists(new))

    def test_transient_failure_retries_without_lifting_figure(self):
        portal, m = self.mirror()
        original = m.real.write_block
        with patch.object(m.real, 'write_block') as write:
            # The second attempt performs the real fake-device write.
            def attempt(i, block, data):
                if write.call_count == 1:
                    raise TimeoutError('transient')
                original(i, block, data)
            write.side_effect = attempt
            self.write(portal, 7)
            m.poll()
            m.poll()
            self.assertEqual(write.call_count, 1)  # backoff, not a tight loop
            self.now += 1
            m.poll()
            self.assertEqual(write.call_count, 2)
        self.assertEqual(m.real.figures[0][80:96], bytes([7]) * 16)
        self.assertEqual(m.sync_status(), '')
        self.assertEqual(bytes(m.known[0]), bytes(portal.slots[0].data))

    def test_readback_mismatch_is_retried(self):
        portal, m = self.mirror()
        original = m.real.write_block
        with patch.object(m.real, 'write_block') as write:
            write.side_effect = lambda *args: original(*args) if write.call_count > 1 else None
            self.write(portal, 8)
            m.poll()
            self.now += 1
            m.poll()
            self.assertEqual(write.call_count, 2)
        self.assertEqual(m.real.figures[0][80:96], bytes([8]) * 16)

    def test_permanent_failure_is_bounded_and_visible(self):
        portal, m = self.mirror()
        with patch.object(m.real, 'write_block', side_effect=TimeoutError('persistent')) as write:
            self.write(portal, 7)
            for _ in range(10):
                m.poll()
                self.now += 10
            self.assertEqual(write.call_count, m.WRITE_ATTEMPTS)
        self.assertIn('FAILED', m.sync_status())
        self.assertEqual(m.notices.qsize(), 1)
        self.assertEqual(open_bytes(portal.slots[0].path)[80:96], bytes([7]) * 16)
        m.set_write_back(False)
        m.set_write_back(True)
        m.poll()
        self.assertEqual(m.real.figures[0][80:96], bytes([7]) * 16)
        self.assertEqual(m.sync_status(), '')

    def test_new_save_supersedes_failed_write(self):
        portal, m = self.mirror()
        with patch.object(m.real, 'write_block', side_effect=TimeoutError('once')):
            self.write(portal, 7)
            m.poll()
        with patch.object(m.real, 'write_block', wraps=m.real.write_block) as write:
            self.write(portal, 9)
            self.now += 10
            m.poll()
            m.poll()
            self.assertEqual([call.args[2] for call in write.call_args_list], [bytes([9]) * 16])
        self.assertEqual(m.real.figures[0][80:96], bytes([9]) * 16)

    def test_disabling_cancels_queued_automatic_writes(self):
        portal, m = self.mirror()
        self.write(portal, 7)
        m.set_write_back(False)
        with patch.object(m.real, 'write_block', wraps=m.real.write_block) as write:
            m.poll()
            write.assert_not_called()
        self.assertEqual(bytes(m.real.figures[0]), self.image)

    def test_disabling_cancels_retries(self):
        portal, m = self.mirror()
        with patch.object(m.real, 'write_block', side_effect=TimeoutError('once')):
            self.write(portal, 7)
            m.poll()
        m.set_write_back(False)
        self.now += 10
        with patch.object(m.real, 'write_block', wraps=m.real.write_block) as write:
            m.poll()
            write.assert_not_called()
        self.assertEqual(m.sync_status(), '')

    def test_off_on_does_not_resurrect_old_queue(self):
        portal, m = self.mirror()
        self.write(portal, 7)
        m.set_write_back(False)
        self.write(portal, 9)
        m.set_write_back(True)
        with patch.object(m.real, 'write_block', wraps=m.real.write_block) as write:
            m.poll()
        self.assertEqual([call.args[2] for call in write.call_args_list], [bytes([9]) * 16])

    def test_explicit_restore_works_with_write_back_disabled(self):
        portal, m = self.mirror()
        self.write(portal, 7)
        m.set_write_back(False)
        restored = bytearray(self.image)
        restored[80:96] = bytes([9]) * 16
        path = os.path.join(pb.DUMPS, 'restore.sky')
        pb.save_dump(path, restored)
        m.restore(path)
        m.poll()
        self.assertEqual(m.real.figures[0][80:96], bytes([9]) * 16)

    def test_restore_supersedes_automatic_retry(self):
        portal, m = self.mirror()
        with patch.object(m.real, 'write_block', side_effect=TimeoutError('once')):
            self.write(portal, 7)
            m.poll()
        path = os.path.join(pb.DUMPS, 'restore.sky')
        pb.save_dump(path, self.image)
        m.restore(path)
        self.now += 10
        m.poll()
        m.poll()
        self.assertEqual(bytes(m.real.figures[0]), self.image)
        self.assertEqual(m.sync_status(), '')

    def test_replaced_figure_never_receives_old_retry(self):
        portal, m = self.mirror()
        with patch.object(m.real, 'write_block', side_effect=TimeoutError('once')):
            self.write(portal, 7)
            m.poll()
        other = saved_figure('aabbccdd')
        m.real.figures[0] = bytearray(other)
        self.now += 10
        with patch.object(m.real, 'write_block', wraps=m.real.write_block) as write:
            m.poll()
            write.assert_not_called()
        self.assertEqual(bytes(m.real.figures[0]), other)

    def test_invalid_blocks_never_reach_physical_write(self):
        portal, m = self.mirror()
        with patch.object(m.real, 'write_block', wraps=m.real.write_block) as write:
            for block in (0, 3, 7, 63, 64, 65, 254, 255):
                self.write(portal, 7, block)
            m.poll()
            write.assert_not_called()

    def test_short_write_and_direct_boundary_validation(self):
        portal, m = self.mirror()
        with patch.object(m.real, 'write_block', wraps=m.real.write_block) as write:
            portal.command(b'W\x10\x05\x07')
            m.queue_block(0, 5, b'short')
            m.poll()
            for block, data in ((-1, bytes(16)), (64, bytes(16)), (3, bytes(16)), (0, bytes(16)), (5, b'short')):
                self.assertFalse(m.write_verified(0, block, data, self.uid))
            write.assert_not_called()
        self.assertEqual(bytes(portal.slots[0].data), self.image)

    def holding(self, block, value):
        """Every file in the temp folder (dumps, recycled ones too) whose block holds value."""
        return [n for n in os.listdir(self.temp.name) if n.endswith(('.sky', '.recycled'))
                and open_bytes(os.path.join(self.temp.name, n))[block * 16:block * 16 + 16] == bytes([value]) * 16]

    def test_restore_after_incomplete_one_snapshots_new_progress(self):
        portal, m = self.mirror()
        m.real.fail = {6: 99}
        older = bytearray(self.image)
        older[80:96], older[96:112] = bytes([1]) * 16, bytes([2]) * 16
        pb.save_dump(os.path.join(pb.DUMPS, 'older.sky'), older)
        first = m.restore(os.path.join(pb.DUMPS, 'older.sky'))
        m.poll()
        m.real.fail = {}
        self.assertEqual(first.state, 'incomplete')
        self.game_polls(portal)
        self.write(portal, 0x77, block=9)  # new progress, written back to the figure
        m.poll()
        self.assertEqual(m.real.figures[0][144:160], bytes([0x77]) * 16)
        reset = m.reset(self.uid)
        m.poll()
        self.assertEqual(reset.state, 'completed')
        self.assertNotEqual(reset.snapshot, first.snapshot)
        self.assertTrue(self.holding(9, 0x77))  # the progress before the reset is a backup (or in the bin)

    def test_short_current_copy_and_unexpected_errors_keep_mirror_alive(self):
        server = pb.Server(pb.Portal(DESC.r_reply), DESC)
        m = pb.Mirror(server, 3)
        m.real = FakeReal({0: bytearray(self.image)})
        pb.save_dump(pb.dump_path(self.image, 'original'), self.image)
        pb.save_dump(pb.dump_path(self.image, 'current'), b'')  # 0-byte file after a power loss
        m.poll()
        self.assertEqual(open_bytes(server.portal.slots[0].path), self.image)
        polls = []
        def poll():
            polls.append(1)
            if len(polls) == 1:
                raise IndexError('short report')
            m.stopping = True
        with patch.object(m, 'poll', side_effect=poll), patch.object(pb, 'portal_devices', return_value=[]), \
                patch.object(pb.time, 'sleep'):
            m.run()  # returns instead of dying on the IndexError
        self.assertEqual(len(polls), 2)

    def test_restore_onto_unloaded_figure_keeps_game_progress(self):
        portal, m = self.mirror()
        m.set_write_back(False)
        self.write(portal, 9)  # progress only in current.sky
        with m.server.cv:
            portal.remove(0)  # right-click Unload
        other = bytearray(self.image)
        other[96:112] = bytes([7]) * 16
        path = os.path.join(pb.DUMPS, 'other.sky')
        pb.save_dump(path, other)
        m.restore(path)
        m.poll()
        self.assertTrue([n for n in self.holding(5, 9) if ' - saved ' in n])

    def test_kept_figure_played_elsewhere_is_not_overwritten(self):
        portal, m = self.mirror()
        real = m.real
        with patch.object(pb, 'portal_devices', return_value=[]):
            m.lost(OSError('unplugged'))  # the figure stays loaded (kept)
        real.figures[0][96:112] = bytes([0x55]) * 16  # played on a console meanwhile
        m.real, m.fresh = real, True
        m.poll()
        m.poll()
        self.assertEqual(real.figures[0][96:112], bytes([0x55]) * 16)
        self.assertTrue([n for n in self.holding(6, 0) if ' - saved ' in n])  # the game copy is a backup...
        self.assertEqual(m.offers.qsize(), 1)  # ... offered for restore

    def test_unfinished_restore_snapshot_survives_restart(self):
        portal, m = self.mirror()
        m.real.fail = {5: 99}
        older = bytearray(self.image)
        older[80:96], older[96:112] = bytes([1]) * 16, bytes([2]) * 16
        pb.save_dump(os.path.join(pb.DUMPS, 'older.sky'), older)
        job = m.restore(os.path.join(pb.DUMPS, 'older.sky'))
        self.assertTrue(m.restore_running())  # App.close asks first
        m.poll()
        self.assertEqual(job.state, 'incomplete')
        self.assertFalse(m.restore_running())
        figure = m.real.figures[0]
        # restart: main() prunes all but the unfinished one, then the figure is placed again
        for uid in pb.backups():
            if uid not in pb.unfinished_restores():
                pb.keep_one_backup(uid)
        m2 = pb.Mirror(pb.Server(pb.Portal(DESC.r_reply), DESC), 3)
        m2.real = FakeReal({0: figure})
        m2.poll()
        self.assertTrue(os.path.exists(job.snapshot))
        self.assertEqual(open_bytes(job.snapshot), self.image)

    def test_recycle_never_deletes_on_drive_without_recycle_bin(self):
        path = os.path.join(pb.DUMPS, 'old.zip')
        pb.save_dump(path, b'zip')
        with patch.object(pb, 'has_recycle_bin', return_value=False), \
                patch.object(pb.ctypes.windll.shell32, 'SHFileOperationW') as op:
            self.assertFalse(REAL_RECYCLE(path))
        op.assert_not_called()
        self.assertTrue(os.path.exists(path))

    def test_unreadable_settings_are_never_overwritten(self):
        settings = os.path.join(self.temp.name, 'settings.json')
        self.enterContext(patch.object(pb, 'SETTINGS', settings))
        app = type('A', (), {'settings': staticmethod(pb.App.settings)})()
        with open(settings, 'w') as f:
            f.write('{"teams": {"Air": []}, "write_back": true,}')  # trailing comma
        pb.App.remember(app, volume=80)
        broken = [n for n in os.listdir(self.temp.name) if n.startswith('settings.json.broken-')]
        self.assertEqual(len(broken), 1)
        self.assertIn(b'"teams"', open_bytes(os.path.join(self.temp.name, broken[0])))
        with open(settings, 'w') as f:
            f.write('{"teams": {"Air": []}}')
        with patch.object(builtins, 'open', side_effect=PermissionError('locked')):
            pb.App.remember(app, volume=10)  # a read error: nothing written
        pb.App.remember(app, volume=20)
        self.assertEqual(pb.App.settings(), {'teams': {'Air': []}, 'volume': 20})

    def test_same_folder_under_another_path_is_not_moved(self):
        name = 'Zook (86bc1263) - current.sky'
        pb.save_dump(os.path.join(pb.DUMPS, name), self.image)
        with patch.object(pb.os.path, 'samefile', return_value=True):  # a junction / mapped drive / 8.3 name
            self.assertEqual(pb.move_dumps(pb.DUMPS, pb.DUMPS + '-link'), 0)
        self.assertEqual(os.listdir(pb.DUMPS), [name])
        self.assertFalse(os.path.exists(pb.DUMPS + '-link'))

    def test_before_edit_copy_stays_beside_a_file_outside_dumps(self):
        with tempfile.TemporaryDirectory() as other:
            before = pb.stamped(self.image, 'before edit', other)
            self.assertEqual(os.path.dirname(before), other)
            self.assertIn(' - before edit ', os.path.basename(before))
        self.assertEqual(os.path.dirname(pb.stamped(self.image, 'saved')), pb.DUMPS)

    def test_game_write_after_a_swap_is_refused_until_the_game_saw_it(self):
        portal, m = self.mirror()
        self.game_polls(portal)
        other = bytearray(self.image)
        other[96:112] = bytes([7]) * 16
        path = os.path.join(pb.DUMPS, 'other.sky')
        pb.save_dump(path, other)
        job = m.restore(path)
        write = m.real.write_block

        def mid_restore(i, block, data):  # the game was saving: its next block arrives while the restore writes
            self.write(portal, 0xEE, block=9)
            write(i, block, data)
        m.real.write_block = mid_restore
        m.poll()
        m.poll()
        self.assertEqual(job.state, 'completed')
        self.assertEqual(bytes(m.real.figures[0]), bytes(other))  # no block of the old save on the restored figure
        self.assertEqual(bytes(portal.slots[0].data), bytes(other))
        self.assertEqual(portal.queries[-1][:3], bytes([ord('W'), 0x01, 9]))  # the game was told it failed
        m.real.write_block = write
        self.game_polls(portal)  # it saw the figure lifted and placed: its saves count again
        self.write(portal, 0x44, block=9)
        m.poll()
        self.assertEqual(m.real.figures[0][144:160], bytes([0x44]) * 16)

    def test_slot_awaiting_its_figure_is_not_free(self):
        portal = pb.Portal(DESC.r_reply)
        a, b, c = (saved_figure(uid) for uid in ('0a0a0a0a', '0b0b0b0b', '0c0c0c0c'))
        portal.load(a, 'a.sky')
        portal.activate()
        self.game_polls(portal)
        portal.remove(0)
        self.assertEqual(portal.load(b, 'b.sky'), 0)
        portal.status()  # the game hears REMOVING: the status alone says the slot is empty
        self.assertEqual(portal.load(c, 'c.sky'), 1)
        self.assertEqual(portal.slots[0].path, 'b.sky')

    def test_figure_lifted_mid_save_is_caught_up_not_repaired(self):
        portal, m = self.mirror()
        new = pb.edit(self.image, gold=4321)  # a game save: a few blocks into the older area, valid checksums
        write, landed = m.real.write_block, []

        def lifted(i, block, data):  # the first block reaches the figure, its read-back does not
            if not landed:
                landed.append(block)
                write(i, block, data)
            raise TimeoutError('lifted')
        m.real.write_block = lifted
        for b in pb.changed_blocks(new, self.image):
            portal.command(bytes([ord('W'), 0x10, b]) + new[b * 16:b * 16 + 16])
        m.poll()
        half = m.real.figures.pop(0)
        self.assertTrue(pb.figure_info(bytes(half)).damaged)  # a half-written save
        m.poll()  # lifted
        m.real.write_block = write
        m.real.figures[0] = half
        m.poll()  # placed again: the game's copy finishes the save
        m.poll()
        self.assertEqual(bytes(m.real.figures[0]), new)
        self.assertTrue(m.repairs.empty() and m.offers.empty())
        self.assertEqual(self.recycled, [])

    def test_real_figure_takes_the_place_of_its_file_copy(self):
        current = pb.dump_path(self.image, 'current')
        pb.save_dump(current, pb.edit(self.image, gold=1))
        pb.save_dump(pb.dump_path(self.image, 'original'), self.image)
        server = pb.Server(pb.Portal(DESC.r_reply), DESC)
        m = pb.Mirror(server, 3)
        path, data = pb.open_dump(current)
        server.portal.load(data, path)  # Add Skylander → Load current
        m.real = FakeReal({0: bytearray(self.image)})
        m.poll()
        loaded = [s for s in server.portal.slots if s.path]
        self.assertEqual(len(loaded), 1)  # not the same figure twice, both on current.sky
        self.assertEqual(bytes(loaded[0].data), self.image)
        self.assertEqual(m.offers.qsize(), 1)  # the file copy's progress: a backup, offered for restore

    def test_figure_moved_to_another_real_slot_stays_loaded(self):
        portal, m = self.mirror()
        m.real.figures = {2: m.real.figures[0]}  # between two polls: it is seen placed before it is seen lifted
        m.poll()
        loaded = [n for n, s in enumerate(portal.slots) if s.path]
        self.assertEqual(len(loaded), 1)
        self.assertEqual(m.slots, {2: (loaded[0], pb.dump_path(self.image, 'current'), self.uid)})

    def test_edit_gives_no_second_area_to_a_figure_without_one(self):
        plain, second = bytearray(pb.crypt(self.image)), (0x110, 0x120, 0x140, 0x150, 0x2D0, 0x2E0, 0x300, 0x310)
        for o in second:  # as after Spyro's Adventure only
            plain[o:o + 16] = bytes(16)
        first_game = pb.repaired(pb.crypt(bytes(plain), encrypt=True))
        edited = pb.edit(first_game, gold=5)
        self.assertFalse(any(any(edited[o:o + 16]) for o in second))
        self.assertEqual((pb.figure_info(edited).gold, pb.figure_info(edited).damaged), (5, False))
        needs_it = pb.edit(first_game, xp=pb.LEVELS[-1])  # level 20 lives in the second area
        self.assertEqual((pb.figure_info(needs_it).level, pb.figure_info(needs_it).damaged), (20, False))

    def test_usbip_server_takes_one_host_and_bounded_transfers(self):
        import socket
        import struct
        listener = socket.create_server(('127.0.0.1', 0))
        self.addCleanup(listener.close)
        threading.Thread(target=pb.Server(pb.Portal(DESC.r_reply), DESC).serve, args=(listener,), daemon=True).start()

        def imported():
            c = socket.create_connection(listener.getsockname())
            self.addCleanup(c.close)
            c.sendall(struct.pack('>HHI', 0x0111, 0x8003, 0) + b'1-1'.ljust(32, b'\0'))
            return c, struct.unpack('>HHI', pb.recv_exact(c, 8))[2]
        first, status = imported()
        self.assertEqual(status, 0)
        pb.recv_exact(first, 0x138)
        self.assertEqual(imported()[1], 1)  # the portal is taken
        first.sendall(struct.pack('>IIIIIIIIII', 1, 1, 0x10001, 0, 0, 0, 2 ** 31, 0, 0xFFFFFFFF, 0) + bytes(8))
        self.assertEqual(first.recv(1), b'')  # 2 GB "for the portal": hung up on, not read into memory
        for _ in range(50):  # the portal is free again
            if imported()[1] == 0:
                break
            pb.time.sleep(0.02)
        else:
            self.fail('the virtual portal stayed taken')

    def test_placed_copy_decides_from_the_images_alone(self):
        left = self.image  # what PortalSync left on the figure
        game = pb.edit(left, gold=4321)  # the game's copy after a save
        blocks = pb.changed_blocks(game, left)
        half = bytearray(left)  # the first block of that save reached the figure
        half[blocks[0] * 16:blocks[0] * 16 + 16] = game[blocks[0] * 16:blocks[0] * 16 + 16]
        elsewhere = pb.edit(left, gold=7)  # played on a console meanwhile
        trailer = bytearray(left)
        trailer[0x70:0x80] = bytes([1]) * 16  # differs in a sector trailer only: nothing a restore would write
        for figure, known, copy, write_back, damaged, want in (
                (left, left, left, True, False, 'same'),
                (left, left, game, True, False, 'catch up'),  # lifted before any block landed
                (bytes(half), left, game, True, False, 'catch up'),  # lifted with one block landed
                (bytes(half), left, game, False, False, 'offer'),  # write-back off: the user decides
                (left, None, game, True, False, 'offer'),  # first placement this run: nothing known about it
                (elsewhere, left, game, True, False, 'offer'),  # played elsewhere: never synced over
                (left, left, game, True, True, 'set aside'),  # a damaged copy is never written nor offered
                (left, left, bytes(trailer), False, False, 'set aside')):
            self.assertEqual(pb.placed_copy(figure, known, copy, write_back, damaged), want)

    def test_original_backup_is_never_recycled(self):
        old, new = self.backups(self.image)
        newer = pb.dump_path(self.image, 'saved 2026-10-18 12.00.00')
        pb.save_dump(newer, self.image)
        pb.keep_one_backup(self.uid)
        self.assertEqual(self.recycled, [os.path.basename(new)])
        self.assertTrue(os.path.exists(old) and os.path.exists(newer))

    def test_unsynced_counts_game_saves_the_figure_lacks(self):
        portal, m = self.mirror()
        ui = SimpleNamespace(mirror=m, root=None)
        m.set_write_back(False)
        with patch('tkinter.messagebox.askyesno', return_value=False) as ask:
            self.assertTrue(pb.App.unsynced_ok(ui, self.uid) and not ask.called)  # nothing to lose: not asked
            self.write(portal, 9)  # write-back off: only in the game's copy
            self.assertEqual(m.unsynced(self.uid), 1)
            self.assertFalse(pb.App.unsynced_ok(ui, self.uid))  # asked, and the user keeps the progress
            self.assertIn('"Write saves to real figures" is off', ask.call_args[0][1])
            m.set_write_back(True)
            m.poll()
            self.assertEqual((m.unsynced(self.uid), m.unsynced('ffffffff')), (0, 0))
            self.assertTrue(pb.App.unsynced_ok(ui, self.uid) and ask.call_count == 1)


def open_bytes(path):
    with open(path, 'rb') as f:
        return f.read()


if __name__ == '__main__':
    unittest.main()
