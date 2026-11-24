"""Repository hygiene gate: block personal paths, private LAN endpoints, and leaked data."""
from __future__ import annotations
import re
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {'.git', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}
SKIP_FILES = {'.DS_Store'}
FORBIDDEN_DATA_DIRS = {'local', 'outputs', 'work', 'StudioData'}
MAX_FILE_BYTES = 512 * 1024  # 512 KB ceiling per tracked repo file

ALLOWED_UNIX_USERS = {'replace-me', 'username', 'user', 'your-name', 'example'}
ALLOWED_WIN_USERS = ALLOWED_UNIX_USERS | {'test', 'prod'}
ALLOWED_SMB_HOSTS = {'主机', 'host', 'server', 'example', 'nas-host', 'replace-me'}

UNIX_USER_RE = re.compile(r'(?<![A-Za-z0-9._-])/(?:Users|home)/([A-Za-z0-9._-]+)')
WIN_USER_RE = re.compile(r'[A-Za-z]:(?:\\+|/)Users(?:\\+|/)([A-Za-z0-9._-]+)')
PRIVATE_IP_RE = re.compile(
    r'(?<![0-9.])(?:'
    r'192\.168\.\d{1,3}\.\d{1,3}'
    r'|10\.\d{1,3}\.\d{1,3}\.\d{1,3}'
    r'|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}'
    r')(?![0-9.])'
)
NET_SHARE_HOST_RE = re.compile(r'\b(?:smb|afp)://([^\s/`"\')>\]]+)')
SKILL_ID_RE = re.compile(r'^studio-[a-z0-9]+(?:-[a-z0-9]+)*$')
FRONTMATTER_NAME_RE = re.compile(r'^---\s*\n(?:.*\n)*?name:\s*([^\s#]+)', re.MULTILINE)


def scan_tree(root: Path) -> list[str]:
    """Return human-readable hygiene violations found under root."""
    issues: list[str] = []

    for path in sorted(root.rglob('*')):
        rel_parts = path.relative_to(root).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        if path.name in SKIP_FILES:
            continue

        if path.is_dir():
            if path.name in FORBIDDEN_DATA_DIRS:
                issues.append(f'{path.relative_to(root)}: forbidden data directory "{path.name}/" inside repo')
            continue

        if not path.is_file():
            continue

        rel = path.relative_to(root).as_posix()
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            issues.append(f'{rel}: file size {size} bytes exceeds {MAX_FILE_BYTES} bytes limit')
            continue

        try:
            content = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            issues.append(f'{rel}: non-UTF-8 or binary file is not allowed in code-only repo')
            continue

        for lineno, line in enumerate(content.splitlines(), 1):
            for match in UNIX_USER_RE.finditer(line):
                user = match.group(1)
                if user not in ALLOWED_UNIX_USERS:
                    issues.append(f'{rel}:{lineno}: hardcoded personal Unix path "{match.group(0)}"')

            for match in WIN_USER_RE.finditer(line):
                user = match.group(1)
                if user not in ALLOWED_WIN_USERS:
                    issues.append(f'{rel}:{lineno}: hardcoded personal Windows path "{match.group(0)}"')

            for match in PRIVATE_IP_RE.finditer(line):
                issues.append(f'{rel}:{lineno}: hardcoded private LAN IP "{match.group(0)}"')

            for match in NET_SHARE_HOST_RE.finditer(line):
                host = match.group(1).split('@')[-1].split(':')[0]
                if host and host not in ALLOWED_SMB_HOSTS:
                    issues.append(f'{rel}:{lineno}: hardcoded network share host "{match.group(0)}"')

    return issues


class RepoHygieneTests(unittest.TestCase):
    def test_repository_has_no_path_or_data_leaks(self) -> None:
        issues = scan_tree(REPO_ROOT)
        self.assertEqual(
            issues,
            [],
            'Repository hygiene violations detected:\n  - ' + '\n  - '.join(issues),
        )

    def test_skills_structure_and_readme_registry(self) -> None:
        skills_dir = REPO_ROOT / 'skills'
        readme = (REPO_ROOT / 'README.md').read_text(encoding='utf-8')
        skill_dirs = [p for p in sorted(skills_dir.iterdir()) if p.is_dir() and p.name not in SKIP_DIRS]
        self.assertGreater(len(skill_dirs), 0, 'Expected at least one skill under skills/')

        for sdir in skill_dirs:
            with self.subTest(skill=sdir.name):
                self.assertRegex(sdir.name, SKILL_ID_RE, 'Skill directory must use studio-<kebab-case>')
                skill_md = sdir / 'SKILL.md'
                changelog_md = sdir / 'CHANGELOG.md'
                self.assertTrue(skill_md.is_file(), f'Missing {skill_md.relative_to(REPO_ROOT)}')
                self.assertTrue(changelog_md.is_file(), f'Missing {changelog_md.relative_to(REPO_ROOT)}')

                match = FRONTMATTER_NAME_RE.match(skill_md.read_text(encoding='utf-8'))
                self.assertIsNotNone(match, f'Missing YAML frontmatter name in {skill_md.name}')
                self.assertEqual(match.group(1), sdir.name, 'SKILL.md frontmatter name must match folder name')
                self.assertIn(f'[{sdir.name}](skills/{sdir.name}/SKILL.md)', readme)

    def test_scanner_catches_seeded_violations(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'outputs').mkdir()
            leaked_unix = '/' + 'Users/' + 'alice/Documents/StudioData'
            leaked_win = 'C:' + '\\Users\\' + 'bob\\Documents\\StudioData'
            leaked_ip = '.'.join(['192', '168', '31', '88'])
            leaked_smb = 'smb://' + 'mile-nas.local/photos'
            ok_unix = '/' + 'Users/' + 'replace-me/Documents/StudioData'

            (root / 'SKILL.md').write_text(
                '\n'.join([leaked_unix, leaked_win, leaked_ip, leaked_smb, ok_unix]),
                encoding='utf-8',
            )
            issues = scan_tree(root)
            joined = '\n'.join(issues)
            self.assertIn('forbidden data directory "outputs/"', joined)
            self.assertIn('hardcoded personal Unix path', joined)
            self.assertIn('hardcoded personal Windows path', joined)
            self.assertIn('hardcoded private LAN IP', joined)
            self.assertIn('hardcoded network share host', joined)
            self.assertNotIn('replace-me', joined)


if __name__ == '__main__':
    unittest.main()
