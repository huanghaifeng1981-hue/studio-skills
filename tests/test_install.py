"""Isolated unit tests for scripts/install.py."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1] / 'scripts' / 'install.py'


class InstallScriptTests(unittest.TestCase):
    def _make_repo(self, root: Path, skill_names: list[str]) -> Path:
        repo = root / 'repo'
        (repo / 'scripts').mkdir(parents=True)
        (repo / 'skills').mkdir(parents=True)
        (repo / 'scripts' / 'install.py').write_bytes(INSTALLER.read_bytes())
        for name in skill_names:
            sdir = repo / 'skills' / name
            sdir.mkdir(parents=True)
            (sdir / 'SKILL.md').write_text(f'# {name}\nnew', encoding='utf-8')
        return repo

    def test_empty_repo_and_overlap_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._make_repo(root, [])
            target = root / 'installed'
            res = subprocess.run(
                [sys.executable, str(repo / 'scripts' / 'install.py'), '--target', str(target)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn('No skills found', res.stdout)

            bad = subprocess.run(
                [sys.executable, str(repo / 'scripts' / 'install.py'), '--target', str(repo / 'skills'), '--apply'],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(bad.returncode, 0)

    def test_preview_symlink_update_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._make_repo(root, ['demo'])
            runtime = root / 'runtime' / 'demo'
            runtime.mkdir(parents=True)
            (runtime / 'SKILL.md').write_text('old', encoding='utf-8')
            (runtime / 'keep.txt').write_text('keep', encoding='utf-8')

            target = root / 'skills'
            target.mkdir()
            (target / 'demo').symlink_to(runtime, target_is_directory=True)

            cmd = [sys.executable, str(repo / 'scripts' / 'install.py'), '--target', str(target)]
            subprocess.run(cmd, check=True, capture_output=True)
            self.assertEqual((runtime / 'SKILL.md').read_text(encoding='utf-8'), 'old')

            subprocess.run(cmd + ['--apply'], check=True, capture_output=True)
            self.assertTrue((target / 'demo').is_symlink())
            self.assertIn('new', (runtime / 'SKILL.md').read_text(encoding='utf-8'))
            self.assertEqual((runtime / 'keep.txt').read_text(encoding='utf-8'), 'keep')
            backups = list((root / 'skill-backups').glob('*/demo/SKILL.md'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding='utf-8'), 'old')

    def test_single_skill_filter_and_missing_skill(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._make_repo(root, ['alpha', 'beta'])
            target = root / 'installed'

            subprocess.run(
                [
                    sys.executable,
                    str(repo / 'scripts' / 'install.py'),
                    '--target',
                    str(target),
                    '--skill',
                    'alpha',
                    '--apply',
                ],
                check=True,
                capture_output=True,
            )
            self.assertTrue((target / 'alpha' / 'SKILL.md').is_file())
            self.assertFalse((target / 'beta').exists())

            missing = subprocess.run(
                [
                    sys.executable,
                    str(repo / 'scripts' / 'install.py'),
                    '--target',
                    str(target),
                    '--skill',
                    'nonexistent',
                    '--apply',
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(missing.returncode, 0)

    def test_precheck_aborts_before_any_partial_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self._make_repo(root, ['a', 'z'])
            target = root / 'installed'
            (target / 'a').mkdir(parents=True)
            (target / 'a' / 'SKILL.md').write_text('old', encoding='utf-8')
            (target / 'z').symlink_to(root / 'missing')

            res = subprocess.run(
                [sys.executable, str(repo / 'scripts' / 'install.py'), '--target', str(target), '--apply'],
                capture_output=True,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertEqual((target / 'a' / 'SKILL.md').read_text(encoding='utf-8'), 'old')
            self.assertFalse((root / 'skill-backups').exists())


if __name__ == '__main__':
    unittest.main()
