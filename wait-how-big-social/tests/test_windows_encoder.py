"""Unit checks; mocked subprocess cases are not real encode evidence."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'assets' / 'reviewed' / 'encode_windows_batch.py'
spec = importlib.util.spec_from_file_location('whb_encoder', MODULE)
enc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(enc)


class EncoderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        host = patch.object(enc.platform, 'node', return_value='UNIT-TEST')
        host.start()
        self.addCleanup(host.stop)
        self.root = Path(self.tmp.name)
        self.source = self.make_source(self.root / 'WHB-017')
        self.out = self.root / 'output'

    def make_source(self, path):
        (path / 'scenes').mkdir(parents=True)
        (path / 'SCENE_MANIFEST.json').write_text(json.dumps({'fps': 30, 'frame_counts': [30]}))
        (path / 'scenes/scene-0.png').write_bytes(b'\x89PNG\r\n\x1a\nunit-fixture-only')
        return path

    def manifest(self, data):
        (self.source / 'SCENE_MANIFEST.json').write_text(json.dumps(data))

    def test_actual_captured_bytes_keep_provenance(self):
        item = enc.validate_batch([self.source], self.out)[0]
        original = item['frames'][0]['data']
        (self.source / 'scenes/scene-0.png').write_bytes(b'changed after capture')
        self.assertEqual(item['frames'][0]['data'], original)
        self.assertEqual(item['frames'][0]['sha256'], enc.digest(original))
        self.assertEqual(item['content_id'], 'WHB-017')
        self.assertFalse(self.out.exists())

    def test_invalid_manifests(self):
        for data in ([], {'fps': True, 'frame_counts': [1]}, {'fps': 61, 'frame_counts': [1]},
                     {'fps': 30, 'frame_counts': []}, {'fps': 30, 'frame_counts': [True]},
                     {'fps': 30, 'frame_counts': [0]}, {'fps': 30, 'frame_counts': [18001]},
                     {'fps': 30, 'frame_counts': [1] * 101}):
            with self.subTest(data=data):
                self.manifest(data)
                with self.assertRaises(ValueError):
                    enc.validate_batch([self.source], self.out)
        self.assertFalse(self.out.exists())

    def test_missing_or_invalid_png(self):
        path = self.source / 'scenes/scene-0.png'
        path.write_bytes(b'not png')
        with self.assertRaises(ValueError):
            enc.validate_batch([self.source], self.out)
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            enc.validate_batch([self.source], self.out)

    def test_duplicate_ids_and_bounds(self):
        other = self.make_source(self.root / 'other' / 'WHB-017')
        for sources in ([], [self.source] * 7, [self.source, other]):
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                enc.validate_batch(sources, self.out)

    def test_output_cannot_be_inside_source(self):
        with self.assertRaises(ValueError):
            enc.validate_batch([self.source], self.source / 'new-output')

    def test_existing_output_is_preserved(self):
        self.out.mkdir()
        keep = self.out / 'existing.mp4'
        keep.write_bytes(b'keep')
        with self.assertRaises(ValueError):
            enc.validate_batch([self.source], self.out)
        self.assertEqual(keep.read_bytes(), b'keep')

    def test_escaping_symlink_is_rejected(self):
        external = self.root / 'external.png'
        external.write_bytes(b'\x89PNG\r\n\x1a\nexternal')
        path = self.source / 'scenes/scene-0.png'
        path.unlink()
        try:
            path.symlink_to(external)
        except OSError:
            self.skipTest('Host does not allow unprivileged symlink creation')
        with self.assertRaises(ValueError):
            enc.validate_batch([self.source], self.out)

    def test_env_stop_has_no_side_effects(self):
        with patch.dict(os.environ, {'WHB_STOP': '1'}), patch.object(enc.subprocess, 'Popen') as run:
            code = enc.main(['--scene-dir', str(self.source), '--output-dir', str(self.out),
                             '--ffmpeg', 'not-called', '--ffprobe', 'not-called'])
        self.assertEqual(code, 3)
        run.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_stop_receipt_is_append_only(self):
        item = enc.validate_batch([self.source], self.out)[0]
        self.out.mkdir()
        (self.out / 'STOP').touch()
        receipt = self.out / 'receipts.jsonl'
        receipt.write_text('{"historical":true}\n')
        with patch.object(enc.subprocess, 'Popen') as run:
            row = enc.encode_item(item, self.out, 'unused', 'unused', 'unit-version')
        run.assert_not_called()
        rows = [json.loads(x) for x in receipt.read_text().splitlines()]
        self.assertTrue(rows[0]['historical'])
        self.assertEqual(row['status'], 'stopped')
        self.assertIn('source_inputs', row)
        self.assertIn('elapsed_seconds', row)

    def test_collision_receipt_does_not_overwrite(self):
        item = enc.validate_batch([self.source], self.out)[0]
        self.out.mkdir()
        target = self.out / 'WHB-017-silent-video.mp4'
        target.write_bytes(b'keep')
        with patch.object(enc.subprocess, 'Popen') as run:
            row = enc.encode_item(item, self.out, 'unused', 'unused', 'unit-version')
        run.assert_not_called()
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(target.read_bytes(), b'keep')

    def test_active_stop_kills_encoder(self):
        item = enc.validate_batch([self.source], self.out)[0]
        self.out.mkdir()
        with patch.object(enc.subprocess, 'Popen') as run:
            proc = run.return_value
            proc.stdin.write.side_effect = lambda data: (self.out / 'STOP').touch()
            proc.poll.return_value = None
            proc.wait.return_value = -9
            row = enc.encode_item(item, self.out, 'unused', 'unused', 'unit-version')
            proc.kill.assert_called()
        self.assertEqual(row['status'], 'stopped')

    def test_streaming_deadline_kills_encoder(self):
        item = enc.validate_batch([self.source], self.out)[0]
        self.out.mkdir()
        killed = threading.Event()
        def blocked_write(data):
            if not killed.wait(2):
                raise AssertionError('Watchdog did not kill blocked encoder')
            raise BrokenPipeError('simulated killed encoder')
        with patch.object(enc.subprocess, 'Popen') as run:
            proc = run.return_value
            proc.stdin.write.side_effect = blocked_write
            proc.kill.side_effect = killed.set
            proc.poll.return_value = None
            proc.wait.return_value = -9
            row = enc.encode_item(item, self.out, 'unused', 'unused', 'unit-version', timeout=0.01)
        self.assertTrue(killed.is_set())
        self.assertEqual(row['status'], 'failed')
        self.assertLess(row['elapsed_seconds'], 1)

    def test_probe_rejects_false_success(self):
        item = enc.validate_batch([self.source], self.out)[0]
        good = {'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'width': 1080,
                             'height': 1920, 'nb_read_frames': '30'}], 'format': {'duration': '1'}}
        self.assertEqual(enc.check_probe(good, item)['frames'], 30)
        for change in ('frames', 'audio', 'duration', 'nan', 'codec'):
            probe = json.loads(json.dumps(good))
            if change == 'frames': probe['streams'][0]['nb_read_frames'] = '29'
            if change == 'audio': probe['streams'].append({'codec_type': 'audio'})
            if change == 'duration': probe['format']['duration'] = '1.04'
            if change == 'nan': probe['format']['duration'] = 'NaN'
            if change == 'codec': probe['streams'][0]['codec_name'] = 'other'
            with self.subTest(change=change), self.assertRaises(ValueError):
                enc.check_probe(probe, item)


if __name__ == '__main__':
    unittest.main()
