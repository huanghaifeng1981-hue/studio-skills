"""Unit tests for studio-photo-folder-match scripts/copy_photos.py."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'copy_photos.py'
spec = importlib.util.spec_from_file_location('copy_photos', SCRIPT)
copy_photos = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(copy_photos)


class CopyPhotosTests(unittest.TestCase):
    def test_exact_name_never_matches_substrings(self) -> None:
        self.assertTrue(copy_photos.exact_name('2026.10.24r 王燕', ['王燕']))
        self.assertFalse(copy_photos.exact_name('2026.10.24r 王燕玲', ['王燕']))
        self.assertTrue(copy_photos.exact_name('2026-11-5ab 张三、李小宝', ['李小宝']))
        self.assertFalse(copy_photos.exact_name('未知无日期目录 王燕', ['王燕']))

    def test_plan_rejects_overlap_state_inside_roots_and_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / 'src'
            dst = root / 'dst'
            (src / '2026.10.24r 严青青').mkdir(parents=True)
            dst.mkdir()

            with self.assertRaises(ValueError):
                copy_photos.plan(src, src / 'nested', ['2026.10.24r 严青青'], root / 'state.json')

            with self.assertRaises(ValueError):
                copy_photos.plan(src, dst, ['2026.10.24r 严青青'], dst / 'state.json')

            with self.assertRaises(ValueError):
                copy_photos.plan(src, dst, ['../outside'], root / 'state.json')

    def test_plan_run_skip_existing_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / 'src'
            dst = root / 'dst'
            state = root / 'state' / 'task.json'
            folder = '2026.10.24r 严青青'

            (src / folder / '已修').mkdir(parents=True)
            (src / folder / 'A001.JPG').write_bytes(b'new-a')
            (src / folder / '已修' / 'A002.arw').write_bytes(b'new-b')
            (src / folder / 'ignore.mp4').write_bytes(b'video')
            (src / folder / '.hidden.jpg').write_bytes(b'hidden')

            # Pre-populate one existing file to verify no-overwrite guarantee.
            (dst / folder).mkdir(parents=True)
            (dst / folder / 'A001.JPG').write_bytes(b'keep-existing')

            subprocess.run(
                [sys.executable, str(SCRIPT), 'plan', '--source', str(src), '--target', str(dst), '--state', str(state), '--folder', folder],
                check=True,
            )
            subprocess.run([sys.executable, str(SCRIPT), 'run', '--state', str(state)], check=True)
            rep = subprocess.run(
                [sys.executable, str(SCRIPT), 'report', '--state', str(state)],
                check=True,
                capture_output=True,
                text=True,
            )
            summary = json.loads(rep.stdout.strip())
            self.assertEqual(summary['planned'], 2)
            self.assertEqual(summary['existing'], 1)
            self.assertEqual(summary['copied'], 1)
            self.assertEqual(summary['failed'], 0)
            self.assertEqual((dst / folder / 'A001.JPG').read_bytes(), b'keep-existing')
            self.assertEqual((dst / folder / '已修' / 'A002.arw').read_bytes(), b'new-b')
            self.assertFalse((dst / folder / 'ignore.mp4').exists())


if __name__ == '__main__':
    unittest.main()
