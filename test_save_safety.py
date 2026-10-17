"""Hardware-free regressions: python -m unittest -v test_save_safety."""
import builtins
import os
import tempfile
import unittest
from unittest.mock import patch

import portal_bridge as pb
from test_portal_bridge import DESC, FakeReal, saved_figure


class SaveSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 0.0
        self.recycled = []
        self.enterContext(patch.object(pb, 'DUMPS', self.temp.name))
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


def open_bytes(path):
    with open(path, 'rb') as f:
        return f.read()


if __name__ == '__main__':
    unittest.main()
