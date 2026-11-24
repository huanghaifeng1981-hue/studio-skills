#!/usr/bin/env python3
"""Local manifest-based photo copying. No image inspection or content hashing."""
from __future__ import annotations
import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.heic', '.heif', '.tif', '.tiff',
    '.cr2', '.cr3', '.arw', '.nef', '.dng', '.raf', '.orf', '.rw2',
}


def exact_name(folder: str, names: list[str] | set[str]) -> bool:
    """Match complete tokenized names after date+photographer prefix; never match substrings."""
    match = re.fullmatch(r'\d{4}[.-]\d{1,2}[.-]\d{1,2}[A-Za-z]*\s*(.+)', folder)
    if not match:
        return False
    tokens = re.split(r'[\s、,，/&＆]+', match.group(1).strip())
    return bool(set(tokens) & {n.strip() for n in names if n.strip()})


def save(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def safe_path(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise ValueError('Invalid relative path')
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Symbolic links are not followed')
    if not inside(current.resolve(), root):
        raise ValueError('Path escapes configured root')
    return current


def plan(source: Path, target: Path, folders: list[str], state: Path) -> None:
    source, target, state = source.resolve(), target.resolve(), state.resolve()
    if not source.is_dir() or not target.is_dir():
        raise ValueError('Source and target must already be accessible directories')
    if inside(source, target) or inside(target, source):
        raise ValueError('Source and target must not overlap')
    if inside(state, source) or inside(state, target):
        raise ValueError('State must be stored locally outside both photo roots')
    if state.exists():
        raise ValueError('State exists; resume it or choose a new task state')

    data = {'version': 1, 'source': str(source), 'target': str(target), 'folders': {}, 'files': {}}
    for folder in dict.fromkeys(folders):
        base = safe_path(source, folder)
        if not base.is_dir():
            raise ValueError('Missing folder: ' + folder)
        count = 0

        def fail(error):
            raise error

        for root, dirs, files in os.walk(base, onerror=fail, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith('.') and not (Path(root) / d).is_symlink()]
            for name in sorted(files):
                p = Path(root) / name
                if name.startswith('.') or p.is_symlink() or p.suffix.lower() not in EXTENSIONS:
                    continue
                relative = p.relative_to(source).as_posix()
                if relative not in data['files']:
                    data['files'][relative] = {'folder': folder, 'status': 'pending'}
                    count += 1
        data['folders'][folder] = count

    state.parent.mkdir(parents=True, exist_ok=True)
    save(state, data)


def worker(source: Path, target: Path, relative: str, journal: Path | None = None) -> None:
    source, target = source.resolve(), target.resolve()
    if not source.is_dir() or not target.is_dir():
        raise OSError('Share unavailable')
    src, dst = safe_path(source, relative), safe_path(target, relative)
    if not src.is_file():
        raise OSError('Source unavailable')

    if journal and journal.exists():
        owned = json.loads(journal.read_text(encoding='utf-8'))
        if owned.get('target') != str(dst):
            raise RuntimeError('Unresolved partial file from another transfer')
        if dst.exists():
            if dst.stat().st_ino != owned['inode']:
                raise RuntimeError('Partial destination identity changed; not overwritten')
            dst.unlink()
        journal.unlink()

    if dst.exists():
        print('existing')
        return

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name('.' + dst.name + '.' + uuid.uuid4().hex + '.copying')
    try:
        with src.open('rb') as read, tmp.open('xb') as write:
            while True:
                block = read.read(1024 * 1024)
                if not block:
                    break
                write.write(block)
            write.flush()
            os.fsync(write.fileno())

        # macOS supports exclusive atomic rename on SMB, which may lack hard links.
        try:
            if sys.platform == 'darwin':
                libc = ctypes.CDLL(None, use_errno=True)
                rename = libc.renamex_np
                rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
                rename.restype = ctypes.c_int
                if rename(os.fsencode(tmp), os.fsencode(dst), 4) != 0:  # RENAME_EXCL
                    code = ctypes.get_errno()
                    if code in (errno.ENOTSUP, errno.EOPNOTSUPP) and journal:
                        # SMB fallback: exclusive creation plus a local ownership journal.
                        with dst.open('xb') as out:
                            save(journal, {'target': str(dst), 'inode': os.fstat(out.fileno()).st_ino})
                            with tmp.open('rb') as inp:
                                shutil.copyfileobj(inp, out, 1024 * 1024)
                            out.flush()
                            os.fsync(out.fileno())
                        journal.unlink()
                    else:
                        raise OSError(code, os.strerror(code), str(dst))
            else:
                os.link(tmp, dst)
        except FileExistsError:
            print('existing')
        else:
            print('copied')
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def run(state: Path, timeout: float) -> int:
    data = json.loads(state.read_text(encoding='utf-8'))
    for relative, entry in data['files'].items():
        if entry['status'] in ('copied', 'existing'):
            continue
        entry['status'] = 'pending'
        save(state, data)
        try:
            result = subprocess.run(
                [sys.executable, __file__, '_copy', data['source'], data['target'], relative, str(state.resolve()) + '.active'],
                capture_output=True,
                text=True,
                encoding='utf-8',
                timeout=timeout,
            )
            status = result.stdout.strip()
            if result.returncode or status not in ('copied', 'existing'):
                raise RuntimeError(result.stderr[-1000:] or 'Copy failed')
            entry['status'] = status
            entry.pop('error', None)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            entry.update(status='failed', error=str(error))
            save(state, data)
            print('Stopped; progress saved. Resume with the same state.', flush=True)
            return 1
        save(state, data)
        print(json.dumps({'file': relative, 'status': entry['status']}, ensure_ascii=False), flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)

    q = sub.add_parser('plan')
    q.add_argument('--source', type=Path, required=True)
    q.add_argument('--target', type=Path, required=True)
    q.add_argument('--state', type=Path, required=True)
    q.add_argument('--folder', action='append', required=True)

    q = sub.add_parser('run')
    q.add_argument('--state', type=Path, required=True)
    q.add_argument('--timeout', type=float, default=120)

    q = sub.add_parser('report')
    q.add_argument('--state', type=Path, required=True)

    q = sub.add_parser('_copy')
    q.add_argument('source', type=Path)
    q.add_argument('target', type=Path)
    q.add_argument('relative')
    q.add_argument('journal', nargs='?', type=Path)

    a = p.parse_args()
    if a.command == 'plan':
        plan(a.source, a.target, a.folder, a.state)
    elif a.command == 'run':
        if a.timeout <= 0:
            p.error('timeout must be positive')
        return run(a.state, a.timeout)
    elif a.command == '_copy':
        worker(a.source, a.target, a.relative, a.journal)
    else:
        data = json.loads(a.state.read_text(encoding='utf-8'))
        for folder, count in data['folders'].items():
            records = [v for v in data['files'].values() if v['folder'] == folder]
            counts = {s: sum(v['status'] == s for v in records) for s in ('copied', 'existing', 'pending', 'failed')}
            print(json.dumps({'folder': folder, 'planned': count, **counts}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
