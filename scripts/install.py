"""Install repository skills safely with automatic backup. Python 3.9+."""
from __future__ import annotations
import argparse
import os
import shutil
from datetime import datetime
from pathlib import Path

IGNORE_PATTERNS = ('__pycache__', '*.pyc', '*.pyo', '.DS_Store')


def parse_args() -> argparse.Namespace:
    default_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Execute installation (default: preview only)')
    parser.add_argument('--target', type=Path, default=default_home / 'skills', help='Target skills directory')
    parser.add_argument('--skill', action='append', dest='skills', help='Install only specified skill ID (repeatable)')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    skills_dir = repo_root / 'skills'
    if not skills_dir.is_dir():
        raise SystemExit(f'Skills directory missing: {skills_dir}')

    source_root = skills_dir.resolve()
    target_root_resolved = args.target.resolve()
    if (
        target_root_resolved == source_root
        or source_root in target_root_resolved.parents
        or target_root_resolved in source_root.parents
    ):
        raise SystemExit(f'Source and target overlap: {args.target}')

    all_sources = {
        p.name: p
        for p in sorted(skills_dir.iterdir())
        if p.is_dir() and (p / 'SKILL.md').is_file()
    }

    if args.skills:
        missing = [name for name in args.skills if name not in all_sources]
        if missing:
            raise SystemExit(f"Skill not found: {', '.join(missing)}")
        sources = [all_sources[name] for name in dict.fromkeys(args.skills)]
    else:
        sources = list(all_sources.values())

    if not sources:
        print('No skills found in skills/ yet.')
        return

    # Pre-validate every destination before modifying anything on disk.
    resolved_targets: list[Path] = []
    for source in sources:
        target = args.target / source.name
        resolved = target.resolve()
        if resolved == source_root or source_root in resolved.parents or resolved in source_root.parents:
            raise SystemExit(f'Source and target overlap: {target}')
        if target.is_symlink() and not target.is_dir():
            raise SystemExit(f'Broken or non-directory skill link: {target}')
        if target.exists() and not target.is_dir():
            raise SystemExit(f'Skill target must be a directory: {target}')
        if target.is_symlink() and any(p.is_symlink() for p in resolved.rglob('*')):
            raise SystemExit(f'Nested links require manual review: {resolved}')
        if any(
            resolved == other or resolved in other.parents or other in resolved.parents
            for other in resolved_targets
        ):
            raise SystemExit(f'Skill destinations overlap: {target}')
        resolved_targets.append(resolved)

    backup_root = args.target.parent / 'skill-backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    ignore = shutil.ignore_patterns(*IGNORE_PATTERNS)

    for source in sources:
        target = args.target / source.name
        print(f'{source.name} -> {target}')
        if not args.apply:
            continue

        args.target.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            resolved = target.resolve(strict=True)
            backup_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(resolved, backup_root / source.name)
            print(f'  Backup: {backup_root / source.name}')
            shutil.copytree(source, resolved, dirs_exist_ok=True, ignore=ignore)
            continue

        if target.exists():
            backup_root.mkdir(parents=True, exist_ok=True)
            target.rename(backup_root / source.name)
            print(f'  Backup: {backup_root / source.name}')

        shutil.copytree(source, target, ignore=ignore)

    if not args.apply:
        print('Preview only. Run with --apply to install.')


if __name__ == '__main__':
    main()
