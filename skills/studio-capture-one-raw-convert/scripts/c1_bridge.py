#!/usr/bin/env python3
"""Deterministic Capture One Pro AppleScript bridge for macOS RAW conversion.

Provides CLI subcommands with crash-consistent file+ledger commits and strict rollback gates:
1. `status`: Check Capture One status, optionally browse a Session RAW folder, and list variants.
2. `init-batch`: Register expected RAW photos and exhaustively verify every requested `--selected-ids`
   item, preserving full filenames when stems collide across extensions (`A.CR2` vs `A.NEF`).
3. `prepare-variant`: Idempotently clone a source variant, snapshot pre-existing inherited client
   layers, restore full uncropped frame, and safely apply default portrait retouching.
4. `inspect`: Read back all 28 global and per-layer adjustments of a variant.
5. `apply` / `rollback`:
   - Rejects non-finite (`NaN`/`Inf`) or non-positive tolerances BEFORE any write.
   - Rejects mask `feather`/`refine` operations on BOTH new and existing layers BEFORE write because
     Capture One AppleScript exposes no read-back property to independently verify raster mask results;
     `verify_readback` never skips `feather`/`refine`, and `rollback` deletes newly created layers.
   - Supports resuming partially executed multi-slider rollbacks via `{pre_rollback, target_rollback}`
     lattice verification AND multi-new-layer partial-deletion rollbacks via exact deleted-predecessor
     stack index shift calculation, while blocking unrelated reorders or substitute layers with zero writes.
6. `ensure-recipes`, `export` & `resolve-export-conflict`:
   - Canonicalizes `output_dir` while preserving the leaf filename un-resolved (`canonicalize_leaf_path`),
     blocking active and dangling symlinks; enforces exclusive directory write permissions on `--overwrite-own`
     and offers `--versioned` non-overwriting versioned filenames for shared directories.
   - Proves export file ownership via kernel `(st_dev, st_ino)` (`candidate_file_ids` & `previous_file_id`)
     rather than content hash alone:
     * A pre-existing or early-arriving external file with IDENTICAL bytes is NEVER deleted on abort,
       NEVER granted an ownership receipt on crash recovery, and NEVER adopted by `restore-backup`;
     * Legacy `pending_export_commit` records lacking reliable inode proof (`candidate_file_ids`) are
       immediately latched into conflict for manual resolution rather than defaulting to trusted.
   - Uses kernel-atomic `renamex_np(..., RENAME_SWAP)` for overwrites and backup rollbacks, and during
     crash recovery jointly verifies BOTH `target_path` and `temp_publish_path` — cleaning up the swap
     temp path ONLY after confirming the displaced file's `(st_dev, st_ino)` and SHA-256 belong to the
     original receipt.
   - Enforces exclusive directory write constraints across both initial publish and overwrite commits, and
     extends full-file content SHA-256 continuity verification through the completion of receipt save
     (catching equal-length in-place overwrites during `save_batch_ledger` and revoking the receipt).
   - Latches persisted conflicts across repeated ledger reloads until explicitly cleared via
     `resolve-export-conflict` (`--resolution keep-external | restore-backup`).
7. `record-handoff`: Blocks `passed` whenever any adjustment/rollback round is unresolved, re-audits
   every row in `handoff.jsonl` against current `adjustment_seq` and live SHA-256 receipts, and checks
   completeness against the full expected batch manifest.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from tempfile import TemporaryDirectory
import time
from typing import Any
import uuid

APP_NAME = "Capture One"
DEFAULT_PREVIEW_RECIPE = "AI_Preview_1536_NoMeta"
DEFAULT_HANDOFF_RECIPE = "AI_Handoff_Full_sRGB100"
DEFAULT_SRGB_PROFILE = "sRGB Color Space Profile"
PREVIEW_MANIFEST_FILENAME = ".c1_preview_manifest.json"

RAW_EXTENSIONS: frozenset[str] = frozenset({
    ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2",
    ".raf", ".dng", ".iiq", ".orf", ".rw2", ".pef", ".3fr", ".erf",
})

ADJUSTMENT_PROP_MAP: dict[str, str] = {
    "temperature": "temperature",
    "tint": "tint",
    "exposure": "exposure",
    "brightness": "brightness",
    "contrast": "contrast",
    "saturation": "saturation",
    "highlight_adjustment": "highlight adjustment",
    "highlight_recovery": "highlight recovery",
    "shadow_recovery": "shadow recovery",
    "white_recovery": "white recovery",
    "black_recovery": "black recovery",
    "clarity_amount": "clarity amount",
    "clarity_structure": "clarity structure",
    "dehaze_amount": "dehaze amount",
    "level_shadow_rgb": "level shadow rgb",
    "level_midtone_rgb": "level midtone rgb",
    "level_highlight_rgb": "level highlight rgb",
    "color_balance_master_hue": "color balance master hue",
    "color_balance_master_saturation": "color balance master saturation",
    "color_balance_shadow_hue": "color balance shadow hue",
    "color_balance_shadow_saturation": "color balance shadow saturation",
    "color_balance_shadow_lightness": "color balance shadow lightness",
    "color_balance_midtone_hue": "color balance midtone hue",
    "color_balance_midtone_saturation": "color balance midtone saturation",
    "color_balance_midtone_lightness": "color balance midtone lightness",
    "color_balance_highlight_hue": "color balance highlight hue",
    "color_balance_highlight_saturation": "color balance highlight saturation",
    "color_balance_highlight_lightness": "color balance highlight lightness",
}

FIELD_TOLERANCES: dict[str, float] = {
    "exposure": 0.02,           # EV
    "tint": 0.10,               # Tint units
    "temperature": 15.0,        # Kelvin quantization in Capture One
    "level_midtone_rgb": 0.02,  # Gamma-like midtone slider
}
DEFAULT_SLIDER_TOLERANCE: float = 0.5

MASK_SPECS: dict[str, dict[str, str]] = {
    "face_skin": {"mode": "people", "area": "face skin", "default_name": "AI_Face_Skin"},
    "body_skin": {"mode": "people", "area": "body skin", "default_name": "AI_Body_Skin"},
    "clothes": {"mode": "people", "area": "clothes", "default_name": "AI_Clothes"},
    "subject": {"mode": "layer_kind", "kind": "subject mask", "default_name": "AI_Subject"},
    "background": {"mode": "layer_kind", "kind": "background mask", "default_name": "AI_Background"},
}


class BridgeError(RuntimeError):
    """Raised when Capture One AppleScript execution or safety gate validation fails."""


def is_recoverable_number(val: Any) -> bool:
    """Return True iff `val` is a finite int/float (not None, not bool, not NaN/Inf)."""
    if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    return math.isfinite(float(val))


def validate_tolerance_scale(tolerance_scale: Any) -> float:
    """Reject NaN, Inf, booleans, strings, and non-positive values for `tolerance_scale`."""
    if not is_recoverable_number(tolerance_scale) or not (float(tolerance_scale) > 0.0):
        raise ValueError(
            f"tolerance_scale must be a finite positive number, got {tolerance_scale!r}"
        )
    return float(tolerance_scale)


def parse_positive_finite_float(raw: str) -> float:
    """Argparse type validator rejecting NaN, Inf, and non-positive numbers at CLI entry."""
    try:
        val = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid float value: {raw!r}") from exc
    if not math.isfinite(val) or not (val > 0.0):
        raise argparse.ArgumentTypeError(
            f"Value must be a finite positive float (not NaN/Inf/<=0), got {raw!r}"
        )
    return val


def within_tolerance(actual: Any, expected: Any, tol: Any, field_key: str | None = None) -> bool:
    """NaN-proof numeric comparison: returns False if any operand is non-finite.
    For cyclic 360-degree hue fields (`*_hue`), compares shortest angular distance modulo 360."""
    if not (is_recoverable_number(actual) and is_recoverable_number(expected) and is_recoverable_number(tol)):
        return False
    t = float(tol)
    if not (t >= 0.0):
        return False
    diff = abs(float(actual) - float(expected))
    if field_key and field_key.endswith("_hue"):
        diff = diff % 360.0
        diff = min(diff, 360.0 - diff)
    return diff <= t


def compute_file_sha256(path: Path) -> tuple[str, int]:
    """Return (hex_sha256, byte_size) of a file on disk."""
    hasher = hashlib.sha256()
    total = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
    return hasher.hexdigest(), total


_MACOS_RENAME_SWAP = 0x00000002
_MACOS_RENAME_EXCL = 0x00000004


def validate_safe_leaf_filename(filename: str) -> str:
    """Ensure `filename` is a single safe directory entry (no path separators, NULs, `.` or `..`)."""
    cleaned = str(filename or "").strip()
    if (
        not cleaned
        or cleaned in (".", "..")
        or "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
    ):
        raise BridgeError(f"Unsafe or invalid output filename {filename!r}")
    return cleaned


def canonicalize_leaf_path(raw_path: Path | str) -> Path:
    """Resolve symlinks on parent directory ONLY, keeping the final leaf entry un-followed!"""
    expanded = Path(raw_path).expanduser()
    leaf = validate_safe_leaf_filename(expanded.name)
    resolved_parent = expanded.parent.resolve()
    return resolved_parent / leaf


def verify_exclusive_output_dir(out_dir: Path) -> None:
    """Enforce exclusive single-owner directory write permissions when performing in-place overwrites.

    Closes R9 P1 #2:
    In-place `--overwrite-own` requires `out_dir` to be owned by the current UID and closed to external
    writers (`S_IWOTH` forbidden; `S_IWGRP` allowed ONLY if shielded by an ancestor `0700` directory owned
    by current UID). For shared directories, callers must use `--versioned` or `--suffix` non-overwriting names.
    """
    resolved_dir = out_dir.resolve()
    st = os.lstat(resolved_dir)
    if not stat.S_ISDIR(st.st_mode):
        raise BridgeError(f"Output directory path is not a directory: {resolved_dir}")
    if hasattr(os, "getuid"):
        uid = os.getuid()
        if st.st_uid != uid:
            raise BridgeError(
                f"SAFETY BLOCK: Output directory {resolved_dir} is owned by uid={st.st_uid} (current uid={uid}); "
                "refusing in-place overwrite in non-exclusive directory. Use --versioned or --suffix instead."
            )
        if (st.st_mode & stat.S_IWOTH) != 0:
            raise BridgeError(
                f"SAFETY BLOCK: Output directory {resolved_dir} is world-writable (mode {oct(st.st_mode & 0o777)}); "
                "refusing in-place overwrite in shared directory. Use --versioned or chmod 700/755."
            )
        if (st.st_mode & stat.S_IWGRP) != 0:
            shielded_by_private_ancestor = False
            cur = resolved_dir
            while True:
                try:
                    ast = os.lstat(cur)
                    if ast.st_uid == uid and (ast.st_mode & 0o077) == 0:
                        shielded_by_private_ancestor = True
                        break
                except OSError:
                    break
                if cur.parent == cur:
                    break
                cur = cur.parent
            if not shielded_by_private_ancestor:
                raise BridgeError(
                    f"SAFETY BLOCK: Output directory {resolved_dir} allows group writes (mode {oct(st.st_mode & 0o777)}) "
                    "without a private 0700 parent barrier; refusing in-place overwrite. Use --versioned or chmod 755."
                )


def inspect_destination_state_unlocked(
    target_path: Path,
    mid_read_hook=None,
) -> tuple[str, str | None, int | None, tuple[int, int] | None]:
    """Inspect `target_path` WITHOUT following symlinks and verify path->inode continuity AFTER reading!

    Closes R8 P2 #4 and R9 P1 #3:
    Returns `(kind, sha256_hex, byte_size, (st_dev, st_ino))` where `kind` is one of:
    - `"missing"`: path does not exist;
    - `"non_regular"`: path is a symlink (active or dangling), directory, FIFO, socket, or device;
    - `"replaced_during_read"` / `"removed_during_read"` / `"modified_during_read"`: destination path
      changed or was unlinked/mutated while its bytes were being hashed;
    - `"regular"`: regular file whose `(st_dev, st_ino)`, size, and mtime remained bound to `target_path`
      from before `open()` through the completion of full-file hashing.
    """
    try:
        st_pre = os.lstat(target_path)
    except FileNotFoundError:
        return ("missing", None, None, None)

    if not stat.S_ISREG(st_pre.st_mode):
        return ("non_regular", None, None, None)

    pre_id = (int(st_pre.st_dev), int(st_pre.st_ino))
    open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(target_path), open_flags)
    except FileNotFoundError:
        return ("missing", None, None, None)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EFTYPE if hasattr(errno, "EFTYPE") else errno.ELOOP, errno.EISDIR):
            return ("non_regular", None, None, None)
        raise

    hasher = hashlib.sha256()
    total = 0
    hook_fired = False
    with os.fdopen(fd, "rb") as fh:
        fst_open = os.fstat(fh.fileno())
        if not stat.S_ISREG(fst_open.st_mode):
            return ("non_regular", None, None, None)
        if (int(fst_open.st_dev), int(fst_open.st_ino)) != pre_id:
            return ("replaced_during_read", None, None, None)

        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
            if not hook_fired and mid_read_hook is not None:
                hook_fired = True
                mid_read_hook()

        if not hook_fired and mid_read_hook is not None:
            hook_fired = True
            mid_read_hook()

        fst_end = os.fstat(fh.fileno())
        if fst_end.st_size != total or fst_end.st_mtime_ns != fst_open.st_mtime_ns:
            return ("modified_during_read", None, None, None)

        # Second-pass content verification on the same fd to catch equal-length in-place writes
        # that occurred during `mid_read_hook` even within the same timestamp tick!
        fh.seek(0)
        hasher_verify = hashlib.sha256()
        total_verify = 0
        while True:
            chunk2 = fh.read(65536)
            if not chunk2:
                break
            hasher_verify.update(chunk2)
            total_verify += len(chunk2)
        first_digest = hasher.hexdigest()
        if total_verify != total or hasher_verify.hexdigest() != first_digest:
            return ("modified_during_read", None, None, None)

    # Re-verify `target_path` STILL points to the exact `(st_dev, st_ino)` we just finished hashing!
    try:
        st_post = os.lstat(target_path)
    except FileNotFoundError:
        return ("removed_during_read", None, None, None)

    if not stat.S_ISREG(st_post.st_mode):
        return ("non_regular", None, None, None)
    if (int(st_post.st_dev), int(st_post.st_ino)) != pre_id or st_post.st_size != total:
        return ("replaced_during_read", None, None, None)

    return ("regular", first_digest, total, pre_id)


def copy_file_verified_to_temp(
    src_path: Path,
    dst_temp_path: Path,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
    on_temp_created_fn=None,
) -> tuple[str, int]:
    """Copy `src_path` to `dst_temp_path` on target filesystem, fsync, and verify full SHA-256/size."""
    dst_temp_path.parent.mkdir(parents=True, exist_ok=True)
    dst_temp_path.unlink(missing_ok=True)
    try:
        with src_path.open("rb") as sfh, dst_temp_path.open("wb") as dfh:
            if on_temp_created_fn is not None:
                fst = os.fstat(dfh.fileno())
                on_temp_created_fn((int(fst.st_dev), int(fst.st_ino)))
            shutil.copyfileobj(sfh, dfh)
            dfh.flush()
            os.fsync(dfh.fileno())
        copied_sha, copied_size = compute_file_sha256(dst_temp_path)
        if expected_sha256 is not None and copied_sha != expected_sha256:
            raise BridgeError(
                f"Temp copy SHA-256 mismatch at {dst_temp_path}: expected {expected_sha256}, got {copied_sha}"
            )
        if expected_size is not None and copied_size != int(expected_size):
            raise BridgeError(
                f"Temp copy byte_size mismatch at {dst_temp_path}: expected {expected_size}, got {copied_size}"
            )
        return copied_sha, copied_size
    except BaseException:
        dst_temp_path.unlink(missing_ok=True)
        raise


def create_backup_copy(src_path: Path, backup_path: Path, expected_sha256: str | None = None) -> None:
    """Create an independent-inode crash-safe backup of `src_path` at `backup_path`.

    Uses a verified physical copy (NEVER `os.link`) so an external in-place `open(target, 'wb')`
    truncation on `src_path` can NEVER mutate `backup_path` via a shared inode!
    """
    backup_path.unlink(missing_ok=True)
    part_path = backup_path.parent / f"{backup_path.name}.part.{uuid.uuid4().hex[:8]}"
    copy_file_verified_to_temp(src_path, part_path, expected_sha256=expected_sha256)
    os.replace(part_path, backup_path)


def call_macos_renamex_flags(src_path: Path, dst_path: Path, flags: int) -> tuple[bool, int]:
    """Invoke macOS kernel `renamex_np(src, dst, flags)`. Returns `(succeeded, errno_val)`."""
    if sys.platform != "darwin":
        return (False, errno.ENOTSUP)
    libc = ctypes.CDLL(None, use_errno=True)
    ctypes.set_errno(0)
    rc = libc.renamex_np(
        os.fsencode(str(src_path)),
        os.fsencode(str(dst_path)),
        ctypes.c_uint(flags),
    )
    if rc == 0:
        return (True, 0)
    return (False, ctypes.get_errno() or errno.EIO)


def call_macos_renamex_excl(src_path: Path, dst_path: Path) -> tuple[bool, int]:
    """Invoke macOS kernel `renamex_np(src, dst, RENAME_EXCL)`."""
    return call_macos_renamex_flags(src_path, dst_path, _MACOS_RENAME_EXCL)


def call_macos_renamex_swap(src_path: Path, dst_path: Path) -> tuple[bool, int]:
    """Invoke macOS kernel `renamex_np(src, dst, RENAME_SWAP)`."""
    return call_macos_renamex_flags(src_path, dst_path, _MACOS_RENAME_SWAP)


def publish_verified_temp_no_overwrite(temp_path: Path, final_path: Path) -> tuple[int, int]:
    """Atomically publish an already-verified same-directory `temp_path` to `final_path` WITHOUT overwriting.

    Returns the published `(st_dev, st_ino)` tuple.
    """
    kind, _, _, _ = inspect_destination_state_unlocked(final_path)
    if kind != "missing":
        temp_path.unlink(missing_ok=True)
        if kind == "non_regular":
            raise BridgeError(f"Refusing to publish to symlink or non-regular path at {final_path}.")
        raise BridgeError(f"Refusing to overwrite existing file at {final_path} (pre-publish check).")

    temp_st = os.lstat(temp_path)
    pub_id = (int(temp_st.st_dev), int(temp_st.st_ino))

    # 1. Try POSIX kernel-atomic hardlink (`EEXIST` on any existing entry including dangling symlinks)
    try:
        os.link(temp_path, final_path)
        temp_path.unlink(missing_ok=True)
        return pub_id
    except FileExistsError as exc:
        temp_path.unlink(missing_ok=True)
        raise BridgeError(
            f"Refusing to overwrite existing file at {final_path} (atomic link EEXIST)."
        ) from exc
    except OSError as link_exc:
        if link_exc.errno not in (
            errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL, errno.EMLINK
        ):
            temp_path.unlink(missing_ok=True)
            raise BridgeError(f"Failed to link verified temp file to {final_path}: {link_exc}") from link_exc

    # 2. Hardlinks unsupported: require kernel-atomic `renamex_np(..., RENAME_EXCL)` (NEVER `os.replace`!)
    with file_lock(final_path):
        try:
            ok, err_no = call_macos_renamex_excl(temp_path, final_path)
        except Exception as exc:
            temp_path.unlink(missing_ok=True)
            raise BridgeError(
                f"SAFETY BLOCK: Atomic non-overwriting publish unavailable at {final_path} ({exc}); "
                "refusing unsafe os.replace fallback."
            ) from exc

        if ok:
            return pub_id
        temp_path.unlink(missing_ok=True)
        if err_no == errno.EEXIST:
            raise BridgeError(
                f"Refusing to overwrite existing file at {final_path} (atomic RENAME_EXCL EEXIST)."
            )
        if err_no in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL, errno.EPERM, errno.EXDEV):
            raise BridgeError(
                f"SAFETY BLOCK: Target filesystem at {final_path} supports neither hardlinks (link) "
                f"nor atomic non-overwriting rename (RENAME_EXCL: {os.strerror(err_no)}); "
                "refusing unsafe fallback to prevent overwriting external files."
            )
        raise BridgeError(
            f"renamex_np failed publishing {temp_path} -> {final_path}: {os.strerror(err_no)}"
        )


def atomic_swap_replace_verified_victim(
    same_dir_replacement: Path,
    target_path: Path,
    expected_victim_sha256: str,
    expected_victim_id: tuple[int, int] | None = None,
    pre_swap_race_hook=None,
    post_swap_before_verify_hook=None,
) -> tuple[int, int]:
    """Losslessly replace `target_path` via kernel `RENAME_SWAP`, restoring any concurrent 3rd-party file!

    Closes R9 P1 #2 and R10 P1 #1:
    1. Verifies `target_path` currently matches `expected_victim_sha256` (and `expected_victim_id` if known).
    2. Atomically swaps `same_dir_replacement` <-> `target_path` via `renamex_np(..., RENAME_SWAP)` so NO
       inode is ever unlinked before verification (refusing unsafe `os.replace` if `RENAME_SWAP` is unsupported).
    3. Inspects the displaced file now sitting at `same_dir_replacement`: only unlinks `same_dir_replacement`
       AFTER confirming its `(st_dev, st_ino)` and SHA-256 match the expected victim; otherwise swaps the
       displaced 3rd-party file BACK onto `target_path` untouched and raises `EXPORT RECOVERY CONFLICT`!
    """
    rep_st = os.lstat(same_dir_replacement)
    new_id = (int(rep_st.st_dev), int(rep_st.st_ino))

    kind, live_sha, _, live_id = inspect_destination_state_unlocked(target_path)
    if kind == "non_regular":
        same_dir_replacement.unlink(missing_ok=True)
        raise BridgeError(f"Refusing overwrite at {target_path}: destination is a symlink or non-regular file.")
    if kind != "regular" or live_id is None:
        same_dir_replacement.unlink(missing_ok=True)
        raise BridgeError(f"Refusing overwrite at {target_path}: expected prior owned file is missing ({kind}).")
    if live_sha != expected_victim_sha256:
        same_dir_replacement.unlink(missing_ok=True)
        raise BridgeError(
            f"Refusing overwrite at {target_path}: existing file SHA-256 ({str(live_sha)[:12]}...) "
            f"does not match expected ({expected_victim_sha256[:12]}...)."
        )
    if expected_victim_id is not None and live_id != expected_victim_id:
        same_dir_replacement.unlink(missing_ok=True)
        raise BridgeError(
            f"Refusing overwrite at {target_path}: existing file inode {live_id} does not match "
            f"owned inode {expected_victim_id} (same-content external file protected)."
        )

    if pre_swap_race_hook is not None:
        pre_swap_race_hook()

    try:
        ok, err_no = call_macos_renamex_swap(same_dir_replacement, target_path)
    except BaseException as exc:
        # DO NOT unlink `same_dir_replacement` unconditionally here if `call_macos_renamex_swap` was wrapped
        # to raise after performing the swap! Only unlink if `same_dir_replacement` still holds `new_id`!
        try:
            st_check = os.lstat(same_dir_replacement)
            if (int(st_check.st_dev), int(st_check.st_ino)) == new_id:
                same_dir_replacement.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, Exception):
            raise BridgeError(
                f"SAFETY BLOCK: Atomic RENAME_SWAP unavailable at {target_path} ({exc}); "
                "refusing unsafe os.replace overwrite. Use --versioned instead."
            ) from exc
        raise

    if not ok:
        same_dir_replacement.unlink(missing_ok=True)
        if err_no == errno.ENOENT:
            raise BridgeError(
                f"EXPORT RECOVERY CONFLICT: Destination {target_path} was removed concurrently before swap."
            )
        if err_no in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL, errno.EPERM, errno.EXDEV):
            raise BridgeError(
                f"SAFETY BLOCK: Target filesystem at {target_path} does not support atomic RENAME_SWAP "
                f"({os.strerror(err_no)}); refusing unsafe os.replace overwrite. Use --versioned instead."
            )
        raise BridgeError(f"renamex_np(RENAME_SWAP) failed at {target_path}: {os.strerror(err_no)}")

    if post_swap_before_verify_hook is not None:
        post_swap_before_verify_hook()

    # Verify the displaced file now sitting at `same_dir_replacement` was indeed the expected victim!
    disp_kind, disp_sha, _, disp_id = inspect_destination_state_unlocked(same_dir_replacement)
    if disp_kind == "regular" and disp_id == live_id and disp_sha == expected_victim_sha256:
        same_dir_replacement.unlink(missing_ok=True)
        return new_id

    # Concurrent external file detected! Swap the displaced 3rd-party file BACK onto `target_path` untouched!
    restored_external = False
    try:
        cur_st = os.lstat(target_path)
        if (int(cur_st.st_dev), int(cur_st.st_ino)) == new_id:
            swap_back_ok, _ = call_macos_renamex_swap(same_dir_replacement, target_path)
            if swap_back_ok:
                restored_external = True
                same_dir_replacement.unlink(missing_ok=True)
    except OSError:
        restored_external = False

    if not restored_external:
        rescue_path = target_path.parent / f".c1_conflict_rescued_{target_path.name}.{uuid.uuid4().hex[:8]}"
        try:
            os.replace(same_dir_replacement, rescue_path)
        except OSError:
            pass
        raise BridgeError(
            f"EXPORT RECOVERY CONFLICT: Concurrent external change at {target_path} during swap; "
            f"displaced entry preserved at {rescue_path}."
        )

    raise BridgeError(
        f"EXPORT RECOVERY CONFLICT: Concurrent external replacement detected at {target_path} during "
        "overwrite window; external file was atomically restored untouched."
    )


def restore_file_preserving_backup(
    backup_path: Path,
    target_path: Path,
    expected_sha256: str,
    expected_current_new_sha256: str | None = None,
    expected_current_new_id: tuple[int, int] | None = None,
    pre_swap_race_hook=None,
) -> tuple[int, int]:
    """Losslessly restore `backup_path` onto `target_path` returning the restored `(st_dev, st_ino)`."""
    bak_sha, bak_size = compute_file_sha256(backup_path)
    if bak_sha != expected_sha256:
        raise BridgeError(
            f"Cannot restore backup {backup_path}: SHA-256 {bak_sha[:12]}... != expected {expected_sha256[:12]}..."
        )
    restore_tmp = target_path.parent / f".c1_restore_{target_path.name}.{uuid.uuid4().hex[:8]}"
    copy_file_verified_to_temp(
        backup_path,
        restore_tmp,
        expected_sha256=expected_sha256,
        expected_size=bak_size,
    )

    cur_kind, cur_sha, _, cur_id = inspect_destination_state_unlocked(target_path)
    if cur_kind == "missing":
        if pre_swap_race_hook is not None:
            pre_swap_race_hook()
        try:
            return publish_verified_temp_no_overwrite(restore_tmp, target_path)
        except BridgeError as exc:
            raise BridgeError(
                f"EXPORT RECOVERY CONFLICT: Destination {target_path} occupied concurrently during backup restore ({exc})"
            ) from exc

    if (
        cur_kind == "regular"
        and expected_current_new_sha256 is not None
        and cur_sha == expected_current_new_sha256
        and (expected_current_new_id is None or cur_id == expected_current_new_id)
    ):
        return atomic_swap_replace_verified_victim(
            same_dir_replacement=restore_tmp,
            target_path=target_path,
            expected_victim_sha256=expected_current_new_sha256,
            expected_victim_id=cur_id,
            pre_swap_race_hook=pre_swap_race_hook,
        )

    restore_tmp.unlink(missing_ok=True)
    raise BridgeError(
        f"EXPORT RECOVERY CONFLICT: Refusing backup restore onto unexpected destination state at {target_path} "
        f"(kind={cur_kind!r}, sha={str(cur_sha)[:12] if cur_sha else 'None'})."
    )


def build_serialize_adjustments_handler() -> str:
    """Generate AppleScript serializer covering 100% of ADJUSTMENT_PROP_MAP plus orientation/profile/curve."""
    lines = [
        "on serializeAdjustments(adjRef)",
        '    tell application "Capture One"',
        "        set kv to {}",
    ]
    for json_key, as_prop in ADJUSTMENT_PROP_MAP.items():
        lines.append(f'        set end of kv to "\\"{json_key}\\":" & my jsonNum({as_prop} of adjRef)')
    lines.extend([
        "        try",
        '            set end of kv to "\\"orientation\\":" & my jsonNum(orientation of adjRef)',
        "        end try",
        "        try",
        '            set end of kv to "\\"color_profile\\":" & my jsonStr(color profile of adjRef)',
        "        end try",
        "        try",
        '            set end of kv to "\\"film_curve\\":" & my jsonStr(film curve of adjRef)',
        "        end try",
        '        return "{" & my joinItems(kv, ",") & "}"',
        "    end tell",
        "end serializeAdjustments",
    ])
    return "\n".join(lines)


APPLESCRIPT_STATIC_HELPERS = r'''
on jsonEscape(txt)
    if txt is missing value then return ""
    set s to txt as text
    set AppleScript's text item delimiters to "\\"
    set parts to text items of s
    set AppleScript's text item delimiters to "\\\\"
    set s to parts as text
    set AppleScript's text item delimiters to "\""
    set parts to text items of s
    set AppleScript's text item delimiters to "\\\""
    set s to parts as text
    set AppleScript's text item delimiters to return
    set parts to text items of s
    set AppleScript's text item delimiters to "\\n"
    set s to parts as text
    set AppleScript's text item delimiters to linefeed
    set parts to text items of s
    set AppleScript's text item delimiters to "\\n"
    set s to parts as text
    set AppleScript's text item delimiters to tab
    set parts to text items of s
    set AppleScript's text item delimiters to "\\t"
    set s to parts as text
    set AppleScript's text item delimiters to ""
    return s
end jsonEscape

on jsonStr(val)
    if val is missing value then return "null"
    return "\"" & my jsonEscape(val as text) & "\""
end jsonStr

on jsonNum(val)
    if val is missing value then return "null"
    try
        set n to val as real
        set s to n as text
        set AppleScript's text item delimiters to ","
        set parts to text items of s
        set AppleScript's text item delimiters to "."
        set s to parts as text
        set AppleScript's text item delimiters to ""
        return s
    on error
        return "null"
    end try
end jsonNum

on jsonBool(val)
    if val is missing value then return "null"
    if val is true then return "true"
    return "false"
end jsonBool

on joinItems(itemList, sep)
    set AppleScript's text item delimiters to sep
    set outTxt to itemList as text
    set AppleScript's text item delimiters to ""
    return outTxt
end joinItems

on findVariantById(docRef, targetId)
    tell application "Capture One"
        try
            return (first variant of docRef whose id is targetId)
        end try
        repeat with colRef in (every collection of docRef)
            try
                return (first variant of colRef whose id is targetId)
            end try
        end repeat
    end tell
    error "Variant ID not found in document: " & targetId
end findVariantById

on serializeVariantFull(vRef)
    tell application "Capture One"
        set imgRef to parent image of vRef
        set dimVal to dimensions of imgRef
        set cropVal to crop of vRef
        set vAdj to adjustments of vRef
        set orientVal to 0
        try
            set orientVal to orientation of vAdj
        end try
        set layerJsonList to {}
        set lyrIdx to 0
        repeat with lyr in (every layer of vRef)
            set lyrIdx to lyrIdx + 1
            set lkv to {}
            set end of lkv to "\"index\":" & my jsonNum(lyrIdx)
            set end of lkv to "\"name\":" & my jsonStr(name of lyr)
            set end of lkv to "\"kind\":" & my jsonStr((kind of lyr) as text)
            set end of lkv to "\"enabled\":" & my jsonBool(enabled of lyr)
            set end of lkv to "\"opacity\":" & my jsonNum(opacity of lyr)
            set end of lkv to "\"adjustments\":" & my serializeAdjustments(adjustments of lyr)
            set end of layerJsonList to "{" & my joinItems(lkv, ",") & "}"
        end repeat

        set vkv to {}
        set end of vkv to "\"variant_id\":" & my jsonStr(id of vRef)
        set end of vkv to "\"variant_name\":" & my jsonStr(name of vRef)
        set end of vkv to "\"position\":" & my jsonNum(position of vRef)
        set end of vkv to "\"selected\":" & my jsonBool(selected of vRef)
        set end of vkv to "\"color_tag\":" & my jsonNum(color tag of vRef)
        set end of vkv to "\"rating\":" & my jsonNum(rating of vRef)
        set end of vkv to "\"orientation\":" & my jsonNum(orientVal)
        set end of vkv to "\"image_id\":" & my jsonStr(id of imgRef)
        set end of vkv to "\"image_name\":" & my jsonStr(name of imgRef)
        set end of vkv to "\"raw_path\":" & my jsonStr(path of imgRef)
        set end of vkv to "\"dimensions\":[" & my jsonNum(item 1 of dimVal) & "," & my jsonNum(item 2 of dimVal) & "]"
        set end of vkv to "\"crop\":[" & my jsonNum(item 1 of cropVal) & "," & my jsonNum(item 2 of cropVal) & "," & my jsonNum(item 3 of cropVal) & "," & my jsonNum(item 4 of cropVal) & "]"
        set end of vkv to "\"global_adjustments\":" & my serializeAdjustments(vAdj)
        set end of vkv to "\"layers\":[" & my joinItems(layerJsonList, ",") & "]"
        return "{" & my joinItems(vkv, ",") & "}"
    end tell
end serializeVariantFull
'''

APPLESCRIPT_HELPERS = APPLESCRIPT_STATIC_HELPERS + "\n" + build_serialize_adjustments_handler()


@contextmanager
def file_lock(target_path: Path):
    """Process- and thread-safe exclusive lock file for ledger, JSONL, and destination paths."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target_path.parent / f".{target_path.name}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def atomic_write_text(target_path: Path, content: str) -> None:
    """Write text to a unique temp file in the same directory and atomically replace target."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        suffix=".tmp",
        dir=str(target_path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def preserve_receipt_ownership_before_clear(entry: dict[str, Any]) -> None:
    """Migrate path + SHA-256 from legacy `export_receipt` into `owned_export_files` BEFORE clearing receipt."""
    if not isinstance(entry, dict):
        return
    receipt = entry.get("export_receipt")
    if not isinstance(receipt, dict):
        return
    raw_out = receipt.get("output_path") or entry.get("exported_jpg")
    sha = receipt.get("sha256")
    if not raw_out or not sha:
        return
    resolved_out = str(canonicalize_leaf_path(raw_out))
    owned_map: dict[str, dict[str, Any]] = dict(entry.get("owned_export_files") or {})
    if resolved_out not in owned_map:
        owned_map[resolved_out] = {
            "output_path": resolved_out,
            "sha256": str(sha),
            "byte_size": receipt.get("byte_size"),
            "file_id": receipt.get("file_id"),
            "updated_at": receipt.get("verified_at") or datetime.now(timezone.utc).isoformat(),
        }
        entry["owned_export_files"] = owned_map


def record_pending_conflict_unlocked(
    pending: dict[str, Any],
    reason: str,
    message: str,
    live_sha256: str | None = None,
    live_byte_size: int | None = None,
) -> bool:
    """Attach structured conflict metadata onto `pending` (returns True if `pending` changed)."""
    prev_conflict = pending.get("conflict")
    if (
        isinstance(prev_conflict, dict)
        and prev_conflict.get("reason") == reason
        and prev_conflict.get("live_sha256") == live_sha256
        and prev_conflict.get("message") == message
    ):
        return False
    pending["conflict"] = {
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "live_sha256": live_sha256,
        "live_byte_size": live_byte_size,
        "previous_sha256": pending.get("previous_sha256"),
        "new_sha256": pending.get("new_sha256"),
        "backup_path": pending.get("backup_path"),
        "message": message,
    }
    return True


def parse_candidate_file_ids(pending: dict[str, Any]) -> set[tuple[int, int]]:
    """Extract registered `(st_dev, st_ino)` pairs created by THIS export commit attempt."""
    out: set[tuple[int, int]] = set()
    raw_list = pending.get("candidate_file_ids")
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                try:
                    out.add((int(item[0]), int(item[1])))
                except (TypeError, ValueError):
                    pass
    return out


def parse_single_file_id(raw_id: Any) -> tuple[int, int] | None:
    if isinstance(raw_id, (list, tuple)) and len(raw_id) == 2:
        try:
            return (int(raw_id[0]), int(raw_id[1]))
        except (TypeError, ValueError):
            return None
    return None


def has_reliable_pending_ownership_proof(pending: dict[str, Any]) -> bool:
    """Return True iff `pending` carries reliable kernel `(st_dev, st_ino)` ownership proof.

    Closes R10 P1 #4:
    Legacy `pending_export_commit` records lacking a non-empty `candidate_file_ids` list (or an overwrite
    record lacking `previous_file_id`) MUST NOT default to trusted (`True`); they must be latched into
    conflict for manual resolution.
    """
    if "candidate_file_ids" not in pending or not isinstance(pending.get("candidate_file_ids"), list):
        return False
    cand_ids = parse_candidate_file_ids(pending)
    if not cand_ids:
        return False
    if pending.get("previous_sha256") is not None and parse_single_file_id(pending.get("previous_file_id")) is None:
        return False
    return True


def inspect_swap_temp_path_for_recovery_unlocked(
    temp_pub_path: Path | None,
    cand_ids: set[tuple[int, int]],
    prev_sha: Any,
    prev_id: tuple[int, int] | None,
) -> tuple[bool, list[Path], str | None]:
    """Verify `temp_publish_path` before cleaning it up or rolling forward after an interrupted swap!

    Closes R10 P1 #1:
    Returns `(temp_is_safe_to_clean, validated_cleanup_paths, displaced_conflict_reason_or_None)`.
    - If `temp_pub_path` does not exist (`missing`): safe (`True, [], None`).
    - If `temp_pub_path` is a regular file whose `(st_dev, st_ino)` is in `cand_ids`: it is our own
      un-swapped temporary copy, safe to clean up (`True, [temp_pub_path], None`).
    - If `temp_pub_path` is a regular file whose `(st_dev, st_ino)` equals `prev_id` AND whose SHA-256
      equals `prev_sha`: it is the file displaced by `RENAME_SWAP`, confirmed to belong to the original
      receipt! Safe to clean up (`True, [temp_pub_path], None`).
    - Otherwise: `temp_pub_path` holds a displaced 3rd-party external file (or unverified entry)!
      NEVER delete `temp_pub_path` (`False, [], "displaced_external_file_in_swap_temp"`).
    """
    if temp_pub_path is None:
        return (True, [], None)
    tp_kind, tp_sha, _, tp_id = inspect_destination_state_unlocked(temp_pub_path)
    if tp_kind == "missing":
        return (True, [], None)
    if tp_kind == "regular" and tp_id is not None:
        if tp_id in cand_ids:
            return (True, [temp_pub_path], None)
        if prev_sha is not None and prev_id is not None and tp_id == prev_id and tp_sha == str(prev_sha):
            return (True, [temp_pub_path], None)
    return (
        False,
        [],
        f"swap temp path {temp_pub_path} holds displaced external/unverified file "
        f"(kind={tp_kind!r}, inode={tp_id}, sha={str(tp_sha)[:12] if tp_sha else 'None'}, "
        f"expected_prev_inode={prev_id}, expected_prev_sha={str(prev_sha)[:12] if prev_sha else 'None'})",
    )


def rollback_aborted_commit_or_detect_conflict_unlocked(
    photo_id: str,
    entry: dict[str, Any],
    pending: dict[str, Any],
    target_path: Path,
    backup_path: Path | None,
    rollback_race_hook=None,
) -> tuple[bool, list[Path], str | None]:
    """Unified pre-commit rollback & conflict detector proving authorship via `(st_dev, st_ino)`.

    Closes R7 P1 #1, R8 P1 #1, R9 P1 #1, R9 P1 #2, R9 P2 #4, R10 P1 #1, and R10 P1 #4.
    """
    existing_conflict = pending.get("conflict")
    if isinstance(existing_conflict, dict):
        c_msg = str(
            existing_conflict.get("message")
            or f"photo {photo_id!r} at {target_path}: latched export conflict ({existing_conflict.get('reason')})"
        )
        return (False, [], c_msg)

    prev_sha = pending.get("previous_sha256")
    prev_id = parse_single_file_id(pending.get("previous_file_id"))
    new_sha = pending.get("new_sha256")
    new_size = pending.get("new_byte_size")
    cand_ids = parse_candidate_file_ids(pending)
    temp_pub_str = pending.get("temp_publish_path")
    temp_pub_path = canonicalize_leaf_path(temp_pub_str) if temp_pub_str else None
    cleanup_paths: list[Path] = []

    kind, live_sha, live_size, live_id = inspect_destination_state_unlocked(target_path)

    # Closes R10 P1 #4: Legacy `pending_export_commit` without reliable inode proof MUST latch conflict!
    if not has_reliable_pending_ownership_proof(pending):
        conflict_msg = (
            f"photo {photo_id!r} at {target_path}: legacy pending_export_commit lacks reliable inode "
            "ownership proof (candidate_file_ids / previous_file_id); preserving live files and latching "
            "conflict for manual resolution"
        )
        changed = record_pending_conflict_unlocked(
            pending,
            "legacy_unverified_pending_commit",
            conflict_msg,
            live_sha256=live_sha,
            live_byte_size=live_size,
        )
        return (changed, [], conflict_msg)

    # Closes R10 P1 #1: Verify `temp_publish_path` BEFORE any rollback or cleanup!
    tp_safe, tp_cleanups, tp_conflict_detail = inspect_swap_temp_path_for_recovery_unlocked(
        temp_pub_path=temp_pub_path,
        cand_ids=cand_ids,
        prev_sha=prev_sha,
        prev_id=prev_id,
    )
    if not tp_safe:
        # `temp_pub_path` holds a displaced 3rd-party external file from an interrupted `RENAME_SWAP`!
        # NEVER delete `temp_pub_path` while it holds the external file; if `target_path` currently holds
        # our unreceipted candidate (`live_id in cand_ids`), atomically swap the displaced external file
        # BACK onto `target_path`, verify `temp_pub_path` now holds our candidate inode, and ONLY unlink
        # our own candidate (do NOT hardlink the customer's restored file back to `temp_pub_path`!).
        if kind == "regular" and live_id is not None and live_id in cand_ids and temp_pub_path is not None:
            try:
                swap_ok, _ = call_macos_renamex_swap(temp_pub_path, target_path)
                if swap_ok:
                    try:
                        st_tp = os.lstat(temp_pub_path)
                        if stat.S_ISREG(st_tp.st_mode) and (int(st_tp.st_dev), int(st_tp.st_ino)) in cand_ids:
                            temp_pub_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            except Exception:
                pass
        k_now, s_now, sz_now, _ = inspect_destination_state_unlocked(target_path)
        entry["export_verified"] = False
        entry["export_receipt"] = None
        conflict_msg = (
            f"photo {photo_id!r} at {target_path}: interrupted RENAME_SWAP detected — {tp_conflict_detail}; "
            f"preserving displaced external file, backup ({backup_path}), and pending_export_commit"
        )
        changed = record_pending_conflict_unlocked(
            pending,
            "external_file_conflict",
            conflict_msg,
            live_sha256=s_now,
            live_byte_size=sz_now,
        )
        return (changed or True, [], conflict_msg)

    cleanup_paths.extend(tp_cleanups)

    if kind != "missing" and kind != "regular":
        conflict_msg = (
            f"photo {photo_id!r} at {target_path}: non-regular or concurrently changing entry ({kind!r}) "
            f"occupies destination; preserving destination, backup ({backup_path}), and pending_export_commit"
        )
        changed = record_pending_conflict_unlocked(pending, "non_regular_destination", conflict_msg)
        return (changed, [], conflict_msg)

    inode_matches_new = bool(cand_ids) and (live_id is not None) and (live_id in cand_ids)
    matches_new = (
        kind == "regular"
        and inode_matches_new
        and new_sha is not None
        and live_sha == str(new_sha)
        and (new_size is None or live_size == int(new_size))
    )
    inode_matches_prev = (prev_id is not None) and (live_id is not None) and (live_id == prev_id)
    matches_prev = (
        kind == "regular"
        and inode_matches_prev
        and prev_sha is not None
        and live_sha == str(prev_sha)
    )

    if kind == "regular" and not matches_new and not matches_prev:
        same_bytes_note = " (same bytes, foreign inode!)" if (new_sha and live_sha == str(new_sha)) else ""
        conflict_msg = (
            f"photo {photo_id!r} at {target_path}: external file occupies destination{same_bytes_note} "
            f"(live_sha={str(live_sha)[:12]}..., live_inode={live_id}, candidate_inodes={sorted(cand_ids)}); "
            f"preserving live file, backup ({backup_path}), and pending_export_commit"
        )
        changed = record_pending_conflict_unlocked(
            pending,
            "external_file_conflict",
            conflict_msg,
            live_sha256=live_sha,
            live_byte_size=live_size,
        )
        return (changed, [], conflict_msg)

    # If publish had ALREADY succeeded (`publish_succeeded=True`) yet destination is now `missing`,
    # latch `destination_removed_before_receipt` instead of silently clearing or overwriting!
    if kind == "missing" and pending.get("publish_succeeded") is True:
        conflict_msg = (
            f"photo {photo_id!r} at {target_path}: destination file disappeared after publish before "
            f"receipt commit; preserving empty destination, backup ({backup_path}), and pending_export_commit"
        )
        changed = record_pending_conflict_unlocked(
            pending,
            "destination_removed_before_receipt",
            conflict_msg,
        )
        return (changed, [], conflict_msg)

    # Destination is `missing` (pre-publish), `matches_prev`, or `matches_new` -> safe to align back to pre-commit!
    if prev_sha is None:
        if matches_new and live_id is not None:
            # Verify inode one final time right before unlinking our own unreceipted file
            try:
                st_now = os.lstat(target_path)
                if stat.S_ISREG(st_now.st_mode) and (int(st_now.st_dev), int(st_now.st_ino)) == live_id:
                    target_path.unlink()
                else:
                    conflict_msg = (
                        f"photo {photo_id!r} at {target_path}: destination changed before initial-export rollback; "
                        "preserving live destination and pending_export_commit"
                    )
                    changed = record_pending_conflict_unlocked(pending, "external_file_conflict", conflict_msg)
                    return (changed, [], conflict_msg)
            except FileNotFoundError:
                pass
        entry.pop("pending_export_commit", None)
        if backup_path:
            cleanup_paths.append(backup_path)
        return (True, cleanup_paths, None)

    if matches_prev:
        entry.pop("pending_export_commit", None)
        if backup_path:
            cleanup_paths.append(backup_path)
        return (True, cleanup_paths, None)

    # `prev_sha` is present and destination is `missing` or `matches_new`: losslessly restore from `backup_path`
    if backup_path and backup_path.is_file():
        try:
            restored_id = restore_file_preserving_backup(
                backup_path,
                target_path,
                str(prev_sha),
                expected_current_new_sha256=str(new_sha) if matches_new and new_sha else None,
                expected_current_new_id=live_id if matches_new else None,
                pre_swap_race_hook=rollback_race_hook,
            )
        except BridgeError as rb_exc:
            if "EXPORT RECOVERY CONFLICT" in str(rb_exc):
                k_after, s_after, sz_after, _ = inspect_destination_state_unlocked(target_path)
                changed = record_pending_conflict_unlocked(
                    pending,
                    "external_file_conflict" if k_after == "regular" else "non_regular_destination",
                    str(rb_exc),
                    live_sha256=s_after,
                    live_byte_size=sz_after,
                )
                return (changed, [], str(rb_exc))
            raise
        owned_map = dict(entry.get("owned_export_files") or {})
        if str(target_path) in owned_map and owned_map[str(target_path)].get("sha256") == str(prev_sha):
            owned_map[str(target_path)] = dict(owned_map[str(target_path)], file_id=list(restored_id))
            entry["owned_export_files"] = owned_map
        rec = entry.get("export_receipt")
        if (
            isinstance(rec, dict)
            and str(rec.get("output_path")) == str(target_path)
            and rec.get("sha256") == str(prev_sha)
        ):
            rec["file_id"] = list(restored_id)
        entry.pop("pending_export_commit", None)
        cleanup_paths.append(backup_path)
        return (True, cleanup_paths, None)

    reason = "missing_backup_for_rollback" if matches_new else "missing_destination_and_backup"
    conflict_msg = (
        f"photo {photo_id!r} at {target_path}: cannot restore previous_sha256 ({str(prev_sha)[:12]}...) "
        f"because backup {backup_path} is missing; preserving destination and pending_export_commit"
    )
    changed = record_pending_conflict_unlocked(
        pending,
        reason,
        conflict_msg,
        live_sha256=live_sha,
        live_byte_size=live_size,
    )
    return (changed, [], conflict_msg)


def reconcile_pending_export_commits_unlocked(
    data: dict[str, Any],
) -> tuple[bool, list[Path], list[str], list[dict[str, Any]]]:
    """Reconcile interrupted two-phase export commits (`pending_export_commit`) on ledger load."""
    dirty = False
    cleanup_paths: list[Path] = []
    conflicts: list[str] = []
    rolled_forward_items: list[dict[str, Any]] = []
    photos = data.get("photos") or {}

    for photo_key, entry in photos.items():
        if not isinstance(entry, dict):
            continue
        pending = entry.get("pending_export_commit")
        if not isinstance(pending, dict):
            continue

        photo_id = str(entry.get("photo_id") or photo_key)
        target_str = str(pending.get("target_path") or "")
        target_path = canonicalize_leaf_path(target_str) if target_str else None
        backup_str = pending.get("backup_path")
        backup_path = Path(str(backup_str)) if backup_str else None

        # Closes R9 P2 #4: Once a conflict has been latched in `pending["conflict"]`, NEVER auto-clear,
        # NEVER overwrite/restore destination, and NEVER delete `backup_path` on subsequent reloads!
        existing_conflict = pending.get("conflict")
        if isinstance(existing_conflict, dict):
            c_msg = str(
                existing_conflict.get("message")
                or f"photo {photo_id!r} at {target_path}: latched export conflict ({existing_conflict.get('reason')})"
            )
            conflicts.append(c_msg)
            continue

        temp_pub_str = pending.get("temp_publish_path")
        temp_pub_path = canonicalize_leaf_path(temp_pub_str) if temp_pub_str else None

        prev_sha = pending.get("previous_sha256")
        prev_id = parse_single_file_id(pending.get("previous_file_id"))
        new_sha = pending.get("new_sha256")
        new_size = pending.get("new_byte_size")
        pending_seq = pending.get("adjustment_seq")
        current_seq = int(entry.get("adjustment_seq") or 0)
        healthy = len(find_unresolved_history_issues(entry, include_pending_export=False)) == 0
        cand_ids = parse_candidate_file_ids(pending)

        if target_path is None:
            entry.pop("pending_export_commit", None)
            if backup_path:
                cleanup_paths.append(backup_path)
            dirty = True
            continue

        with file_lock(target_path):
            # Closes R10 P1 #4: Legacy `pending_export_commit` lacking reliable inode ownership proof
            # MUST transition into latched conflict state (NEVER roll forward or adopt external files)!
            if not has_reliable_pending_ownership_proof(pending):
                _, l_sha, l_sz, _ = inspect_destination_state_unlocked(target_path)
                conflict_msg = (
                    f"photo {photo_id!r} at {target_path}: legacy pending_export_commit lacks reliable inode "
                    "ownership proof (candidate_file_ids / previous_file_id); preserving live files and "
                    "latching conflict for manual resolution"
                )
                if record_pending_conflict_unlocked(
                    pending,
                    "legacy_unverified_pending_commit",
                    conflict_msg,
                    live_sha256=l_sha,
                    live_byte_size=l_sz,
                ):
                    dirty = True
                conflicts.append(conflict_msg)
                continue

            # Closes R10 P1 #1: Jointly verify `target_path` AND `temp_publish_path`!
            # Only clean up `temp_publish_path` or allow roll-forward after confirming any displaced file at
            # `temp_publish_path` belonged to the original receipt (`prev_id` & `prev_sha`) or our candidate!
            tp_safe, tp_cleanups, _ = inspect_swap_temp_path_for_recovery_unlocked(
                temp_pub_path=temp_pub_path,
                cand_ids=cand_ids,
                prev_sha=prev_sha,
                prev_id=prev_id,
            )

            kind, live_sha, live_size, live_id = inspect_destination_state_unlocked(target_path)
            inode_authored_by_us = bool(cand_ids) and (live_id is not None) and (live_id in cand_ids)

            # Roll forward ONLY if `tp_safe` is True AND `target_path` is a regular file whose `(st_dev, st_ino)`
            # is proven to be created by THIS export (`inode_authored_by_us`), matching `(new_sha, new_size)`!
            if (
                tp_safe
                and kind == "regular"
                and inode_authored_by_us
                and new_sha
                and live_sha == str(new_sha)
                and (new_size is None or live_size == int(new_size))
                and pending_seq is not None
                and int(pending_seq) == current_seq
                and healthy
            ):
                prev_owned_snap = json.loads(json.dumps(entry.get("owned_export_files") or {}))
                prev_exported_jpg = entry.get("exported_jpg")
                prev_raw_dims = entry.get("raw_dimensions")
                prev_orient = entry.get("orientation", 0)
                pending_snapshot = json.loads(json.dumps(pending))

                unregister_from_preview_manifest(target_path)
                owned_map: dict[str, dict[str, Any]] = dict(entry.get("owned_export_files") or {})
                owned_map[str(target_path)] = {
                    "output_path": str(target_path),
                    "sha256": str(new_sha),
                    "byte_size": live_size,
                    "file_id": list(live_id) if live_id else None,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                entry["owned_export_files"] = owned_map
                entry["exported_jpg"] = str(target_path)
                export_meta = pending.get("export_meta") or {}
                entry["raw_dimensions"] = export_meta.get("raw_dimensions", entry.get("raw_dimensions"))
                entry["orientation"] = export_meta.get("orientation", entry.get("orientation", 0))
                entry["export_verified"] = True
                entry["export_receipt"] = {
                    "adjustment_seq": current_seq,
                    "output_path": str(target_path),
                    "sha256": str(new_sha),
                    "byte_size": live_size,
                    "file_id": list(live_id) if live_id else None,
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                    "export_meta": export_meta,
                    "sips_info": pending.get("sips_info") or {},
                }
                entry.pop("pending_export_commit", None)
                rolled_forward_items.append(
                    {
                        "photo_key": photo_key,
                        "photo_id": photo_id,
                        "target_path": target_path,
                        "expected_sha256": str(new_sha),
                        "expected_size": int(live_size) if live_size is not None else None,
                        "expected_id": live_id,
                        "prev_owned_snapshot": prev_owned_snap,
                        "prev_exported_jpg": prev_exported_jpg,
                        "prev_raw_dims": prev_raw_dims,
                        "prev_orient": prev_orient,
                        "pending_snapshot": pending_snapshot,
                        "cleanup_paths": list(tp_cleanups) + ([backup_path] if backup_path else []),
                    }
                )
                dirty = True
                continue

            sub_dirty, sub_cleanups, conflict_msg = rollback_aborted_commit_or_detect_conflict_unlocked(
                photo_id=photo_id,
                entry=entry,
                pending=pending,
                target_path=target_path,
                backup_path=backup_path,
            )
            if sub_dirty:
                dirty = True
            cleanup_paths.extend(sub_cleanups)
            if conflict_msg:
                conflicts.append(conflict_msg)

    return dirty, cleanup_paths, conflicts, rolled_forward_items


def save_batch_ledger(ledger_path: Path, data: dict[str, Any]) -> None:
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_text(ledger_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_batch_ledger(
    ledger_path: Path,
    reconcile_writes: bool = True,
    allow_conflicts: bool = False,
) -> dict[str, Any]:
    """Load batch state ledger, reconciling interrupted export commits and reporting external-file conflicts."""
    if not ledger_path.is_file():
        raise BridgeError(f"Batch state ledger not found: {ledger_path}")
    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("photos"), dict):
        raise BridgeError(f"Corrupted batch state ledger: {ledger_path}")
    for p_entry in data["photos"].values():
        if isinstance(p_entry, dict):
            preserve_receipt_ownership_before_clear(p_entry)
    dirty, cleanup_paths, conflicts, rolled_forward_items = reconcile_pending_export_commits_unlocked(data)
    if dirty and reconcile_writes:
        save_batch_ledger(ledger_path, data)
        # Closes R11 P1 #3: Verify full content SHA-256 + inode + size continuity AFTER saving
        # rolled-forward receipts! Catch equal-length in-place overwrites during roll-forward save.
        revoked_any = False
        safe_rf_cleanups: list[Path] = []
        for rf in rolled_forward_items:
            t_path: Path = rf["target_path"]
            with file_lock(t_path):
                after_kind, after_sha, after_size, after_id = inspect_destination_state_unlocked(t_path)
                still_bound = (
                    after_kind == "regular"
                    and after_id == rf["expected_id"]
                    and (rf["expected_size"] is None or after_size == rf["expected_size"])
                    and after_sha == rf["expected_sha256"]
                )
                if still_bound:
                    safe_rf_cleanups.extend(rf["cleanup_paths"])
                else:
                    entry = (data.get("photos") or {})[rf["photo_key"]]
                    entry["owned_export_files"] = rf["prev_owned_snapshot"]
                    entry["exported_jpg"] = rf["prev_exported_jpg"]
                    entry["raw_dimensions"] = rf["prev_raw_dims"]
                    entry["orientation"] = rf["prev_orient"]
                    entry["export_verified"] = False
                    entry["export_receipt"] = None
                    pending_rev = rf["pending_snapshot"]
                    c_reason = (
                        "non_regular_destination"
                        if after_kind == "non_regular"
                        else (
                            "destination_removed_before_receipt"
                            if after_kind in ("missing", "removed_during_read")
                            else "external_file_conflict"
                        )
                    )
                    c_msg = (
                        f"photo {rf['photo_id']!r} at {t_path}: destination content or identity changed while "
                        f"saving rolled-forward receipt (kind={after_kind!r}, live_id={after_id}, "
                        f"expected_id={rf['expected_id']}, live_sha={str(after_sha)[:12] if after_sha else 'None'}, "
                        f"expected_sha={rf['expected_sha256'][:12]}...); revoking receipt and latching conflict"
                    )
                    record_pending_conflict_unlocked(
                        pending_rev,
                        c_reason,
                        c_msg,
                        live_sha256=after_sha,
                        live_byte_size=after_size,
                    )
                    entry["pending_export_commit"] = pending_rev
                    conflicts.append(c_msg)
                    revoked_any = True
        if revoked_any:
            try:
                save_batch_ledger(ledger_path, data)
            except Exception as rev_exc:
                try:
                    data["updated_at"] = datetime.now(timezone.utc).isoformat()
                    atomic_write_text(ledger_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                except Exception as fallback_exc:
                    raise BridgeError(
                        f"CRITICAL EXPORT RECOVERY CONFLICT: Failed to persist revoked rolled-forward receipt "
                        f"to {ledger_path} ({rev_exc}; fallback: {fallback_exc})"
                    ) from fallback_exc
        for cp in cleanup_paths + safe_rf_cleanups:
            cp.unlink(missing_ok=True)
    elif not dirty and reconcile_writes:
        for cp in cleanup_paths:
            cp.unlink(missing_ok=True)
    if conflicts and not allow_conflicts:
        raise BridgeError("EXPORT RECOVERY CONFLICT: " + "; ".join(conflicts))
    return data


def resolve_export_conflict(
    ledger_path: Path,
    photo_id: str,
    resolution: str,
) -> dict[str, Any]:
    """Explicitly clear a latched `pending_export_commit` conflict after operator inspection.

    Closes R9 P2 #4:
    `resolution` must be one of:
    - `"keep-external"`: Leave `target_path` untouched as-is; revoke stale ownership of `target_path`
      if it no longer matches prior owned file; clear `pending_export_commit` and remove `backup_path`.
    - `"restore-backup"`: Requires `target_path` to be vacant (`missing`) or already holding `previous_sha256`
      (NEVER overwrites an unknown file still occupying `target_path` — operator must move it aside first);
      restores `backup_path` via non-overwriting publish (if `previous_sha256` existed), clears
      `pending_export_commit`, and removes `backup_path` after saving ledger.
    """
    if resolution not in ("keep-external", "restore-backup"):
        raise ValueError(f"Unsupported conflict resolution {resolution!r}; use 'keep-external' or 'restore-backup'")

    with file_lock(ledger_path):
        ledger = load_batch_ledger(ledger_path, allow_conflicts=True)
        photos = ledger.get("photos") or {}
        if photo_id not in photos:
            raise BridgeError(f"photo_id {photo_id!r} not found in batch ledger {ledger_path}")
        entry = photos[photo_id]
        pending = entry.get("pending_export_commit")
        if not isinstance(pending, dict):
            raise BridgeError(f"No pending_export_commit to resolve for {photo_id!r}")

        target_str = str(pending.get("target_path") or "")
        target_path = canonicalize_leaf_path(target_str) if target_str else None
        backup_str = pending.get("backup_path")
        backup_path = Path(str(backup_str)) if backup_str else None
        temp_pub_str = pending.get("temp_publish_path")
        temp_pub_path = canonicalize_leaf_path(temp_pub_str) if temp_pub_str else None
        prev_sha = pending.get("previous_sha256")
        owned_map: dict[str, dict[str, Any]] = dict(entry.get("owned_export_files") or {})
        expected_prev_id = (
            parse_single_file_id(pending.get("previous_file_id"))
            or parse_single_file_id((owned_map.get(str(target_path)) or {}).get("file_id"))
        )

        if target_path is not None:
            with file_lock(target_path):
                kind, live_sha, _, live_id = inspect_destination_state_unlocked(target_path)
                is_verified_prior_owned = (
                    kind == "regular"
                    and prev_sha is not None
                    and live_sha == str(prev_sha)
                    and expected_prev_id is not None
                    and live_id is not None
                    and live_id == expected_prev_id
                )
                if resolution == "keep-external":
                    if not is_verified_prior_owned:
                        owned_map.pop(str(target_path), None)
                        entry["owned_export_files"] = owned_map
                        rec = entry.get("export_receipt")
                        if isinstance(rec, dict) and str(rec.get("output_path")) == str(target_path):
                            entry["export_verified"] = False
                            entry["export_receipt"] = None
                    entry.pop("pending_export_commit", None)
                    save_batch_ledger(ledger_path, ledger)
                else:
                    # Closes R10 P1 #3: When `target_path` is occupied (`kind != "missing"`), require BOTH
                    # `live_sha == prev_sha` AND `live_id == expected_prev_id`! A same-byte external file
                    # with a different inode MUST keep the conflict latched and NEVER be adopted as owned!
                    if kind != "missing" and not is_verified_prior_owned:
                        raise BridgeError(
                            f"Refusing 'restore-backup' while {target_path} is occupied by an external or "
                            f"unverified file (kind={kind!r}, live_inode={live_id}, expected_owned_inode={expected_prev_id}, "
                            f"sha={str(live_sha)[:12] if live_sha else 'None'}); move external file aside first."
                        )
                    if prev_sha:
                        if kind == "missing":
                            if not (backup_path and backup_path.is_file()):
                                raise BridgeError(f"Cannot restore-backup: backup missing at {backup_path}")
                            restored_id = restore_file_preserving_backup(backup_path, target_path, str(prev_sha))
                        else:
                            assert live_id is not None
                            restored_id = live_id
                        rest_kind, rest_sha, rest_sz, rest_id = inspect_destination_state_unlocked(target_path)
                        if rest_kind != "regular" or rest_sha != str(prev_sha) or rest_id != restored_id:
                            raise BridgeError(f"Backup restore verification failed at {target_path}")
                        if str(target_path) in owned_map and owned_map[str(target_path)].get("sha256") == str(prev_sha):
                            owned_map[str(target_path)]["file_id"] = list(restored_id)
                            owned_map[str(target_path)]["byte_size"] = rest_sz
                            entry["owned_export_files"] = owned_map
                        rec = entry.get("export_receipt")
                        if (
                            isinstance(rec, dict)
                            and str(rec.get("output_path")) == str(target_path)
                            and rec.get("sha256") == str(prev_sha)
                        ):
                            rec["file_id"] = list(restored_id)
                    entry.pop("pending_export_commit", None)
                    save_batch_ledger(ledger_path, ledger)

        preserved_temp_external_path: str | None = None
        if temp_pub_path:
            cand_ids = parse_candidate_file_ids(pending)
            tp_safe, tp_cleanups, _ = inspect_swap_temp_path_for_recovery_unlocked(
                temp_pub_path=temp_pub_path,
                cand_ids=cand_ids,
                prev_sha=prev_sha,
                prev_id=expected_prev_id,
            )
            if tp_safe:
                for cp in tp_cleanups:
                    cp.unlink(missing_ok=True)
            else:
                # Closes R11 P1 #2: NEVER delete an unverified/displaced external file sitting at
                # `temp_pub_path`! Preserve it untouched (rescue to a visible path in the same dir
                # if it is not the same inode as `target_path`).
                tp_id_now = None
                tgt_id_now = None
                try:
                    st_tp = os.lstat(temp_pub_path)
                    if stat.S_ISREG(st_tp.st_mode):
                        tp_id_now = (int(st_tp.st_dev), int(st_tp.st_ino))
                except OSError:
                    pass
                if target_path is not None:
                    try:
                        st_tg = os.lstat(target_path)
                        if stat.S_ISREG(st_tg.st_mode):
                            tgt_id_now = (int(st_tg.st_dev), int(st_tg.st_ino))
                    except OSError:
                        pass
                if tp_id_now is not None and tp_id_now == tgt_id_now:
                    temp_pub_path.unlink(missing_ok=True)
                else:
                    rescue_name = (
                        f".c1_conflict_rescued_{target_path.name}.{uuid.uuid4().hex[:8]}"
                        if target_path is not None
                        else f".c1_conflict_rescued_{temp_pub_path.name}.{uuid.uuid4().hex[:8]}"
                    )
                    rescue_path = temp_pub_path.parent / rescue_name
                    try:
                        ok_excl, _ = call_macos_renamex_excl(temp_pub_path, rescue_path)
                        if ok_excl:
                            preserved_temp_external_path = str(rescue_path)
                        else:
                            preserved_temp_external_path = str(temp_pub_path)
                    except Exception:
                        preserved_temp_external_path = str(temp_pub_path)
        if backup_path:
            backup_path.unlink(missing_ok=True)

        return {
            "photo_id": photo_id,
            "resolution": resolution,
            "target_path": str(target_path) if target_path else None,
            "preserved_temp_external_path": preserved_temp_external_path,
            "resolved": True,
        }


def resolve_authorized_clone_entry(ledger: dict[str, Any], variant_id: str) -> dict[str, Any]:
    """Ensure target variant_id is a registered cloned_variant_id and NEVER a source_variant_id."""
    if not variant_id:
        raise BridgeError("Target variant_id must not be empty.")
    photos = ledger.get("photos") or {}
    source_ids = {
        str(entry.get("source_variant_id"))
        for entry in photos.values()
        if entry.get("source_variant_id")
    }
    if variant_id in source_ids:
        raise BridgeError(
            f"SAFETY BLOCK: Variant {variant_id!r} is a registered original source variant! "
            "Refusing to modify source variant; operate on cloned_variant_id only."
        )
    for entry in photos.values():
        if str(entry.get("cloned_variant_id") or "") == variant_id:
            return entry
    raise BridgeError(
        f"SAFETY BLOCK: Variant {variant_id!r} is not registered as a cloned_variant_id in batch ledger."
    )


def find_unresolved_history_issues(entry: dict[str, Any], include_pending_export: bool = True) -> list[str]:
    """Return descriptions of any unresolved failed, interrupted, or conflicted rounds."""
    issues: list[str] = []
    for idx, wal in enumerate(entry.get("history") or [], start=1):
        status = str(wal.get("status") or "")
        verified = wal.get("verified") is True
        if status != "applied" or not verified:
            issues.append(f"round #{idx} status={status!r} verified={verified}")
    if include_pending_export and isinstance(entry.get("pending_export_commit"), dict):
        issues.append("unresolved pending_export_commit recovery")
    return issues


def ensure_variant_healthy_for_handoff(entry: dict[str, Any], action_desc: str) -> None:
    """Block new `apply`, formal `handoff` export, or `passed` handoff if any round/rollback is unresolved."""
    issues = find_unresolved_history_issues(entry)
    if issues:
        photo_id = entry.get("photo_id")
        raise BridgeError(
            f"SAFETY BLOCK: Cannot {action_desc} for {photo_id!r} because variant has unresolved "
            f"failed/interrupted adjustment or rollback round(s): {', '.join(issues)}. "
            "Complete a verified `rollback` first."
        )


def invalidate_photo_in_linked_handoff_files(
    ledger: dict[str, Any],
    photo_id: str,
    new_adjustment_seq: int,
    reason: str,
) -> None:
    """Immediately demote any prior `passed` row for `photo_id` in all linked `handoff.jsonl` files."""
    handoff_files = ledger.get("handoff_files") or []
    for hf_str in handoff_files:
        hf_path = Path(hf_str)
        if not hf_path.is_file():
            continue
        with file_lock(hf_path):
            if not hf_path.is_file():
                continue
            rows: list[dict[str, Any]] = []
            changed = False
            for line in hf_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if str(row.get("photo_id")) == photo_id and row.get("status") == "passed":
                    row = dict(row)
                    row["previous_status"] = "passed"
                    row["status"] = "stale"
                    row["handoff_valid"] = False
                    row["invalidated_reason"] = reason
                    row["invalidated_at_adjustment_seq"] = new_adjustment_seq
                    changed = True
                rows.append(row)
            if changed:
                atomic_write_text(
                    hf_path,
                    "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                )


def run_applescript(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
    """Execute an AppleScript snippet with `osascript` and return stripped stdout."""
    if sys.platform != "darwin":
        raise BridgeError("Capture One AppleScript bridge requires macOS (darwin).")
    cmd = ["/usr/bin/osascript", "-"]
    if args:
        cmd.extend(args)
    full_script = script_body + "\n" + APPLESCRIPT_HELPERS
    res = subprocess.run(
        cmd,
        input=full_script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    if res.returncode != 0:
        err = res.stderr.strip() or res.stdout.strip() or f"osascript exited with {res.returncode}"
        raise BridgeError(err)
    return res.stdout.strip()


def format_as_number(value: Any) -> str:
    if not is_recoverable_number(value):
        raise ValueError(f"Expected finite numeric adjustment value, got {value!r}")
    return f"{float(value):.6f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


def get_field_tolerance(field_key: str, tolerance_scale: float = 1.0) -> float:
    scale = validate_tolerance_scale(tolerance_scale)
    base = FIELD_TOLERANCES.get(field_key, DEFAULT_SLIDER_TOLERANCE)
    return base * scale


def validate_adjustment_dict(
    adj: dict[str, Any],
    scope: str,
    allow_layer_wb: bool = False,
) -> list[tuple[str, str, float]]:
    """Validate adjustment keys/values and return (json_key, as_prop_name, float_val) tuples."""
    validated: list[tuple[str, str, float]] = []
    for key, val in adj.items():
        if val is None:
            continue
        if key not in ADJUSTMENT_PROP_MAP:
            allowed = ", ".join(sorted(ADJUSTMENT_PROP_MAP))
            raise ValueError(f"Unsupported adjustment key {key!r} in {scope}. Allowed: {allowed}")
        if scope.startswith("masks.") and key in ("temperature", "tint") and not allow_layer_wb:
            raise ValueError(
                f"Layer white balance ({scope}.{key}) is disabled by default until real-device unit "
                "verification; adjust global temperature/tint or pass --allow-layer-wb for testing."
            )
        if not is_recoverable_number(val):
            raise ValueError(f"Adjustment {scope}.{key} must be a finite number, got {val!r}")
        validated.append((key, ADJUSTMENT_PROP_MAP[key], float(val)))
    return validated


def reject_unverifiable_mask_ops(masks_spec: dict[str, Any]) -> None:
    """Reject `feather` and `refine` BEFORE write on ALL layers (new and existing).

    Closes R7 P2 #5:
    Capture One 16.9 AppleScript `serializeVariantFull` exposes no read-back property on `layer` or
    `adjustment settings` for `feather mask` or `refine mask`, so neither result verification nor
    existing-layer raster snapshotting can be performed deterministically.
    """
    if not isinstance(masks_spec, dict):
        return
    for mask_key, mask_cfg in masks_spec.items():
        if not isinstance(mask_cfg, dict):
            continue
        for unverified_op in ("feather", "refine"):
            if mask_cfg.get(unverified_op) is not None:
                raise BridgeError(
                    f"SAFETY BLOCK: Mask {unverified_op!r} (masks.{mask_key}.{unverified_op}) cannot be "
                    "independently read back or verified via Capture One AppleScript; aborting before write."
                )


def verify_recoverable_originals_before_write(
    spec: dict[str, Any],
    existing_managed_layers: dict[str, str],
    before_state: dict[str, Any],
) -> None:
    """Ensure every global or mask operation is verifiable and has a finite, recoverable original state.

    Closes R4 P2 #4, R6 P2 #4, and R7 P2 #5:
    - Rejects unverifiable mask raster operations (`feather` / `refine`) on BOTH new and existing layers.
    - Blocks overwriting existing-layer or global fields whose original read-back is missing/null.
    """
    reject_unverifiable_mask_ops(spec.get("masks") or {})

    global_spec = spec.get("global") or {}
    global_before = before_state.get("global_adjustments") or {}
    for key, target_val in global_spec.items():
        if target_val is None:
            continue
        orig_val = global_before.get(key) if isinstance(global_before, dict) else None
        if not is_recoverable_number(orig_val):
            raise BridgeError(
                f"SAFETY BLOCK: Cannot write global.{key} because original read-back value is "
                f"missing or null ({orig_val!r}), making rollback impossible; aborting before write."
            )

    layers_by_name = {
        str(lyr.get("name")): lyr
        for lyr in (before_state.get("layers") or [])
        if isinstance(lyr, dict) and lyr.get("name") is not None
    }
    masks_spec = spec.get("masks") or {}
    for mask_key, mask_cfg in masks_spec.items():
        if not isinstance(mask_cfg, dict):
            continue
        if mask_key not in existing_managed_layers:
            continue  # Brand-new layer will be `delete`d completely on rollback!
        layer_name = existing_managed_layers[mask_key]

        live_lyr = layers_by_name.get(layer_name)
        if not isinstance(live_lyr, dict):
            raise BridgeError(
                f"SAFETY BLOCK: Managed layer {layer_name!r} missing before write; aborting."
            )
        live_adj = live_lyr.get("adjustments") if isinstance(live_lyr.get("adjustments"), dict) else {}
        for key, target_val in mask_cfg.items():
            if target_val is None or key == "enabled":
                continue
            if key == "opacity":
                if not is_recoverable_number(live_lyr.get("opacity")):
                    raise BridgeError(
                        f"SAFETY BLOCK: Cannot write masks.{mask_key}.opacity on {layer_name!r} "
                        "because original opacity read-back is missing/null; aborting before write."
                    )
                continue
            orig_val = live_adj.get(key)
            if not is_recoverable_number(orig_val):
                raise BridgeError(
                    f"SAFETY BLOCK: Cannot write masks.{mask_key}.{key} on existing layer {layer_name!r} "
                    f"because original read-back value is missing or null ({orig_val!r}), "
                    "making rollback impossible; aborting before write."
                )


def verify_layer_matches_signature(
    live_lyr: dict[str, Any],
    rec: dict[str, Any],
    layer_name: str,
    tolerance_scale: float = 1.0,
    lattice_target_rec: dict[str, Any] | None = None,
    expected_index_override: int | None = None,
) -> None:
    """Verify `live_lyr` matches `rec` (with exact deleted-predecessor index shift & per-slider lattice)."""
    scale = validate_tolerance_scale(tolerance_scale)

    exp_kind = str(rec.get("kind") or "")
    act_kind = str(live_lyr.get("kind") or "")
    if exp_kind and act_kind != exp_kind:
        raise BridgeError(
            f"SAFETY BLOCK: Layer {layer_name!r} kind changed ({exp_kind!r} -> {act_kind!r}); "
            "managed layer identity cannot be confirmed, aborting before write."
        )

    if live_lyr.get("index") is not None:
        if expected_index_override is not None:
            if int(live_lyr["index"]) != int(expected_index_override):
                raise BridgeError(
                    f"SAFETY BLOCK: Layer {layer_name!r} stack index changed "
                    f"(expected {expected_index_override}, got {live_lyr['index']}); aborting before write."
                )
        elif rec.get("index") is not None:
            allowed_indices = {int(rec["index"])}
            if lattice_target_rec is not None and lattice_target_rec.get("index") is not None:
                allowed_indices.add(int(lattice_target_rec["index"]))
            if int(live_lyr["index"]) not in allowed_indices:
                raise BridgeError(
                    f"SAFETY BLOCK: Layer {layer_name!r} stack index changed ({rec['index']} -> {live_lyr['index']}); "
                    "aborting before write."
                )

    if "enabled" in rec:
        allowed_enabled = {bool(rec.get("enabled"))}
        if lattice_target_rec is not None and "enabled" in lattice_target_rec:
            allowed_enabled.add(bool(lattice_target_rec.get("enabled")))
        if bool(live_lyr.get("enabled")) not in allowed_enabled:
            raise BridgeError(
                f"SAFETY BLOCK: Layer {layer_name!r} enabled state drifted "
                f"({rec.get('enabled')} -> {live_lyr.get('enabled')}); aborting before write."
            )

    if rec.get("opacity") is not None:
        act_op = live_lyr.get("opacity")
        op_candidates = [rec["opacity"]]
        if lattice_target_rec is not None and lattice_target_rec.get("opacity") is not None:
            op_candidates.append(lattice_target_rec["opacity"])
        if not any(within_tolerance(act_op, cand, 1.0) for cand in op_candidates):
            raise BridgeError(
                f"SAFETY BLOCK: Layer {layer_name!r} opacity drifted "
                f"(allowed {op_candidates}, got {act_op}); aborting before write."
            )

    rec_adj = rec.get("adjustments") or {}
    alt_adj = (lattice_target_rec.get("adjustments") or {}) if isinstance(lattice_target_rec, dict) else {}
    live_adj = live_lyr.get("adjustments") or {}
    for k in ADJUSTMENT_PROP_MAP:
        candidates: list[Any] = []
        if is_recoverable_number(rec_adj.get(k)):
            candidates.append(rec_adj[k])
        if lattice_target_rec is not None and is_recoverable_number(alt_adj.get(k)):
            candidates.append(alt_adj[k])
        if not candidates:
            continue
        av = live_adj.get(k)
        tol = get_field_tolerance(k, scale)
        if not any(within_tolerance(av, cand, tol) for cand in candidates):
            raise BridgeError(
                f"SAFETY BLOCK: Layer {layer_name!r} parameter {k!r} drifted "
                f"(allowed {candidates}, got {av}); aborting before write."
            )


def verify_managed_layers_before_write(
    existing_managed_layers: dict[str, str],
    managed_layer_records: dict[str, dict[str, Any]],
    inherited_layers: list[dict[str, Any]],
    current_layers: list[dict[str, Any]],
    tolerance_scale: float = 1.0,
) -> None:
    """Verify every previously created managed layer is uniquely identifiable and unaltered BEFORE `apply`."""
    scale = validate_tolerance_scale(tolerance_scale)
    inh_ok, inh_violations = verify_inherited_layers_preserved(inherited_layers, {"layers": current_layers})
    if not inh_ok:
        raise BridgeError(
            f"SAFETY BLOCK: Inherited client layer(s) altered before write: {'; '.join(inh_violations)}"
        )

    non_bg_current = [
        lyr for lyr in (current_layers or [])
        if isinstance(lyr, dict) and str(lyr.get("kind") or "") != "background"
    ]

    for mask_key, layer_name in existing_managed_layers.items():
        matches = [lyr for lyr in non_bg_current if str(lyr.get("name") or "") == layer_name]
        if len(matches) == 0:
            raise BridgeError(
                f"SAFETY BLOCK: Managed layer {layer_name!r} (for {mask_key!r}) is missing or was renamed; "
                "aborting before write to avoid touching wrong layer."
            )
        if len(matches) > 1:
            raise BridgeError(
                f"SAFETY BLOCK: Found {len(matches)} duplicate layers named {layer_name!r} (for {mask_key!r}); "
                "cannot uniquely identify target managed layer, aborting before write."
            )
        rec = (managed_layer_records or {}).get(mask_key)
        if isinstance(rec, dict):
            verify_layer_matches_signature(matches[0], rec, layer_name, tolerance_scale=scale)

    if existing_managed_layers:
        expected_non_bg_count = sum(
            1 for lyr in (inherited_layers or [])
            if isinstance(lyr, dict) and str(lyr.get("kind") or "") != "background"
        ) + len(existing_managed_layers)
        if len(non_bg_current) != expected_non_bg_count:
            raise BridgeError(
                f"SAFETY BLOCK: Variant non-background layer count changed unexpectedly "
                f"(expected {expected_non_bg_count}, found {len(non_bg_current)}); aborting before write."
            )


def verify_managed_layers_before_rollback(
    target_wal: dict[str, Any],
    inherited_layers: list[dict[str, Any]],
    current_layers: list[dict[str, Any]],
    tolerance_scale: float = 1.0,
) -> None:
    """Verify target layer identity BEFORE issuing any `rollback` write — across ALL WAL statuses."""
    scale = validate_tolerance_scale(tolerance_scale)
    inh_ok, inh_violations = verify_inherited_layers_preserved(inherited_layers, {"layers": current_layers})
    if not inh_ok:
        raise BridgeError(
            f"SAFETY BLOCK: Inherited client layer(s) altered before rollback: {'; '.join(inh_violations)}"
        )

    wal_status = str(target_wal.get("status") or "")
    resuming_rollback = wal_status in ("rollback_in_flight", "rollback_failed")
    prev_managed: dict[str, str] = dict(target_wal.get("previous_managed_layers") or {})
    planned_managed: dict[str, str] = dict(target_wal.get("planned_managed_layers") or {})
    prev_records: dict[str, dict[str, Any]] = dict(target_wal.get("previous_managed_layer_records") or {})
    after_records: dict[str, dict[str, Any]] = dict(target_wal.get("after_managed_layer_records") or {})
    req_masks: dict[str, Any] = dict((target_wal.get("requested_spec") or {}).get("masks") or {})
    snap_masks: dict[str, Any] = dict((target_wal.get("before_spec_snapshot") or {}).get("masks") or {})

    non_bg_current = [
        lyr for lyr in (current_layers or [])
        if isinstance(lyr, dict) and str(lyr.get("kind") or "") != "background"
    ]

    allowed_counts: dict[str, int] = {}
    for lyr in (inherited_layers or []):
        if isinstance(lyr, dict) and str(lyr.get("kind") or "") != "background":
            nm = str(lyr.get("name") or "")
            allowed_counts[nm] = allowed_counts.get(nm, 0) + 1
    for nm in planned_managed.values():
        allowed_counts[nm] = allowed_counts.get(nm, 0) + 1

    observed_counts: dict[str, int] = {}
    for lyr in non_bg_current:
        nm = str(lyr.get("name") or "")
        observed_counts[nm] = observed_counts.get(nm, 0) + 1
        if nm not in allowed_counts:
            raise BridgeError(
                f"SAFETY BLOCK: Unrecognized layer {nm!r} present on variant before rollback "
                "(a managed layer may have been renamed); aborting before write."
            )
    for nm, count in observed_counts.items():
        if count > allowed_counts.get(nm, 0):
            raise BridgeError(
                f"SAFETY BLOCK: Duplicate/extra layer(s) named {nm!r} found before rollback "
                f"(found {count}, allowed {allowed_counts.get(nm, 0)}); aborting before write."
            )

    # Closes R7 P2 #4: When resuming a multi-layer rollback where some newly created layers were already
    # deleted before interruption, compute the exact downward stack-index shift caused ONLY by those
    # confirmed deleted predecessor layers (`orig_deleted_idx < layer_orig_idx`).
    deleted_new_orig_indices: set[int] = set()
    if resuming_rollback:
        live_names_set = {str(lyr.get("name") or "") for lyr in non_bg_current}
        for m_key, m_name in planned_managed.items():
            if m_key not in prev_managed and m_key in after_records and m_name not in live_names_set:
                d_idx = after_records[m_key].get("index")
                if d_idx is not None:
                    deleted_new_orig_indices.add(int(d_idx))

    def compute_expected_shifted_index(rec_obj: dict[str, Any]) -> int | None:
        raw_idx = rec_obj.get("index")
        if raw_idx is None:
            return None
        orig_i = int(raw_idx)
        shift = sum(1 for d_i in deleted_new_orig_indices if d_i < orig_i)
        return orig_i - shift

    for mask_key, layer_name in planned_managed.items():
        matches = [lyr for lyr in non_bg_current if str(lyr.get("name") or "") == layer_name]
        if len(matches) > 1:
            raise BridgeError(
                f"SAFETY BLOCK: Found {len(matches)} duplicate layers named {layer_name!r} before rollback; "
                "aborting before write."
            )

        if mask_key in prev_managed:
            if len(matches) == 0:
                raise BridgeError(
                    f"SAFETY BLOCK: Previously managed layer {layer_name!r} is missing or renamed before rollback; "
                    "aborting before write."
                )
            live_lyr = matches[0]
            # Construct lattice target record from `prev_records` or `before_spec_snapshot` when resuming rollback
            lattice_target: dict[str, Any] | None = None
            if resuming_rollback:
                if mask_key in prev_records:
                    lattice_target = prev_records[mask_key]
                elif mask_key in snap_masks and isinstance(snap_masks[mask_key], dict):
                    s_cfg = snap_masks[mask_key]
                    lattice_target = {
                        "kind": (after_records.get(mask_key) or {}).get("kind"),
                        "index": (after_records.get(mask_key) or {}).get("index"),
                        "enabled": s_cfg.get("enabled", True),
                        "opacity": s_cfg.get("opacity", 100),
                        "adjustments": {k: v for k, v in s_cfg.items() if k in ADJUSTMENT_PROP_MAP},
                    }

            if mask_key in after_records:
                verify_layer_matches_signature(
                    live_lyr,
                    after_records[mask_key],
                    layer_name,
                    tolerance_scale=scale,
                    lattice_target_rec=lattice_target,
                    expected_index_override=compute_expected_shifted_index(after_records[mask_key]),
                )
            elif mask_key in prev_records:
                prev_rec = prev_records[mask_key]
                req_cfg = req_masks.get(mask_key) if isinstance(req_masks.get(mask_key), dict) else {}
                if prev_rec.get("kind") and str(live_lyr.get("kind") or "") != str(prev_rec["kind"]):
                    raise BridgeError(
                        f"SAFETY BLOCK: Layer {layer_name!r} kind changed before rollback; aborting before write."
                    )
                if prev_rec.get("index") is not None and live_lyr.get("index") is not None:
                    if int(live_lyr["index"]) != int(prev_rec["index"]):
                        raise BridgeError(
                            f"SAFETY BLOCK: Layer {layer_name!r} index changed before rollback; aborting before write."
                        )
                op_allowed = [prev_rec["opacity"]] if prev_rec.get("opacity") is not None else []
                if "opacity" in req_cfg and is_recoverable_number(req_cfg["opacity"]):
                    op_allowed.append(req_cfg["opacity"])
                if op_allowed and not any(within_tolerance(live_lyr.get("opacity"), c, 1.0) for c in op_allowed):
                    raise BridgeError(
                        f"SAFETY BLOCK: Layer {layer_name!r} opacity drifted before rollback; aborting before write."
                    )
                prev_adj = prev_rec.get("adjustments") or {}
                live_adj = live_lyr.get("adjustments") or {}
                for k in ADJUSTMENT_PROP_MAP:
                    cand_vals = []
                    if is_recoverable_number(prev_adj.get(k)):
                        cand_vals.append(prev_adj[k])
                    if k in req_cfg and is_recoverable_number(req_cfg[k]):
                        cand_vals.append(req_cfg[k])
                    if not cand_vals:
                        continue
                    av = live_adj.get(k)
                    tol = get_field_tolerance(k, scale)
                    if not any(within_tolerance(av, cv, tol) for cv in cand_vals):
                        raise BridgeError(
                            f"SAFETY BLOCK: Layer {layer_name!r} field {k!r} drifted before rollback "
                            f"(allowed {cand_vals}, got {av}); aborting before write."
                        )
            else:
                raise BridgeError(
                    f"SAFETY BLOCK: Missing ownership signature for existing layer {layer_name!r} before rollback; "
                    "aborting before write."
                )
        else:
            # Newly planned in the round being rolled back
            if mask_key in after_records:
                if len(matches) == 0:
                    if resuming_rollback:
                        # Already deleted during the interrupted first rollback attempt!
                        continue
                    raise BridgeError(
                        f"SAFETY BLOCK: Newly created managed layer {layer_name!r} was renamed or deleted "
                        "before rollback; aborting before write."
                    )
                verify_layer_matches_signature(
                    matches[0],
                    after_records[mask_key],
                    layer_name,
                    tolerance_scale=scale,
                    expected_index_override=compute_expected_shifted_index(after_records[mask_key]),
                )
            else:
                if len(matches) > 0:
                    raise BridgeError(
                        f"SAFETY BLOCK: Layer {layer_name!r} exists after an interrupted apply without "
                        "post-creation signature proof; cannot confirm layer ownership, retaining pending "
                        "recovery state and aborting before write."
                    )


def snapshot_managed_layer_records(
    managed_layers: dict[str, str],
    state: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Capture authoritative signature of each uniquely present managed layer from Capture One read-back."""
    records: dict[str, dict[str, Any]] = {}
    all_layers = [lyr for lyr in (state.get("layers") or []) if isinstance(lyr, dict)]
    for mask_key, layer_name in managed_layers.items():
        matches = [lyr for lyr in all_layers if str(lyr.get("name") or "") == layer_name]
        if len(matches) == 1:
            lyr = matches[0]
            records[mask_key] = {
                "name": layer_name,
                "kind": lyr.get("kind"),
                "index": lyr.get("index"),
                "enabled": lyr.get("enabled"),
                "opacity": lyr.get("opacity"),
                "adjustments": dict(lyr.get("adjustments") or {}),
            }
    return records


def allocate_managed_layer_plan(
    masks_spec: dict[str, Any],
    existing_managed_layers: dict[str, str],
    inherited_layers: list[dict[str, Any]],
    current_layers: list[dict[str, Any]] | None,
    clone_token: str,
) -> dict[str, tuple[str, bool]]:
    """Resolve `(layer_name, already_managed)` for each requested mask without colliding with client layers."""
    inherited_non_bg = {
        str(lyr.get("name") or "")
        for lyr in (inherited_layers or [])
        if str(lyr.get("kind") or "") != "background"
    }
    live_unmanaged = {
        str(lyr.get("name") or "")
        for lyr in (current_layers or [])
        if str(lyr.get("kind") or "") != "background"
        and str(lyr.get("name") or "") not in set(existing_managed_layers.values())
    }
    reserved_names = inherited_non_bg | live_unmanaged | set(existing_managed_layers.values())

    plan: dict[str, tuple[str, bool]] = {}
    for mask_key, mask_cfg in masks_spec.items():
        if mask_cfg is None:
            continue
        if mask_key not in MASK_SPECS:
            allowed_masks = ", ".join(sorted(MASK_SPECS))
            raise ValueError(f"Unsupported mask key {mask_key!r}. Allowed: {allowed_masks}")
        if mask_key in existing_managed_layers:
            recorded_name = existing_managed_layers[mask_key]
            if recorded_name in inherited_non_bg:
                raise BridgeError(
                    f"SAFETY BLOCK: Managed layer {recorded_name!r} collides with inherited client layer."
                )
            plan[mask_key] = (recorded_name, True)
        else:
            base_name = MASK_SPECS[mask_key]["default_name"]
            candidate = base_name
            suffix_idx = 1
            while candidate in reserved_names:
                candidate = f"{base_name}__m_{clone_token}" if suffix_idx == 1 else f"{base_name}__m_{clone_token}_{suffix_idx}"
                suffix_idx += 1
            reserved_names.add(candidate)
            plan[mask_key] = (candidate, False)
    return plan


def verify_inherited_layers_preserved(
    inherited_layers: list[dict[str, Any]],
    after_state: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Verify every non-background layer inherited from the client RAW remains intact and unmodified."""
    client_layers = [
        lyr for lyr in (inherited_layers or [])
        if isinstance(lyr, dict) and str(lyr.get("kind") or "") != "background"
    ]
    if not client_layers:
        return True, []

    actual_non_bg = [
        lyr for lyr in (after_state.get("layers") or [])
        if isinstance(lyr, dict) and str(lyr.get("kind") or "") != "background"
    ]

    violations: list[str] = []
    used_indices: set[int] = set()
    last_matched_pos = -1
    for expected_lyr in client_layers:
        exp_name = str(expected_lyr.get("name") or "")
        exp_kind = str(expected_lyr.get("kind") or "")
        matched_actual = None
        matched_pos = -1
        for idx, cand in enumerate(actual_non_bg):
            if idx in used_indices:
                continue
            if str(cand.get("name") or "") == exp_name and str(cand.get("kind") or "") == exp_kind:
                matched_actual = cand
                matched_pos = idx
                used_indices.add(idx)
                break
        if matched_actual is None:
            violations.append(f"Inherited client layer {exp_name!r} ({exp_kind}) is missing after operation")
            continue
        if matched_pos < last_matched_pos:
            violations.append(
                f"Inherited client layer {exp_name!r} relative stack order changed unexpectedly"
            )
        last_matched_pos = matched_pos

        if bool(matched_actual.get("enabled")) is not bool(expected_lyr.get("enabled")):
            violations.append(
                f"Inherited client layer {exp_name!r} enabled changed: "
                f"expected {expected_lyr.get('enabled')}, got {matched_actual.get('enabled')}"
            )
        exp_op = expected_lyr.get("opacity")
        act_op = matched_actual.get("opacity")
        if exp_op is not None and not within_tolerance(act_op, exp_op, 0.5):
            violations.append(
                f"Inherited client layer {exp_name!r} opacity changed: expected {exp_op}, got {act_op}"
            )
        exp_adj = expected_lyr.get("adjustments") or {}
        act_adj = matched_actual.get("adjustments") or {}
        for k in ADJUSTMENT_PROP_MAP:
            ev = exp_adj.get(k)
            if ev is None:
                continue
            av = act_adj.get(k)
            tol = get_field_tolerance(k)
            if not within_tolerance(av, ev, tol):
                violations.append(
                    f"Inherited client layer {exp_name!r} adjustment {k!r} changed: expected {ev}, got {av}"
                )

    return (len(violations) == 0, violations)


def build_status_script() -> str:
    return r'''
on run argv
    set browseFolder to item 1 of argv
    tell application "Capture One"
        set appVer to app version
        if (count of documents) is 0 then
            return "{\"running\":true,\"app_version\":" & my jsonStr(appVer) & ",\"has_document\":false}"
        end if
        set docRef to current document
        if browseFolder is not "" then
            browse docRef to path browseFolder
            delay 1.0
        end if
        set colName to ""
        try
            set colName to name of (current collection of docRef)
        end try
        set recipeList to {}
        repeat with rRef in (every recipe of docRef)
            set rkv to {}
            set end of rkv to "\"name\":" & my jsonStr(name of rRef)
            set end of rkv to "\"enabled\":" & my jsonBool(enabled of rRef)
            set end of rkv to "\"format\":" & my jsonStr((output format of rRef) as text)
            set end of rkv to "\"jpeg_quality\":" & my jsonNum(JPEG quality of rRef)
            set end of rkv to "\"color_profile\":" & my jsonStr(color profile of rRef)
            set end of rkv to "\"ignore_crop\":" & my jsonBool(ignore crop of rRef)
            set end of recipeList to "{" & my joinItems(rkv, ",") & "}"
        end repeat

        set varList to {}
        repeat with vRef in (every variant of docRef)
            set imgRef to parent image of vRef
            set dimVal to dimensions of imgRef
            set vkv to {}
            set end of vkv to "\"variant_id\":" & my jsonStr(id of vRef)
            set end of vkv to "\"variant_name\":" & my jsonStr(name of vRef)
            set end of vkv to "\"position\":" & my jsonNum(position of vRef)
            set end of vkv to "\"selected\":" & my jsonBool(selected of vRef)
            set end of vkv to "\"color_tag\":" & my jsonNum(color tag of vRef)
            set end of vkv to "\"rating\":" & my jsonNum(rating of vRef)
            set end of vkv to "\"image_name\":" & my jsonStr(name of imgRef)
            set end of vkv to "\"raw_path\":" & my jsonStr(path of imgRef)
            set end of vkv to "\"dimensions\":[" & my jsonNum(item 1 of dimVal) & "," & my jsonNum(item 2 of dimVal) & "]"
            set end of vkv to "\"layer_count\":" & my jsonNum(count of layers of vRef)
            set end of varList to "{" & my joinItems(vkv, ",") & "}"
        end repeat

        set dkv to {}
        set end of dkv to "\"running\":true"
        set end of dkv to "\"app_version\":" & my jsonStr(appVer)
        set end of dkv to "\"has_document\":true"
        set end of dkv to "\"document_name\":" & my jsonStr(name of docRef)
        set end of dkv to "\"document_kind\":" & my jsonStr((kind of docRef) as text)
        set end of dkv to "\"current_collection\":" & my jsonStr(colName)
        set end of dkv to "\"processing_queue_enabled\":" & my jsonBool(processing queue enabled of docRef)
        set end of dkv to "\"recipes\":[" & my joinItems(recipeList, ",") & "]"
        set end of dkv to "\"variants\":[" & my joinItems(varList, ",") & "]"
        return "{" & my joinItems(dkv, ",") & "}"
    end tell
end run
'''


def build_inspect_script() -> str:
    return r'''
on run argv
    set targetId to item 1 of argv
    tell application "Capture One"
        if (count of documents) is 0 then error "No open Capture One document."
        set docRef to current document
        set vRef to my findVariantById(docRef, targetId)
        return my serializeVariantFull(vRef)
    end tell
end run
'''


def build_prepare_variant_script() -> str:
    """Clone source variant, restore full frame, and safely apply default retouching."""
    restore_pairs = [(f"base_{k}", prop) for k, prop in ADJUSTMENT_PROP_MAP.items()]
    read_base_code = "\n        ".join(
        f"set {var_name} to {prop} of srcAdj" for var_name, prop in restore_pairs
    )
    write_base_code = "\n            ".join(
        f"if {var_name} is not missing value then set {prop} of dstAdj to {var_name}"
        for var_name, prop in restore_pairs
    )

    return f'''
on run argv
    set sourceVarId to item 1 of argv
    set existingCloneId to item 2 of argv
    set retouchSourceVarId to item 3 of argv
    set retouchStyleName to item 4 of argv
    set colorTagStr to item 5 of argv

    tell application "Capture One"
        if (count of documents) is 0 then error "No open Capture One document."
        set docRef to current document

        if existingCloneId is not "" then
            try
                set existingClone to my findVariantById(docRef, existingCloneId)
                return "{{\\"source_variant_id\\":" & my jsonStr(sourceVarId) & ",\\"reused_existing_clone\\":true,\\"retouch_mode\\":\\"reused\\",\\"cloned_variant\\":" & my serializeVariantFull(existingClone) & "}}"
            end try
        end if

        set srcVar to my findVariantById(docRef, sourceVarId)
        set srcNonBgCount to 0
        repeat with lyr in (every layer of srcVar)
            if ((kind of lyr) as text) is not "background" then
                set srcNonBgCount to srcNonBgCount + 1
            end if
        end repeat

        if (retouchSourceVarId is not "") and (srcNonBgCount > 0) then
            error "SAFETY BLOCK: Source variant has " & (srcNonBgCount as text) & " pre-existing local layer(s). Clipboard retouch copy/apply is blocked to protect client edits; use --retouch-style instead."
        end if

        set srcAdj to adjustments of srcVar
        {read_base_code}
        set base_color_profile to color profile of srcAdj
        set base_film_curve to film curve of srcAdj

        set newVar to clone variant srcVar additive select false
        if (id of newVar) is sourceVarId then
            error "Clone variant returned identical ID as source variant."
        end if
        try
            maximum crop newVar horizontal true vertical true apply true
        end try

        set retouchApplied to "none"
        if retouchStyleName is not "" then
            set bgLayer to first layer of newVar whose kind is background
            apply style bgLayer named retouchStyleName
            set dstAdj to adjustments of newVar
            {write_base_code}
            if base_color_profile is not missing value then set color profile of dstAdj to base_color_profile
            if base_film_curve is not missing value then set film curve of dstAdj to base_film_curve
            set retouchApplied to "applied_style"
        else if retouchSourceVarId is not "" then
            set retouchVar to my findVariantById(docRef, retouchSourceVarId)
            copy adjustments retouchVar
            apply adjustments newVar
            set dstAdj to adjustments of newVar
            {write_base_code}
            if base_color_profile is not missing value then set color profile of dstAdj to base_color_profile
            if base_film_curve is not missing value then set film curve of dstAdj to base_film_curve
            repeat with idx from (count of layers of newVar) to 1 by -1
                set lyr to layer idx of newVar
                if ((kind of lyr) as text) is not "background" then
                    delete lyr
                end if
            end repeat
            set retouchApplied to "copied_from_variant"
        end if

        if colorTagStr is not "" then
            set color tag of newVar to (colorTagStr as integer)
        end if

        return "{{\\"source_variant_id\\":" & my jsonStr(sourceVarId) & ",\\"reused_existing_clone\\":false,\\"retouch_mode\\":" & my jsonStr(retouchApplied) & ",\\"cloned_variant\\":" & my serializeVariantFull(newVar) & "}}"
    end tell
end run
'''


def build_apply_script(
    spec: dict[str, Any],
    layer_plan: dict[str, tuple[str, bool]] | None = None,
    allow_layer_wb: bool = False,
) -> str:
    """Generate deterministic AppleScript for global adjustments and isolated managed AI masks."""
    global_spec = spec.get("global") or {}
    if not isinstance(global_spec, dict):
        raise ValueError("'global' in spec must be an object.")
    masks_spec = spec.get("masks") or {}
    if not isinstance(masks_spec, dict):
        raise ValueError("'masks' in spec must be an object.")
    reject_unverifiable_mask_ops(masks_spec)

    global_ops = validate_adjustment_dict(global_spec, "global", allow_layer_wb=allow_layer_wb)
    if layer_plan is None:
        layer_plan = allocate_managed_layer_plan(
            masks_spec=masks_spec,
            existing_managed_layers={},
            inherited_layers=[],
            current_layers=None,
            clone_token="init",
        )

    lines: list[str] = []
    if global_ops:
        lines.append("        set gAdj to adjustments of vRef")
        for _, as_prop, val in global_ops:
            lines.append(f"        set {as_prop} of gAdj to {format_as_number(val)}")

    for mask_key, mask_cfg in masks_spec.items():
        if mask_cfg is None:
            continue
        if mask_key not in MASK_SPECS:
            allowed_masks = ", ".join(sorted(MASK_SPECS))
            raise ValueError(f"Unsupported mask key {mask_key!r}. Allowed: {allowed_masks}")
        if not isinstance(mask_cfg, dict):
            raise ValueError(f"masks.{mask_key} must be an object.")

        meta = MASK_SPECS[mask_key]
        layer_name, already_managed = layer_plan[mask_key]
        delete_layer = mask_cfg.get("_delete") is True
        opacity = mask_cfg.get("opacity")
        enabled = mask_cfg.get("enabled")

        adj_dict = {
            k: v for k, v in mask_cfg.items()
            if k not in ("opacity", "enabled", "feather", "refine", "_delete")
        }
        layer_ops = validate_adjustment_dict(
            adj_dict,
            f"masks.{mask_key}",
            allow_layer_wb=allow_layer_wb,
        )

        lines.append('        set targetLayer to missing value')
        lines.append('        set matchCount to 0')
        lines.append('        repeat with existingLyr in (every layer of vRef)')
        lines.append(f'            if (name of existingLyr) is "{layer_name}" then')
        lines.append('                set matchCount to matchCount + 1')
        lines.append('                set targetLayer to existingLyr')
        lines.append('            end if')
        lines.append('        end repeat')
        lines.append('        if matchCount > 1 then')
        lines.append(f'            error "SAFETY BLOCK: Duplicate layers named {layer_name} found on variant"')
        lines.append('        end if')

        if delete_layer:
            lines.append('        if targetLayer is not missing value then')
            lines.append('            delete targetLayer')
            lines.append('        end if')
            continue

        if not already_managed:
            lines.append('        if matchCount > 0 then')
            lines.append(f'            error "SAFETY BLOCK: Refusing to overwrite unmanaged existing layer named {layer_name}"')
            lines.append('        end if')
            if enabled is False and not layer_ops:
                continue
        else:
            if enabled is False and not layer_ops:
                lines.append('        if targetLayer is not missing value then set enabled of targetLayer to false')
                continue
            lines.append('        if matchCount is 0 then')
            lines.append(f'            error "SAFETY BLOCK: Expected managed layer {layer_name} is missing"')
            lines.append('        end if')

        if meta["mode"] == "people":
            area_enum = meta["area"]
            lines.append('        if targetLayer is missing value then')
            lines.append('            set beforeLayerCount to (count of layers of vRef)')
            lines.append(f'            create people mask vRef areas {{{area_enum}}} with separate layers')
            lines.append('            set afterLayerCount to (count of layers of vRef)')
            lines.append(f'            if afterLayerCount <= beforeLayerCount then error "Capture One create people mask did not create a layer for {area_enum}"')
            lines.append('            set targetLayer to last layer of vRef')
            lines.append(f'            set name of targetLayer to "{layer_name}"')
            lines.append('        end if')
        else:
            kind_enum = meta["kind"]
            lines.append('        if targetLayer is missing value then')
            lines.append('            try')
            lines.append(f'                set targetLayer to (make new layer at end of layers of vRef with properties {{name:"{layer_name}", kind:{kind_enum}}})')
            lines.append('            on error')
            lines.append(f'                set targetLayer to (make new layer at vRef with properties {{name:"{layer_name}", kind:{kind_enum}}})')
            lines.append('            end try')
            lines.append('        end if')

        if enabled is not None:
            if not isinstance(enabled, bool):
                raise ValueError(f"masks.{mask_key}.enabled must be boolean")
            lines.append(f'        set enabled of targetLayer to {"true" if enabled else "false"}')
        else:
            lines.append('        set enabled of targetLayer to true')
        if opacity is not None:
            if not is_recoverable_number(opacity) or not (0.0 <= float(opacity) <= 100.0):
                raise ValueError(f"masks.{mask_key}.opacity must be a finite number between 0 and 100")
            lines.append(f'        set opacity of targetLayer to {int(round(float(opacity)))}')

        if layer_ops:
            lines.append('        set lAdj to adjustments of targetLayer')
            for _, as_prop, val in layer_ops:
                lines.append(f'        set {as_prop} of lAdj to {format_as_number(val)}')

    body_ops = "\n".join(lines)
    return f'''
on run argv
    set targetId to item 1 of argv
    tell application "Capture One"
        if (count of documents) is 0 then error "No open Capture One document."
        set docRef to current document
        set vRef to my findVariantById(docRef, targetId)
{body_ops}
        return my serializeVariantFull(vRef)
    end tell
end run
'''


def verify_readback(
    spec: dict[str, Any],
    state: dict[str, Any],
    tolerance_scale: float = 1.0,
    managed_layer_names: dict[str, str] | None = None,
) -> tuple[bool, list[str]]:
    """Strictly verify every requested global and managed-mask field (NEVER skipping `feather`/`refine`)."""
    scale = validate_tolerance_scale(tolerance_scale)

    mismatches: list[str] = []
    global_spec = spec.get("global") or {}
    global_actual = state.get("global_adjustments") or {}
    for key, expected in global_spec.items():
        if expected is None:
            continue
        if not is_recoverable_number(expected):
            mismatches.append(f"global.{key}: non-finite expected value {expected!r}")
            continue
        actual_raw = global_actual.get(key) if isinstance(global_actual, dict) else None
        if not is_recoverable_number(actual_raw):
            mismatches.append(f"global.{key}: expected {expected}, got missing/null/non-finite {actual_raw!r}")
            continue
        tol = get_field_tolerance(key, scale)
        if not within_tolerance(actual_raw, expected, tol, field_key=key):
            mismatches.append(f"global.{key}: expected {expected} (+/-{tol}), got {float(actual_raw)}")

    layers_by_name_all: dict[str, list[dict[str, Any]]] = {}
    for lyr in (state.get("layers") or []):
        if isinstance(lyr, dict) and lyr.get("name") is not None:
            layers_by_name_all.setdefault(str(lyr["name"]), []).append(lyr)

    masks_spec = spec.get("masks") or {}
    for mask_key, mask_cfg in masks_spec.items():
        if not mask_cfg:
            continue
        if mask_key not in MASK_SPECS:
            mismatches.append(f"masks.{mask_key}: unsupported mask key")
            continue
        layer_name = (
            managed_layer_names[mask_key]
            if (managed_layer_names and mask_key in managed_layer_names)
            else MASK_SPECS[mask_key]["default_name"]
        )
        matches = layers_by_name_all.get(layer_name, [])

        if mask_cfg.get("_delete") is True:
            if len(matches) != 0:
                mismatches.append(
                    f"masks.{mask_key}: expected layer {layer_name!r} to be deleted on rollback, "
                    f"but {len(matches)} still present"
                )
            continue

        adj_keys = [
            k for k, v in mask_cfg.items()
            if k not in ("opacity", "enabled", "feather", "refine", "_delete") and v is not None
        ]
        mask_op_keys = [k for k in ("feather", "refine") if mask_cfg.get(k) is not None]

        if mask_cfg.get("enabled") is False and not adj_keys and not mask_op_keys and len(matches) == 0:
            continue

        if len(matches) == 0:
            mismatches.append(f"masks.{mask_key}: expected managed layer {layer_name!r} not found")
            continue
        if len(matches) > 1:
            mismatches.append(
                f"masks.{mask_key}: ambiguous duplicate layers named {layer_name!r} ({len(matches)} found)"
            )
            continue

        lyr = matches[0]
        if "opacity" in mask_cfg and mask_cfg["opacity"] is not None:
            exp_op = mask_cfg["opacity"]
            actual_op = lyr.get("opacity")
            if not is_recoverable_number(exp_op) or not within_tolerance(actual_op, round(float(exp_op)), 1.0):
                mismatches.append(f"masks.{mask_key}.opacity: expected {exp_op}, got {actual_op!r}")

        if "enabled" in mask_cfg and mask_cfg["enabled"] is not None:
            if lyr.get("enabled") is not bool(mask_cfg["enabled"]):
                mismatches.append(f"masks.{mask_key}.enabled: expected {mask_cfg['enabled']}, got {lyr.get('enabled')}")

        l_actual = lyr.get("adjustments")
        if not isinstance(l_actual, dict):
            l_actual = {}

        # Closes R6 P2 #4: NEVER skip `feather` or `refine` in read-back verification!
        for op_k in mask_op_keys:
            expected_op = mask_cfg[op_k]
            if not is_recoverable_number(expected_op):
                mismatches.append(f"masks.{mask_key}.{op_k}: non-finite expected value {expected_op!r}")
                continue
            actual_op_val = lyr.get(op_k, l_actual.get(op_k))
            if not is_recoverable_number(actual_op_val):
                mismatches.append(
                    f"masks.{mask_key}.{op_k}: expected {expected_op}, got missing/null/non-finite "
                    f"{actual_op_val!r} (unverified mask {op_k})"
                )
                continue
            tol = get_field_tolerance(op_k, scale)
            if not within_tolerance(actual_op_val, expected_op, tol):
                mismatches.append(
                    f"masks.{mask_key}.{op_k}: expected {expected_op} (+/-{tol}), got {float(actual_op_val)}"
                )

        for key in adj_keys:
            expected = mask_cfg[key]
            if not is_recoverable_number(expected):
                mismatches.append(f"masks.{mask_key}.{key}: non-finite expected value {expected!r}")
                continue
            actual_raw = l_actual.get(key)
            if not is_recoverable_number(actual_raw):
                mismatches.append(f"masks.{mask_key}.{key}: expected {expected}, got missing/null/non-finite {actual_raw!r}")
                continue
            tol = get_field_tolerance(key, scale)
            if not within_tolerance(actual_raw, expected, tol):
                mismatches.append(f"masks.{mask_key}.{key}: expected {expected} (+/-{tol}), got {float(actual_raw)}")

    return (len(mismatches) == 0, mismatches)


def build_ensure_recipes_script() -> str:
    return r'''
on ensureNamedRecipe(docRef, rName)
    tell application "Capture One"
        repeat with r in (every recipe of docRef)
            if (name of r) is rName then return r
        end repeat
        return (make new recipe at docRef with properties {name:rName})
    end tell
end ensureNamedRecipe

on run argv
    set previewName to item 1 of argv
    set handoffName to item 2 of argv
    set srgbProfile to item 3 of argv
    set previewLongEdge to (item 4 of argv) as integer

    tell application "Capture One"
        if (count of documents) is 0 then error "No open Capture One document."
        set docRef to current document

        set prevR to my ensureNamedRecipe(docRef, previewName)
        set output format of prevR to JPEG
        set JPEG quality of prevR to 85
        set color profile of prevR to srgbProfile
        set scaling method of prevR to Long_Edge
        set scaling unit of prevR to pixels
        set primary scaling value of prevR to previewLongEdge
        set upscale of prevR to false
        set ignore crop of prevR to true
        set export crop method of prevR to ignore
        set include ratings of prevR to false
        set include copyright of prevR to false
        set include GPS of prevR to false
        set include Camera Metadata of prevR to false
        set include Other Metadata of prevR to false
        set include annotations of prevR to false
        set existing files of prevR to skip

        set fullR to my ensureNamedRecipe(docRef, handoffName)
        set output format of fullR to JPEG
        set JPEG quality of fullR to 100
        set color profile of fullR to srgbProfile
        set scaling method of fullR to Fixed
        set scaling unit of fullR to percent
        set primary scaling value of fullR to 100
        set upscale of fullR to false
        set ignore crop of fullR to true
        set export crop method of fullR to ignore
        set existing files of fullR to skip

        set outJson to "{\"preview_recipe\":{\"name\":" & my jsonStr(name of prevR) & ",\"jpeg_quality\":" & my jsonNum(JPEG quality of prevR) & ",\"color_profile\":" & my jsonStr(color profile of prevR) & ",\"ignore_crop\":" & my jsonBool(ignore crop of prevR) & "},\"handoff_recipe\":{\"name\":" & my jsonStr(name of fullR) & ",\"jpeg_quality\":" & my jsonNum(JPEG quality of fullR) & ",\"color_profile\":" & my jsonStr(color profile of fullR) & ",\"ignore_crop\":" & my jsonBool(ignore crop of fullR) & ",\"export_crop_method\":" & my jsonStr((export crop method of fullR) as text) & "}}"
        return outJson
    end tell
end run
'''


def build_export_script() -> str:
    return r'''
on run argv
    set targetId to item 1 of argv
    set recipeName to item 2 of argv
    set stagingDirPath to item 3 of argv
    set exactOutputStem to item 4 of argv

    tell application "Capture One"
        if (count of documents) is 0 then error "No open Capture One document."
        set docRef to current document
        set vRef to my findVariantById(docRef, targetId)
        set imgRef to parent image of vRef
        set dimVal to dimensions of imgRef
        set vAdj to adjustments of vRef
        set orientVal to 0
        try
            set orientVal to orientation of vAdj
        end try

        set rRef to missing value
        repeat with r in (every recipe of docRef)
            if (name of r) is recipeName then
                set rRef to r
                exit repeat
            end if
        end repeat
        if rRef is missing value then error "Recipe not found: " & recipeName

        set enabled of rRef to true
        set root folder location of rRef to (POSIX file stagingDirPath as alias)
        set root folder type of rRef to custom location
        set output sub folder of rRef to ""
        set output name format of rRef to exactOutputStem
        set existing files of rRef to skip
        set processing queue enabled of docRef to true

        set jobResult to (process vRef recipe recipeName)
        set ekv to {}
        set end of ekv to "\"job_id\":" & my jsonStr(jobResult)
        set end of ekv to "\"variant_id\":" & my jsonStr(id of vRef)
        set end of ekv to "\"image_name\":" & my jsonStr(name of imgRef)
        set end of ekv to "\"raw_path\":" & my jsonStr(path of imgRef)
        set end of ekv to "\"raw_dimensions\":[" & my jsonNum(item 1 of dimVal) & "," & my jsonNum(item 2 of dimVal) & "]"
        set end of ekv to "\"orientation\":" & my jsonNum(orientVal)
        set end of ekv to "\"recipe_format\":" & my jsonStr((output format of rRef) as text)
        set end of ekv to "\"recipe_quality\":" & my jsonNum(JPEG quality of rRef)
        set end of ekv to "\"recipe_profile\":" & my jsonStr(color profile of rRef)
        set end of ekv to "\"scaling_method\":" & my jsonStr((scaling method of rRef) as text)
        set end of ekv to "\"primary_scaling_value\":" & my jsonNum(primary scaling value of rRef)
        set end of ekv to "\"ignore_crop\":" & my jsonBool(ignore crop of rRef)
        set end of ekv to "\"export_crop_method\":" & my jsonStr((export crop method of rRef) as text)
        return "{" & my joinItems(ekv, ",") & "}"
    end tell
end run
'''


def build_check_job_script() -> str:
    return r'''
on run argv
    set targetJobId to item 1 of argv
    tell application "Capture One"
        if (count of documents) is 0 then return "false"
        set docRef to current document
        repeat with j in (every job of docRef)
            if (id of j) is targetJobId then return "true"
        end repeat
        return "false"
    end tell
end run
'''


def inspect_jpg_with_sips(jpg_path: Path) -> dict[str, Any]:
    """Read decoded pixel dimensions, format, and ICC profile of an exported JPG via macOS sips."""
    res = subprocess.run(
        [
            "/usr/bin/sips",
            "-g", "pixelWidth",
            "-g", "pixelHeight",
            "-g", "profile",
            "-g", "format",
            str(jpg_path.resolve(strict=True)),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    if res.returncode != 0:
        raise BridgeError(f"sips failed to decode {jpg_path}: {res.stderr.strip() or res.stdout.strip()}")

    info: dict[str, Any] = {"path": str(jpg_path.resolve())}
    for line in res.stdout.splitlines():
        if ":" not in line:
            continue
        k, v = [part.strip() for part in line.split(":", 1)]
        if k == "pixelWidth":
            info["width"] = int(v)
        elif k == "pixelHeight":
            info["height"] = int(v)
        elif k == "profile":
            info["icc_profile"] = v
        elif k == "format":
            info["format"] = v
    if "width" not in info or "height" not in info:
        raise BridgeError(f"Incomplete image dimensions from sips for {jpg_path}")
    return info


def expected_dimensions_for_orientation(raw_dimensions: list[int | float], orientation: int | float) -> tuple[int, int]:
    if len(raw_dimensions) != 2:
        raise ValueError(f"Invalid raw_dimensions: {raw_dimensions!r}")
    raw_w, raw_h = int(round(raw_dimensions[0])), int(round(raw_dimensions[1]))
    deg = int(round(orientation)) % 360
    if deg in (90, 270):
        return (raw_h, raw_w)
    return (raw_w, raw_h)


def validate_handoff_export(export_meta: dict[str, Any], sips_info: dict[str, Any]) -> list[str]:
    """Enforce the 5 hard quality gates on formal handoff JPG exports using real recipe read-back."""
    errors: list[str] = []

    fmt = str(sips_info.get("format") or "").lower()
    if fmt != "jpeg":
        errors.append(f"Decoded file format must be 'jpeg', got {fmt!r}")

    icc = str(sips_info.get("icc_profile") or "")
    if "srgb" not in icc.lower():
        errors.append(f"Decoded ICC profile must be sRGB, got {icc!r}")

    recipe_profile = str(export_meta.get("recipe_profile") or "")
    if "srgb" not in recipe_profile.lower():
        errors.append(f"Recipe color profile read-back must be sRGB, got {recipe_profile!r}")

    quality = export_meta.get("recipe_quality")
    if not is_recoverable_number(quality) or int(round(float(quality))) != 100:
        errors.append(f"Recipe JPEG quality read-back must be 100, got {quality!r}")

    if export_meta.get("ignore_crop") is not True:
        errors.append(f"Recipe ignore_crop must be True, got {export_meta.get('ignore_crop')!r}")

    crop_method = str(export_meta.get("export_crop_method") or "").lower()
    if not crop_method or crop_method != "ignore":
        errors.append(f"Recipe export_crop_method must be 'ignore', got {export_meta.get('export_crop_method')!r}")

    scaling_method = str(export_meta.get("scaling_method") or "").lower()
    scaling_val = export_meta.get("primary_scaling_value")
    if not scaling_method or scaling_method != "fixed":
        errors.append(f"Recipe scaling_method must be 'Fixed', got {export_meta.get('scaling_method')!r}")
    if not is_recoverable_number(scaling_val) or int(round(float(scaling_val))) != 100:
        errors.append(f"Recipe primary_scaling_value must be 100%, got {scaling_val!r}")

    raw_dims = export_meta.get("raw_dimensions")
    orient = export_meta.get("orientation", 0)
    if (
        not isinstance(raw_dims, list)
        or len(raw_dims) != 2
        or not is_recoverable_number(raw_dims[0])
        or not is_recoverable_number(raw_dims[1])
    ):
        errors.append(f"Missing/invalid raw_dimensions in export metadata: {raw_dims!r}")
    else:
        raw_w, raw_h = int(round(float(raw_dims[0]))), int(round(float(raw_dims[1])))
        exp_w, exp_h = expected_dimensions_for_orientation(raw_dims, orient if is_recoverable_number(orient) else 0)
        act_w, act_h = int(sips_info.get("width", -1)), int(sips_info.get("height", -1))
        # Capture One 16.9 may report `dimensions of parent image` already rotated for portrait RAWs
        # and round odd pixel widths by +/-1-2 px during demosaic/lens correction.
        matches_expected = abs(act_w - exp_w) <= 2 and abs(act_h - exp_h) <= 2
        matches_raw_as_reported = abs(act_w - raw_w) <= 2 and abs(act_h - raw_h) <= 2
        if not (matches_expected or matches_raw_as_reported):
            errors.append(
                f"Exported JPG dimensions {(act_w, act_h)} do not match expected full uncropped "
                f"frame {(exp_w, exp_h)} (raw_dimensions={raw_dims}, orientation={orient})"
            )

    return errors


def wait_for_staged_jpg(
    staging_dir: Path,
    exact_stem: str,
    timeout_sec: float = 90.0,
    job_in_queue_fn=None,
) -> Path:
    """Wait for exact `<exact_stem>.jpg` inside an isolated empty staging directory."""
    exact_names = {f"{exact_stem}.jpg", f"{exact_stem}.jpeg", f"{exact_stem}.JPG", f"{exact_stem}.JPEG"}
    in_progress_names = {f"{n}.tmp" for n in exact_names}
    deadline = time.monotonic() + timeout_sec
    last_size = -1
    stable_hits = 0
    chosen: Path | None = None

    while time.monotonic() < deadline:
        entries = [p for p in staging_dir.iterdir() if p.is_file() and not p.name.startswith(".")]
        unexpected = [p.name for p in entries if p.name not in exact_names and p.name not in in_progress_names]
        if unexpected:
            raise BridgeError(
                f"Unexpected file(s) appeared in isolated staging dir {staging_dir}: {unexpected} "
                f"(expected exact stem {exact_stem!r})"
            )
        has_in_progress = any(p.name in in_progress_names for p in entries)
        matches = [p for p in entries if p.name in exact_names]
        if len(matches) > 1:
            raise BridgeError(f"Ambiguous multiple output files in staging dir: {[m.name for m in matches]}")

        if len(matches) == 1 and not has_in_progress:
            candidate = matches[0]
            sz = candidate.stat().st_size
            still_queued = bool(job_in_queue_fn()) if job_in_queue_fn is not None else False
            if sz > 0 and not still_queued and sz == last_size and chosen == candidate:
                stable_hits += 1
                if stable_hits >= 2:
                    return candidate
            else:
                chosen = candidate
                last_size = sz
                stable_hits = 0
        time.sleep(0.35)

    raise BridgeError(f"Timed out waiting for exact staged JPG '{exact_stem}.jpg' in {staging_dir}")


def load_preview_manifest(out_dir: Path) -> dict[str, str]:
    manifest_path = out_dir / PREVIEW_MANIFEST_FILENAME
    if not manifest_path.is_file():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_preview_manifest(out_dir: Path, data: dict[str, str]) -> None:
    manifest_path = out_dir / PREVIEW_MANIFEST_FILENAME
    atomic_write_text(manifest_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def unregister_from_preview_manifest(final_path: Path) -> None:
    out_dir = final_path.parent
    manifest_path = out_dir / PREVIEW_MANIFEST_FILENAME
    if not manifest_path.exists():
        return
    with file_lock(manifest_path):
        manifest = load_preview_manifest(out_dir)
        if final_path.name in manifest:
            manifest.pop(final_path.name, None)
            save_preview_manifest(out_dir, manifest)


def promote_staged_file(
    staged_file: Path,
    final_path: Path,
    allow_overwrite_same_owner: bool = False,
    expected_existing_sha256: str | None = None,
    expected_existing_file_id: tuple[int, int] | None = None,
    temp_publish_path: Path | None = None,
    expected_new_sha256: str | None = None,
    expected_new_size: int | None = None,
    register_candidate_inode_fn=None,
    pre_swap_race_hook=None,
    post_swap_before_verify_hook=None,
) -> tuple[Path, tuple[int, int]]:
    """Atomically promote staged JPG to `final_path` returning `(final_path, (st_dev, st_ino))`."""
    final_path = canonicalize_leaf_path(final_path)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    src_sha, src_size = (
        (expected_new_sha256, int(expected_new_size))
        if (expected_new_sha256 is not None and expected_new_size is not None)
        else compute_file_sha256(staged_file)
    )
    tmp_pub = temp_publish_path or (
        final_path.parent / f".c1_tmp_pub_{final_path.name}.{uuid.uuid4().hex[:10]}"
    )

    if not allow_overwrite_same_owner:
        pre_kind, _, _, _ = inspect_destination_state_unlocked(final_path)
        if pre_kind == "non_regular":
            raise BridgeError(
                f"Refusing to export to symlink or non-regular path at {final_path}."
            )
        stg_st = os.lstat(staged_file)
        stg_id = (int(stg_st.st_dev), int(stg_st.st_ino))
        if register_candidate_inode_fn is not None:
            register_candidate_inode_fn(stg_id)
        try:
            os.link(staged_file, final_path)
            staged_file.unlink(missing_ok=True)
            return (final_path, stg_id)
        except FileExistsError as exc:
            raise BridgeError(
                f"Refusing to overwrite existing file at {final_path} (atomic link EEXIST)."
            ) from exc
        except OSError as exc:
            if exc.errno not in (
                errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL, errno.EMLINK
            ):
                raise BridgeError(f"Failed to promote staged file to {final_path}: {exc}") from exc
            copy_file_verified_to_temp(
                staged_file,
                tmp_pub,
                expected_sha256=src_sha,
                expected_size=src_size,
                on_temp_created_fn=register_candidate_inode_fn,
            )
            tmp_st = os.lstat(tmp_pub)
            tmp_id = (int(tmp_st.st_dev), int(tmp_st.st_ino))
            if register_candidate_inode_fn is not None:
                register_candidate_inode_fn(tmp_id)
            pub_id = publish_verified_temp_no_overwrite(tmp_pub, final_path)
            staged_file.unlink(missing_ok=True)
            return (final_path, pub_id)

    if not expected_existing_sha256:
        raise BridgeError(
            f"Refusing overwrite at {final_path}: missing expected_existing_sha256 ownership proof."
        )

    # Pre-stage verified replacement in `final_path.parent` BEFORE locking/inspecting `final_path`
    # so cross-volume (`EXDEV`) copy latency NEVER widens the overwrite verification window!
    copy_file_verified_to_temp(
        staged_file,
        tmp_pub,
        expected_sha256=src_sha,
        expected_size=src_size,
        on_temp_created_fn=register_candidate_inode_fn,
    )
    tmp_st = os.lstat(tmp_pub)
    tmp_id = (int(tmp_st.st_dev), int(tmp_st.st_ino))
    if register_candidate_inode_fn is not None:
        register_candidate_inode_fn(tmp_id)

    with file_lock(final_path):
        pub_id = atomic_swap_replace_verified_victim(
            same_dir_replacement=tmp_pub,
            target_path=final_path,
            expected_victim_sha256=expected_existing_sha256,
            expected_victim_id=expected_existing_file_id,
            pre_swap_race_hook=pre_swap_race_hook,
            post_swap_before_verify_hook=post_swap_before_verify_hook,
        )
    staged_file.unlink(missing_ok=True)
    return (final_path, pub_id)


def load_preview_metrics_module():
    metrics_script = Path(__file__).resolve().parent / "preview-metrics.py"
    spec = importlib.util.spec_from_file_location("preview_metrics", metrics_script)
    if spec is None or spec.loader is None:
        raise BridgeError(f"Cannot load {metrics_script}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def commit_handoff_export_atomically(
    ledger_path: Path,
    variant_id: str,
    started_adjustment_seq: int,
    staged_jpg: Path,
    final_path: Path,
    overwrite_own: bool,
    sha256_hex: str,
    byte_size: int,
    export_meta: dict[str, Any],
    sips_info: dict[str, Any],
    pre_promote_hook=None,
    post_promote_hook=None,
    pre_swap_race_hook=None,
    post_swap_before_verify_hook=None,
    mid_verify_read_hook=None,
    during_receipt_save_hook=None,
    rollback_race_hook=None,
) -> None:
    """Crash-consistent two-phase file+ledger commit proving authorship & content continuity through commit end."""
    resolved_final = canonicalize_leaf_path(final_path)
    if overwrite_own:
        verify_exclusive_output_dir(resolved_final.parent)

    with file_lock(ledger_path):
        ledger = load_batch_ledger(ledger_path)
        entry = resolve_authorized_clone_entry(ledger, variant_id)
        photo_id = str(entry.get("photo_id") or variant_id)

        # 1. Verify variant health BEFORE touching destination file!
        ensure_variant_healthy_for_handoff(entry, "commit handoff export")

        # 2. Verify adjustment_seq WITHOUT invalidating any newer valid receipt (Closes R6 P2 #3)!
        current_seq = int(entry.get("adjustment_seq") or 0)
        if current_seq != started_adjustment_seq:
            raise BridgeError(
                f"Stale export rejected for {photo_id!r}: variant adjustment_seq changed "
                f"during export ({started_adjustment_seq} -> {current_seq}). Newer state untouched."
            )

        # 3. Verify destination file type, SHA-256, and owned inode WITHOUT following leaf symlinks
        preserve_receipt_ownership_before_clear(entry)
        owned_map: dict[str, dict[str, Any]] = dict(entry.get("owned_export_files") or {})
        owned_rec = owned_map.get(str(resolved_final))
        allow_overwrite = False
        expected_prev_sha: str | None = None
        expected_prev_id: tuple[int, int] | None = None
        backup_path: Path | None = None

        dest_kind, dest_sha, _, dest_id = inspect_destination_state_unlocked(resolved_final)
        if dest_kind == "non_regular":
            raise BridgeError(
                f"Refusing to export to symlink or non-regular path at {resolved_final}."
            )
        if dest_kind not in ("missing", "regular"):
            raise BridgeError(
                f"Refusing to export while destination {resolved_final} is changing concurrently ({dest_kind})."
            )

        if dest_kind == "regular":
            if not (overwrite_own and isinstance(owned_rec, dict) and owned_rec.get("sha256")):
                raise BridgeError(
                    f"Refusing to overwrite existing file at {resolved_final} (use --overwrite-own on owned file, or --versioned)."
                )
            expected_prev_sha = str(owned_rec["sha256"])
            recorded_prev_id = parse_single_file_id(owned_rec.get("file_id"))
            if dest_sha != expected_prev_sha:
                raise BridgeError(
                    f"Refusing overwrite at {resolved_final}: existing file SHA-256 ({str(dest_sha)[:12]}...) "
                    f"does not match prior owned file ({expected_prev_sha[:12]}...)."
                )
            if recorded_prev_id is not None and dest_id != recorded_prev_id:
                raise BridgeError(
                    f"Refusing overwrite at {resolved_final}: existing file inode {dest_id} does not match "
                    f"owned inode {recorded_prev_id} (same-content external file protected)."
                )
            expected_prev_id = dest_id
            allow_overwrite = True
            backup_path = resolved_final.parent / f".c1_bak_{resolved_final.name}.{uuid.uuid4().hex[:10]}"
            create_backup_copy(resolved_final, backup_path, expected_sha256=expected_prev_sha)

        temp_publish_path = resolved_final.parent / f".c1_tmp_pub_{resolved_final.name}.{uuid.uuid4().hex[:10]}"
        stg_st = os.lstat(staged_jpg)
        initial_cand_id = [int(stg_st.st_dev), int(stg_st.st_ino)]

        sips_record = dict(sips_info)
        sips_record["path"] = str(resolved_final)
        sips_record["sha256"] = sha256_hex
        sips_record["byte_size"] = byte_size

        # 4. PHASE 1: Persist `pending_export_commit` WITH non-empty `candidate_file_ids` BEFORE touching `resolved_final`!
        pending_record: dict[str, Any] = {
            "target_path": str(resolved_final),
            "backup_path": str(backup_path) if backup_path else None,
            "temp_publish_path": str(temp_publish_path),
            "previous_sha256": expected_prev_sha,
            "previous_file_id": list(expected_prev_id) if expected_prev_id else None,
            "new_sha256": sha256_hex,
            "new_byte_size": byte_size,
            "candidate_file_ids": [initial_cand_id],
            "publish_succeeded": False,
            "adjustment_seq": current_seq,
            "export_meta": export_meta,
            "sips_info": sips_record,
        }
        entry["pending_export_commit"] = pending_record
        try:
            save_batch_ledger(ledger_path, ledger)
        except BaseException:
            entry.pop("pending_export_commit", None)
            if backup_path:
                backup_path.unlink(missing_ok=True)
            raise

        def register_candidate_inode(cand_pair: tuple[int, int]) -> None:
            pair_list = [int(cand_pair[0]), int(cand_pair[1])]
            c_list = pending_record.setdefault("candidate_file_ids", [])
            if pair_list not in c_list:
                c_list.append(pair_list)
                save_batch_ledger(ledger_path, ledger)

        def safe_cleanup_temp_publish_path_unlocked() -> None:
            """Closes R11 P1 #1: NEVER blindly unlink `temp_publish_path`; verify ownership first."""
            tp_safe, tp_cleanups, _ = inspect_swap_temp_path_for_recovery_unlocked(
                temp_pub_path=temp_publish_path,
                cand_ids=parse_candidate_file_ids(pending_record),
                prev_sha=expected_prev_sha,
                prev_id=expected_prev_id,
            )
            if tp_safe:
                for cp in tp_cleanups:
                    cp.unlink(missing_ok=True)

        def handle_in_process_abort_unlocked(orig_exc: Exception) -> None:
            """Run the EXACT same inode-aware conflict/rollback state machine as restart recovery!"""
            conflict_msg: str | None = None
            cleanups: list[Path] = []
            sub_dirty = False
            try:
                with file_lock(resolved_final):
                    sub_dirty, cleanups, conflict_msg = rollback_aborted_commit_or_detect_conflict_unlocked(
                        photo_id=photo_id,
                        entry=entry,
                        pending=pending_record,
                        target_path=resolved_final,
                        backup_path=backup_path,
                        rollback_race_hook=rollback_race_hook,
                    )
            except Exception:
                raise orig_exc

            if sub_dirty:
                try:
                    save_batch_ledger(ledger_path, ledger)
                    for cp in cleanups:
                        cp.unlink(missing_ok=True)
                except Exception:
                    pass
            elif cleanups:
                for cp in cleanups:
                    cp.unlink(missing_ok=True)

            if conflict_msg:
                raise BridgeError(
                    f"EXPORT RECOVERY CONFLICT: {conflict_msg} (triggered by: {orig_exc})"
                ) from orig_exc
            if "EXPORT RECOVERY CONFLICT" in str(orig_exc):
                raise orig_exc
            raise orig_exc

        if pre_promote_hook is not None:
            pre_promote_hook()

        # 5. PHASE 2: Atomically publish `staged_jpg` -> `resolved_final` (zero partial files, lossless RENAME_SWAP)
        try:
            _, published_id = promote_staged_file(
                staged_jpg,
                resolved_final,
                allow_overwrite_same_owner=allow_overwrite,
                expected_existing_sha256=expected_prev_sha,
                expected_existing_file_id=expected_prev_id,
                temp_publish_path=temp_publish_path,
                expected_new_sha256=sha256_hex,
                expected_new_size=byte_size,
                register_candidate_inode_fn=register_candidate_inode,
                pre_swap_race_hook=pre_swap_race_hook,
                post_swap_before_verify_hook=post_swap_before_verify_hook,
            )
            pending_record["publish_succeeded"] = True
        except Exception as promote_exc:
            handle_in_process_abort_unlocked(promote_exc)

        if post_promote_hook is not None:
            post_promote_hook()

        # 6. PHASE 3 (Closes R7 P1 #2, R8 P1 #3, R9 P1 #3, R9 P2 #4, and R10 P1 #2):
        # Verify `resolved_final` full content SHA-256, size, and `(st_dev, st_ino)` BOTH before AND
        # after `save_batch_ledger` completes!
        prev_owned_snapshot = json.loads(json.dumps(owned_map))
        prev_exported_jpg = entry.get("exported_jpg")
        prev_raw_dims = entry.get("raw_dimensions")
        prev_orient = entry.get("orientation", 0)
        prev_export_verified = entry.get("export_verified", False)
        prev_export_receipt = json.loads(json.dumps(entry.get("export_receipt")))

        def persist_revoked_receipt_and_conflict(reason_code: str, msg_text: str, l_sha: str | None, l_sz: int | None) -> None:
            entry["owned_export_files"] = prev_owned_snapshot
            entry["exported_jpg"] = prev_exported_jpg
            entry["raw_dimensions"] = prev_raw_dims
            entry["orientation"] = prev_orient
            entry["export_verified"] = False
            entry["export_receipt"] = None
            record_pending_conflict_unlocked(
                pending_record,
                reason_code,
                msg_text,
                live_sha256=l_sha,
                live_byte_size=l_sz,
            )
            entry["pending_export_commit"] = pending_record
            try:
                save_batch_ledger(ledger_path, ledger)
            except Exception as rev_exc:
                try:
                    ledger["updated_at"] = datetime.now(timezone.utc).isoformat()
                    atomic_write_text(ledger_path, json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")
                except Exception as fallback_exc:
                    raise BridgeError(
                        f"CRITICAL EXPORT RECOVERY CONFLICT: Failed to persist revoked receipt to {ledger_path} "
                        f"after detecting tampering ({msg_text}); errors: {rev_exc}; fallback: {fallback_exc}"
                    ) from fallback_exc

        try:
            unregister_from_preview_manifest(resolved_final)

            with file_lock(resolved_final):
                post_kind, post_sha, post_size, post_id = inspect_destination_state_unlocked(
                    resolved_final,
                    mid_read_hook=mid_verify_read_hook,
                )
                if (
                    post_kind != "regular"
                    or post_id != published_id
                    or post_sha != sha256_hex
                    or post_size != int(byte_size)
                ):
                    conflict_reason = (
                        "non_regular_destination"
                        if post_kind == "non_regular"
                        else (
                            "destination_removed_before_receipt"
                            if post_kind in ("missing", "removed_during_read")
                            else "external_file_conflict"
                        )
                    )
                    conflict_msg = (
                        f"photo {photo_id!r} at {resolved_final}: destination changed after publish or during "
                        f"verification read (kind={post_kind!r}, live_id={post_id}, expected_id={published_id}, "
                        f"live_sha={str(post_sha)[:12] if post_sha else 'None'}, live_size={post_size}); "
                        f"preserving live destination, backup ({backup_path}), and latched pending_export_commit"
                    )
                    persist_revoked_receipt_and_conflict(conflict_reason, conflict_msg, post_sha, post_size)
                    raise BridgeError(f"EXPORT RECOVERY CONFLICT: {conflict_msg}")

                # Closes R11 P1 #3: Two-stage crash-recoverable receipt commit!
                # Stage 3a: Persist `receipt_pending_post_verify=True` while KEEPING `pending_export_commit`
                # and `export_verified=False`. If a crash occurs during `during_receipt_save_hook` right after
                # an equal-length in-place overwrite, restart recovery sees `pending_export_commit` and
                # re-hashes the full file instead of trusting a stale `export_verified=True`!
                pending_record["receipt_pending_post_verify"] = True
                entry["pending_export_commit"] = pending_record
                save_batch_ledger(ledger_path, ledger)

                if during_receipt_save_hook is not None:
                    during_receipt_save_hook()

                # Pre-finalization full-content SHA-256 continuity check
                pre_fin_kind, pre_fin_sha, pre_fin_size, pre_fin_id = inspect_destination_state_unlocked(
                    resolved_final
                )
                if (
                    pre_fin_kind != "regular"
                    or pre_fin_id != published_id
                    or pre_fin_size != int(byte_size)
                    or pre_fin_sha != sha256_hex
                ):
                    conflict_reason = (
                        "non_regular_destination"
                        if pre_fin_kind == "non_regular"
                        else (
                            "destination_removed_before_receipt"
                            if pre_fin_kind in ("missing", "removed_during_read")
                            else "external_file_conflict"
                        )
                    )
                    conflict_msg = (
                        f"photo {photo_id!r} at {resolved_final}: destination content or identity changed before "
                        f"finalizing receipt (kind={pre_fin_kind!r}, live_id={pre_fin_id}, expected_id={published_id}, "
                        f"live_sha={str(pre_fin_sha)[:12] if pre_fin_sha else 'None'}, "
                        f"expected_sha={sha256_hex[:12]}..., live_size={pre_fin_size}); "
                        "revoking receipt and latching conflict"
                    )
                    persist_revoked_receipt_and_conflict(conflict_reason, conflict_msg, pre_fin_sha, pre_fin_size)
                    raise BridgeError(f"EXPORT RECOVERY CONFLICT: {conflict_msg}")

                # Stage 3b: Finalize receipt on disk and verify full content continuity again after save!
                owned_map[str(resolved_final)] = {
                    "output_path": str(resolved_final),
                    "sha256": sha256_hex,
                    "byte_size": byte_size,
                    "file_id": list(published_id),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                entry["owned_export_files"] = owned_map
                entry["exported_jpg"] = str(resolved_final)
                entry["raw_dimensions"] = export_meta.get("raw_dimensions")
                entry["orientation"] = export_meta.get("orientation", 0)
                entry["export_verified"] = True
                entry["export_receipt"] = {
                    "adjustment_seq": current_seq,
                    "output_path": str(resolved_final),
                    "sha256": sha256_hex,
                    "byte_size": byte_size,
                    "file_id": list(published_id),
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                    "export_meta": export_meta,
                    "sips_info": sips_record,
                }
                entry.pop("pending_export_commit", None)
                save_batch_ledger(ledger_path, ledger)

                # Closes R10 P1 #2: Full content SHA-256 + inode + size continuity verification AFTER
                # final `save_batch_ledger` finishes!
                after_save_kind, after_save_sha, after_save_size, after_save_id = inspect_destination_state_unlocked(
                    resolved_final
                )
                still_bound = (
                    after_save_kind == "regular"
                    and after_save_id == published_id
                    and after_save_size == int(byte_size)
                    and after_save_sha == sha256_hex
                )

                if not still_bound:
                    conflict_reason = (
                        "non_regular_destination"
                        if after_save_kind == "non_regular"
                        else (
                            "destination_removed_before_receipt"
                            if after_save_kind in ("missing", "removed_during_read")
                            else "external_file_conflict"
                        )
                    )
                    conflict_msg = (
                        f"photo {photo_id!r} at {resolved_final}: destination content or identity changed while "
                        f"saving receipt (kind={after_save_kind!r}, live_id={after_save_id}, expected_id={published_id}, "
                        f"live_sha={str(after_save_sha)[:12] if after_save_sha else 'None'}, "
                        f"expected_sha={sha256_hex[:12]}..., live_size={after_save_size}); "
                        "revoking receipt and latching conflict"
                    )
                    persist_revoked_receipt_and_conflict(
                        conflict_reason,
                        conflict_msg,
                        after_save_sha,
                        after_save_size,
                    )
                    raise BridgeError(f"EXPORT RECOVERY CONFLICT: {conflict_msg}")
        except BridgeError as b_exc:
            if "EXPORT RECOVERY CONFLICT" in str(b_exc):
                safe_cleanup_temp_publish_path_unlocked()
                raise
            entry["owned_export_files"] = prev_owned_snapshot
            entry["exported_jpg"] = prev_exported_jpg
            entry["raw_dimensions"] = prev_raw_dims
            entry["orientation"] = prev_orient
            entry["export_verified"] = prev_export_verified
            entry["export_receipt"] = prev_export_receipt
            pending_record["publish_succeeded"] = False
            entry["pending_export_commit"] = pending_record
            handle_in_process_abort_unlocked(b_exc)
        except Exception as commit_exc:
            entry["owned_export_files"] = prev_owned_snapshot
            entry["exported_jpg"] = prev_exported_jpg
            entry["raw_dimensions"] = prev_raw_dims
            entry["orientation"] = prev_orient
            entry["export_verified"] = prev_export_verified
            entry["export_receipt"] = prev_export_receipt
            pending_record["publish_succeeded"] = False
            entry["pending_export_commit"] = pending_record
            handle_in_process_abort_unlocked(commit_exc)

        safe_cleanup_temp_publish_path_unlocked()
        if backup_path and backup_path.exists():
            backup_path.unlink(missing_ok=True)


def finalize_export_in_ledger(
    ledger_path: Path,
    variant_id: str,
    started_adjustment_seq: int,
    final_path: Path,
    sha256_hex: str,
    byte_size: int,
    export_meta: dict[str, Any],
    sips_info: dict[str, Any],
) -> None:
    """Record an already-placed handoff JPG in ledger after verifying regular-file identity."""
    resolved_out = canonicalize_leaf_path(final_path)
    with file_lock(ledger_path):
        ledger = load_batch_ledger(ledger_path)
        entry = resolve_authorized_clone_entry(ledger, variant_id)
        ensure_variant_healthy_for_handoff(entry, "finalize handoff export")

        current_seq = int(entry.get("adjustment_seq") or 0)
        if current_seq != started_adjustment_seq:
            existing_rec = entry.get("export_receipt")
            has_newer_valid_receipt = (
                isinstance(existing_rec, dict)
                and existing_rec.get("adjustment_seq") == current_seq
                and bool(entry.get("export_verified"))
            )
            if not has_newer_valid_receipt:
                with file_lock(resolved_out):
                    kind, live_sha, _, _ = inspect_destination_state_unlocked(resolved_out)
                    if kind == "regular" and live_sha == sha256_hex:
                        resolved_out.unlink(missing_ok=True)
                entry["export_verified"] = False
                save_batch_ledger(ledger_path, ledger)
            raise BridgeError(
                f"Stale export rejected for {entry['photo_id']!r}: variant adjustment_seq changed "
                f"during export ({started_adjustment_seq} -> {current_seq}). Re-export latest state."
            )

        with file_lock(resolved_out):
            kind, live_sha, live_size, live_id = inspect_destination_state_unlocked(resolved_out)
            if kind != "regular" or live_sha != sha256_hex or live_size != int(byte_size):
                raise BridgeError(
                    f"Destination verification failed before finalizing receipt at {resolved_out} "
                    f"(kind={kind!r}, live_sha={live_sha}, live_size={live_size})."
                )

            preserve_receipt_ownership_before_clear(entry)
            owned_map: dict[str, dict[str, Any]] = dict(entry.get("owned_export_files") or {})
            owned_map[str(resolved_out)] = {
                "output_path": str(resolved_out),
                "sha256": sha256_hex,
                "byte_size": byte_size,
                "file_id": list(live_id) if live_id else None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            entry["owned_export_files"] = owned_map
            entry["exported_jpg"] = str(resolved_out)
            entry["raw_dimensions"] = export_meta.get("raw_dimensions")
            entry["orientation"] = export_meta.get("orientation", 0)
            entry["export_verified"] = True
            entry["export_receipt"] = {
                "adjustment_seq": current_seq,
                "output_path": str(resolved_out),
                "sha256": sha256_hex,
                "byte_size": byte_size,
                "file_id": list(live_id) if live_id else None,
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "export_meta": export_meta,
                "sips_info": sips_info,
            }
            save_batch_ledger(ledger_path, ledger)


def is_row_currently_valid_passed(row: dict[str, Any], ledger_entry: dict[str, Any]) -> tuple[bool, str]:
    """Re-verify whether an existing `passed` row in `handoff.jsonl` still matches healthy ledger & disk state."""
    if ledger_entry.get("missing_source"):
        return False, "missing_source"
    if find_unresolved_history_issues(ledger_entry):
        return False, "unresolved_adjustment_failure"
    if not ledger_entry.get("export_verified"):
        return False, "export_invalidated_after_adjustment"
    receipt = ledger_entry.get("export_receipt")
    if not isinstance(receipt, dict):
        return False, "missing_export_receipt"
    current_seq = int(ledger_entry.get("adjustment_seq") or 0)
    receipt_seq = receipt.get("adjustment_seq")
    row_seq = row.get("verified_adjustment_seq")
    if receipt_seq is None or int(receipt_seq) != current_seq:
        return False, "receipt_seq_mismatch"
    if row_seq is None or int(row_seq) != current_seq:
        return False, "row_adjustment_seq_stale"
    if row.get("handoff_valid") is not True:
        return False, "handoff_valid_flag_missing"
    out_str = str(row.get("output_path") or "").strip()
    rec_out = str(receipt.get("output_path") or "").strip()
    if not out_str or not rec_out:
        return False, "output_path_mismatch"
    try:
        out_path = canonicalize_leaf_path(out_str)
        rec_path = canonicalize_leaf_path(rec_out)
    except BridgeError:
        return False, "output_path_invalid"
    if out_path != rec_path:
        return False, "output_path_mismatch"
    kind, live_sha, live_size, live_id = inspect_destination_state_unlocked(out_path)
    if kind == "missing":
        return False, "output_file_missing"
    if kind != "regular":
        return False, "output_file_non_regular"
    rec_id = parse_single_file_id(receipt.get("file_id"))
    if rec_id is not None and live_id != rec_id:
        return False, "output_inode_mismatch"
    if (
        live_sha != receipt.get("sha256")
        or live_size != receipt.get("byte_size")
        or row.get("verified_sha256") != live_sha
    ):
        return False, "output_sha256_mismatch"
    return True, ""


def upsert_handoff_record(
    jsonl_path: Path,
    record: dict[str, Any],
    ledger_path: Path,
) -> dict[str, Any]:
    """Verify variant health, live SHA-256, `adjustment_seq`, and re-audit all rows in `handoff.jsonl`."""
    photo_id = str(record.get("photo_id") or "").strip()
    if not photo_id:
        raise ValueError("handoff record requires non-empty 'photo_id'")
    status = record.get("status")
    if status not in ("passed", "needs_review", "failed"):
        raise ValueError(f"Invalid handoff status {status!r}; expected passed, needs_review, or failed")

    resolved_jsonl = str(jsonl_path.expanduser().resolve())

    with file_lock(ledger_path):
        ledger = load_batch_ledger(ledger_path)
        photos = ledger.get("photos") or {}
        if photo_id not in photos:
            raise BridgeError(f"photo_id {photo_id!r} is not part of batch ledger {ledger_path}")

        handoff_files: list[str] = list(ledger.get("handoff_files") or [])
        if resolved_jsonl not in handoff_files:
            handoff_files.append(resolved_jsonl)
            ledger["handoff_files"] = handoff_files

        ledger_entry = photos[photo_id]
        if ledger_entry.get("missing_source") and status == "passed":
            raise BridgeError(f"Cannot mark {photo_id!r} as 'passed': source RAW was missing from batch.")

        record_to_save = dict(record)
        if status == "passed":
            ensure_variant_healthy_for_handoff(ledger_entry, "mark handoff status as 'passed'")

            out_path_str = str(record.get("output_path") or "").strip()
            if not out_path_str:
                raise BridgeError(f"Cannot mark {photo_id!r} as 'passed' without non-empty 'output_path'.")
            out_path = canonicalize_leaf_path(out_path_str)
            out_kind, live_sha, live_size, live_id = inspect_destination_state_unlocked(out_path)
            if out_kind == "non_regular":
                raise BridgeError(
                    f"Cannot mark {photo_id!r} as 'passed': output_path is a symlink or non-regular file at {out_path}"
                )
            if out_kind != "regular" or not live_size:
                raise BridgeError(f"Cannot mark {photo_id!r} as 'passed': JPG file missing, empty, or changing at {out_path} ({out_kind})")

            receipt = ledger_entry.get("export_receipt")
            if not ledger_entry.get("export_verified") or not isinstance(receipt, dict):
                raise BridgeError(
                    f"Cannot mark {photo_id!r} as 'passed': batch ledger has no verified export_receipt."
                )

            current_seq = int(ledger_entry.get("adjustment_seq") or 0)
            receipt_seq = receipt.get("adjustment_seq")
            if receipt_seq is None or int(receipt_seq) != current_seq:
                raise BridgeError(
                    f"Cannot mark {photo_id!r} as 'passed': export_receipt adjustment_seq ({receipt_seq}) "
                    f"does not match current variant adjustment_seq ({current_seq})."
                )

            recorded_export = receipt.get("output_path") or ledger_entry.get("exported_jpg")
            if not recorded_export or canonicalize_leaf_path(str(recorded_export)) != out_path:
                raise BridgeError(
                    f"Output path mismatch for {photo_id!r}: record has {out_path}, "
                    f"ledger verified export is {recorded_export}"
                )

            rec_id = parse_single_file_id(receipt.get("file_id"))
            if rec_id is not None and live_id != rec_id:
                raise BridgeError(
                    f"File identity mismatch (inode) for {photo_id!r} at {out_path}: live inode {live_id} != "
                    f"verified receipt inode {rec_id} (file was replaced!)."
                )

            if live_sha != receipt.get("sha256") or live_size != receipt.get("byte_size"):
                raise BridgeError(
                    f"File identity mismatch for {photo_id!r} at {out_path}: live SHA-256/size "
                    f"({live_sha[:12]}.../{live_size}) != verified export receipt "
                    f"({str(receipt.get('sha256'))[:12]}.../{receipt.get('byte_size')}). "
                    "File was modified or replaced after export!"
                )

            persisted_export_meta = receipt.get("export_meta")
            if not isinstance(persisted_export_meta, dict):
                raise BridgeError(f"Missing persisted recipe read-back export_meta in receipt for {photo_id!r}")

            sips_info = inspect_jpg_with_sips(out_path)
            gate_errors = validate_handoff_export(persisted_export_meta, sips_info)
            if gate_errors:
                raise BridgeError(
                    f"Cannot mark {photo_id!r} as 'passed'; JPG failed validation: {'; '.join(gate_errors)}"
                )

            record_to_save["output_path"] = str(out_path)
            record_to_save["verified_adjustment_seq"] = current_seq
            record_to_save["verified_sha256"] = live_sha
            record_to_save["handoff_valid"] = True
            record_to_save.pop("invalidated_reason", None)
            record_to_save.pop("invalidated_at_adjustment_seq", None)
        else:
            record_to_save["handoff_valid"] = True
            record_to_save["verified_adjustment_seq"] = int(ledger_entry.get("adjustment_seq") or 0)

        with file_lock(jsonl_path):
            existing_rows: list[dict[str, Any]] = []
            if jsonl_path.is_file():
                for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        existing_rows.append(json.loads(line))

            updated = False
            for idx, row in enumerate(existing_rows):
                if str(row.get("photo_id")) == photo_id:
                    existing_rows[idx] = record_to_save
                    updated = True
                    break
            if not updated:
                existing_rows.append(record_to_save)

            for idx, row in enumerate(existing_rows):
                r_pid = str(row.get("photo_id") or "")
                if r_pid in photos and row.get("status") == "passed":
                    still_ok, reason = is_row_currently_valid_passed(row, photos[r_pid])
                    if not still_ok:
                        demoted = dict(row)
                        demoted["previous_status"] = "passed"
                        demoted["status"] = "stale"
                        demoted["handoff_valid"] = False
                        demoted["invalidated_reason"] = reason
                        demoted["invalidated_at_adjustment_seq"] = int(photos[r_pid].get("adjustment_seq") or 0)
                        existing_rows[idx] = demoted
                        photos[r_pid]["handoff_status"] = "stale"

            atomic_write_text(
                jsonl_path,
                "\n".join(json.dumps(r, ensure_ascii=False) for r in existing_rows) + "\n",
            )

        ledger_entry["handoff_status"] = status
        save_batch_ledger(ledger_path, ledger)

        expected_ids = sorted(photos.keys())
        recorded_by_id = {str(r.get("photo_id")): r for r in existing_rows}
        missing_ids = [pid for pid in expected_ids if pid not in recorded_by_id]
        missing_source_ids = [pid for pid, p in photos.items() if p.get("missing_source")]
        unexpected_ids = [pid for pid in recorded_by_id if pid not in photos]
        stale_ids = [str(r.get("photo_id")) for r in existing_rows if r.get("status") == "stale"]

        counts: dict[str, Any] = {
            "expected_total": len(expected_ids),
            "recorded_total": len(existing_rows),
            "passed": 0,
            "needs_review": 0,
            "failed": 0,
            "stale": len(stale_ids),
            "stale_photo_ids": sorted(stale_ids),
            "missing_count": len(missing_ids),
            "missing_photo_ids": missing_ids,
            "missing_source_photo_ids": sorted(missing_source_ids),
            "unexpected_photo_ids": unexpected_ids,
        }
        for r in existing_rows:
            st = r.get("status")
            if st in ("passed", "needs_review", "failed"):
                counts[st] += 1

        valid_terminal_sum = counts["passed"] + counts["needs_review"] + counts["failed"]
        counts["balanced"] = (
            counts["stale"] == 0
            and counts["missing_count"] == 0
            and len(missing_source_ids) == 0
            and len(unexpected_ids) == 0
            and valid_terminal_sum == counts["expected_total"]
            and counts["recorded_total"] == counts["expected_total"]
        )
        return {
            "handoff_path": str(jsonl_path),
            "photo_id": photo_id,
            "updated_existing": updated,
            "summary": counts,
        }


def load_json_arg(spec_arg: str) -> dict[str, Any]:
    if spec_arg.startswith("@"):
        return json.loads(Path(spec_arg[1:]).expanduser().read_text(encoding="utf-8"))
    return json.loads(spec_arg)


def register_batch_entries(
    ledger_path: Path,
    c1_variants: list[dict[str, Any]],
    raw_dir: Path | None = None,
    selected_ids: list[str] | set[str] | None = None,
    record_missing_selected: bool = False,
) -> dict[str, Any]:
    """Create or resume a batch state ledger, preserving distinct filenames across same-stem extensions."""
    ordered_selected: list[str] | None = None
    if selected_ids is not None:
        seen_sel: set[str] = set()
        ordered_selected = []
        for raw_item in selected_ids:
            cleaned = str(raw_item).strip()
            if cleaned and cleaned not in seen_sel:
                seen_sel.add(cleaned)
                ordered_selected.append(cleaned)
        if not ordered_selected:
            raise BridgeError("selected_ids was provided but contained no non-empty IDs.")

    with file_lock(ledger_path):
        if ledger_path.is_file():
            ledger = load_batch_ledger(ledger_path)
        else:
            ledger = {
                "batch_id": datetime.now(timezone.utc).strftime("batch-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "handoff_files": [],
                "photos": {},
            }

        by_raw_path: dict[str, dict[str, Any]] = {}
        for v in sorted(c1_variants, key=lambda item: int(item.get("position") or 1)):
            rp = str(v.get("raw_path") or "")
            if not rp:
                continue
            norm_rp = str(Path(rp).resolve()) if Path(rp).exists() else rp
            if norm_rp not in by_raw_path:
                by_raw_path[norm_rp] = v

        candidate_files: list[Path] = []
        if raw_dir is not None:
            resolved_dir = raw_dir.expanduser().resolve(strict=True)
            for child in sorted(resolved_dir.iterdir()):
                if child.is_file() and not child.is_symlink() and child.suffix.lower() in RAW_EXTENSIONS:
                    candidate_files.append(child.resolve())
        else:
            for norm_rp in sorted(by_raw_path):
                candidate_files.append(Path(norm_rp))

        expected_files: list[Path] = []
        missing_selected: list[str] = []
        ambiguous_selected: dict[str, list[str]] = {}

        if ordered_selected is not None:
            seen_paths: set[str] = set()
            for req in ordered_selected:
                exact_name_hits = [fp for fp in candidate_files if fp.name == req]
                hits = exact_name_hits if exact_name_hits else [fp for fp in candidate_files if fp.stem == req]
                if not hits:
                    missing_selected.append(req)
                elif len(hits) > 1:
                    ambiguous_selected[req] = [str(h) for h in hits]
                else:
                    hit_str = str(hits[0])
                    if hit_str not in seen_paths:
                        seen_paths.add(hit_str)
                        expected_files.append(hits[0])
            if ambiguous_selected:
                raise BridgeError(
                    f"Ambiguous selected photo ID(s) matched multiple RAW files: {ambiguous_selected}"
                )
            if missing_selected and not record_missing_selected:
                raise BridgeError(
                    f"Selected photo(s) missing from RAW source: {missing_selected}"
                )
        else:
            expected_files = candidate_files

        if not expected_files and not missing_selected:
            raise BridgeError("No matching RAW photos found to initialize batch.")

        photos = ledger["photos"]
        stem_counts: dict[str, int] = {}
        name_counts: dict[str, int] = {}
        existing_raw_paths = {
            str(entry.get("raw_path")): pid
            for pid, entry in photos.items()
            if entry.get("raw_path") and not entry.get("missing_source")
        }
        existing_missing_tokens = {
            str(entry.get("requested_token") or pid): pid
            for pid, entry in photos.items()
            if entry.get("missing_source")
        }

        for fp in expected_files:
            stem_counts[fp.stem] = stem_counts.get(fp.stem, 0) + 1
            name_counts[fp.name] = name_counts.get(fp.name, 0) + 1
        for req in missing_selected:
            req_stem = Path(req).stem or req
            req_name = Path(req).name or req
            stem_counts[req_stem] = stem_counts.get(req_stem, 0) + 1
            name_counts[req_name] = name_counts.get(req_name, 0) + 1

        unmatched_files: list[str] = []
        for fp in expected_files:
            key_rp = str(fp)
            matched_var = by_raw_path.get(key_rp)
            if matched_var is None:
                unmatched_files.append(key_rp)
                continue
            if key_rp in existing_raw_paths:
                continue

            upgraded_placeholder_pid: str | None = None
            if fp.name in photos and photos[fp.name].get("missing_source"):
                upgraded_placeholder_pid = fp.name
            elif stem_counts[fp.stem] == 1 and fp.stem in photos and photos[fp.stem].get("missing_source"):
                req_tok = str(photos[fp.stem].get("requested_token") or fp.stem)
                if req_tok in (fp.stem, fp.name):
                    upgraded_placeholder_pid = fp.stem

            if upgraded_placeholder_pid is not None:
                photo_id = upgraded_placeholder_pid
            elif stem_counts[fp.stem] == 1 and fp.stem not in photos:
                photo_id = fp.stem
            elif name_counts[fp.name] == 1 and fp.name not in photos:
                photo_id = fp.name
            else:
                photo_id = f"{fp.parent.name}__{fp.name}"
                if photo_id in photos and str(photos[photo_id].get("raw_path")) != key_rp:
                    raise BridgeError(
                        f"Photo ID collision in batch ledger for {photo_id!r}: "
                        f"{photos[photo_id].get('raw_path')} vs {key_rp}"
                    )

            photos[photo_id] = {
                "photo_id": photo_id,
                "raw_path": key_rp,
                "source_variant_id": str(matched_var["variant_id"]),
                "raw_dimensions": matched_var.get("dimensions"),
                "orientation": 0,
                "missing_source": False,
                "cloned_variant_id": None,
                "clone_token": uuid.uuid4().hex[:6],
                "inherited_layers": [],
                "managed_layers": {},
                "managed_layer_records": {},
                "owned_export_files": {},
                "adjustment_seq": 0,
                "retouch_mode": None,
                "history": [],
                "exported_jpg": None,
                "export_verified": False,
                "export_receipt": None,
                "handoff_status": None,
            }

        for req in missing_selected:
            if req in existing_missing_tokens:
                continue
            req_name = Path(req).name or req
            req_stem = Path(req).stem or req
            has_ext = bool(Path(req).suffix)
            if not has_ext and stem_counts.get(req_stem, 0) == 1 and req_stem not in photos:
                pid = req_stem
            elif req_name not in photos:
                pid = req_name
            else:
                pid = f"missing__{req}"

            photos[pid] = {
                "photo_id": pid,
                "requested_token": req,
                "raw_path": None,
                "source_variant_id": None,
                "raw_dimensions": None,
                "orientation": 0,
                "missing_source": True,
                "cloned_variant_id": None,
                "clone_token": uuid.uuid4().hex[:6],
                "inherited_layers": [],
                "managed_layers": {},
                "managed_layer_records": {},
                "owned_export_files": {},
                "adjustment_seq": 0,
                "retouch_mode": None,
                "history": [],
                "exported_jpg": None,
                "export_verified": False,
                "export_receipt": None,
                "handoff_status": None,
            }

        if unmatched_files:
            raise BridgeError(
                f"{len(unmatched_files)} RAW file(s) on disk are not loaded in current Capture One collection: "
                f"{unmatched_files[:5]}"
            )

        save_batch_ledger(ledger_path, ledger)
        return ledger


def extract_spec_from_state(
    state: dict[str, Any],
    managed_layers: dict[str, str] | None = None,
    include_layer_wb: bool = False,
) -> dict[str, Any]:
    """Convert a serialized variant state back into an `apply` spec snapshot for managed layers."""
    g_adj = state.get("global_adjustments") or {}
    global_clean = {
        k: g_adj[k]
        for k in ADJUSTMENT_PROP_MAP
        if k in g_adj and is_recoverable_number(g_adj[k])
    }

    if managed_layers is not None:
        layer_name_to_key = {v: k for k, v in managed_layers.items()}
    else:
        layer_name_to_key = {v["default_name"]: k for k, v in MASK_SPECS.items()}

    masks_clean: dict[str, Any] = {}
    for lyr in (state.get("layers") or []):
        m_key = layer_name_to_key.get(str(lyr.get("name") or ""))
        if not m_key:
            continue
        l_adj = lyr.get("adjustments") or {}
        m_cfg: dict[str, Any] = {
            "enabled": bool(lyr.get("enabled", True)),
            "opacity": int(round(float(lyr.get("opacity", 100)))),
        }
        for k in ADJUSTMENT_PROP_MAP:
            if k in ("temperature", "tint") and not include_layer_wb:
                continue
            if k in l_adj and is_recoverable_number(l_adj[k]):
                m_cfg[k] = l_adj[k]
        masks_clean[m_key] = m_cfg
    return {"global": global_clean, "masks": masks_clean}


def execute_apply_transaction(
    ledger_path: Path,
    variant_id: str,
    spec: dict[str, Any],
    note: str = "",
    tolerance_scale: float = 1.0,
    allow_layer_wb: bool = False,
    runner_fn=run_applescript,
) -> dict[str, Any]:
    """Execute `apply` with pre-write tolerance, health, layer-identity, and recoverable-original checks."""
    scale = validate_tolerance_scale(tolerance_scale)

    with file_lock(ledger_path):
        ledger = load_batch_ledger(ledger_path)
        entry = resolve_authorized_clone_entry(ledger, variant_id)
        photo_id = str(entry["photo_id"])

        ensure_variant_healthy_for_handoff(entry, "apply new adjustments")

        existing_managed: dict[str, str] = dict(entry.get("managed_layers") or {})
        managed_records: dict[str, dict[str, Any]] = dict(entry.get("managed_layer_records") or {})
        inherited_layers: list[dict[str, Any]] = list(entry.get("inherited_layers") or [])
        clone_token = str(entry.get("clone_token") or uuid.uuid4().hex[:6])
        entry["clone_token"] = clone_token
        ever_used_layer_wb = bool(entry.get("layer_wb_used") or allow_layer_wb)

        before_state = json.loads(runner_fn(build_inspect_script(), [variant_id]))

        verify_managed_layers_before_write(
            existing_managed_layers=existing_managed,
            managed_layer_records=managed_records,
            inherited_layers=inherited_layers,
            current_layers=before_state.get("layers") or [],
            tolerance_scale=scale,
        )

        layer_plan = allocate_managed_layer_plan(
            masks_spec=spec.get("masks") or {},
            existing_managed_layers=existing_managed,
            inherited_layers=inherited_layers,
            current_layers=before_state.get("layers") or [],
            clone_token=clone_token,
        )
        script = build_apply_script(spec, layer_plan=layer_plan, allow_layer_wb=allow_layer_wb)

        verify_recoverable_originals_before_write(
            spec=spec,
            existing_managed_layers=existing_managed,
            before_state=before_state,
        )

        if existing_managed and not managed_records:
            managed_records = snapshot_managed_layer_records(existing_managed, before_state)

        planned_managed = dict(existing_managed)
        for m_key, (l_name, _) in layer_plan.items():
            planned_managed[m_key] = l_name

        preserve_receipt_ownership_before_clear(entry)

        new_seq = int(entry.get("adjustment_seq") or 0) + 1
        wal_entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "note": note,
            "status": "in_flight",
            "allow_layer_wb": ever_used_layer_wb,
            "requested_spec": spec,
            "planned_managed_layers": planned_managed,
            "previous_managed_layers": existing_managed,
            "previous_managed_layer_records": managed_records,
            "after_managed_layer_records": {},
            "before_spec_snapshot": extract_spec_from_state(
                before_state,
                managed_layers=existing_managed,
                include_layer_wb=ever_used_layer_wb,
            ),
            "verified": False,
            "mismatches": [],
        }
        entry.setdefault("history", []).append(wal_entry)
        entry["layer_wb_used"] = ever_used_layer_wb
        entry["adjustment_seq"] = new_seq
        entry["export_verified"] = False
        entry["export_receipt"] = None
        if entry.get("handoff_status") == "passed":
            entry["handoff_status"] = "stale"
        save_batch_ledger(ledger_path, ledger)

        invalidate_photo_in_linked_handoff_files(
            ledger=ledger,
            photo_id=photo_id,
            new_adjustment_seq=new_seq,
            reason="adjusted_after_handoff",
        )

        try:
            raw = runner_fn(script, [variant_id])
            after_state = json.loads(raw)
        except Exception as exc:
            wal_entry["status"] = "failed_mid_apply"
            wal_entry["error"] = str(exc)
            entry["managed_layers"] = planned_managed
            save_batch_ledger(ledger_path, ledger)
            raise

        entry["managed_layers"] = planned_managed
        post_records = snapshot_managed_layer_records(planned_managed, after_state)
        wal_entry["after_managed_layer_records"] = post_records

        rb_ok, rb_mismatches = verify_readback(
            spec,
            after_state,
            tolerance_scale=scale,
            managed_layer_names=planned_managed,
        )
        inh_ok, inh_violations = verify_inherited_layers_preserved(inherited_layers, after_state)
        all_mismatches = rb_mismatches + inh_violations
        verified = rb_ok and inh_ok

        if verified:
            entry["managed_layer_records"] = post_records

        wal_entry["status"] = "applied" if verified else "verification_failed"
        wal_entry["verified"] = verified
        wal_entry["mismatches"] = all_mismatches
        entry["orientation"] = after_state.get("orientation", entry.get("orientation", 0))
        save_batch_ledger(ledger_path, ledger)

        return {
            "photo_id": photo_id,
            "variant_id": variant_id,
            "adjustment_seq": entry["adjustment_seq"],
            "verified": verified,
            "mismatches": all_mismatches,
            "managed_layers": planned_managed,
            "round": len(entry["history"]),
            "state": after_state,
        }


def execute_rollback_transaction(
    ledger_path: Path,
    photo_id: str,
    tolerance_scale: float = 1.0,
    runner_fn=run_applescript,
) -> dict[str, Any]:
    """Roll back the latest `apply` round (deleting newly created layers & verifying lattice resume)."""
    scale = validate_tolerance_scale(tolerance_scale)

    with file_lock(ledger_path):
        ledger = load_batch_ledger(ledger_path)
        photos = ledger["photos"]
        if photo_id not in photos:
            raise BridgeError(f"photo_id {photo_id!r} not found in batch ledger")
        entry = photos[photo_id]
        clone_id = str(entry.get("cloned_variant_id") or "")
        resolve_authorized_clone_entry(ledger, clone_id)
        history = entry.get("history") or []
        if not history:
            raise BridgeError(f"No adjustment history to roll back for {photo_id!r}")

        target_wal = history[-1]
        restore_spec = json.loads(json.dumps(target_wal["before_spec_snapshot"]))
        prev_managed: dict[str, str] = dict(target_wal.get("previous_managed_layers") or {})
        planned_managed: dict[str, str] = dict(
            target_wal.get("planned_managed_layers") or entry.get("managed_layers") or {}
        )
        inherited_layers: list[dict[str, Any]] = list(entry.get("inherited_layers") or [])
        allow_wb_restore = bool(target_wal.get("allow_layer_wb") or entry.get("layer_wb_used"))

        live_before_rb = json.loads(runner_fn(build_inspect_script(), [clone_id]))
        verify_managed_layers_before_rollback(
            target_wal=target_wal,
            inherited_layers=inherited_layers,
            current_layers=live_before_rb.get("layers") or [],
            tolerance_scale=scale,
        )

        live_layer_names = {
            str(lyr.get("name") or "")
            for lyr in (live_before_rb.get("layers") or [])
            if isinstance(lyr, dict)
        }

        # Delete newly created layers from this round completely (eliminates any raster feather/refine changes!)
        for m_key, m_name in planned_managed.items():
            if m_key not in prev_managed and m_name in live_layer_names:
                restore_spec.setdefault("masks", {})[m_key] = {"_delete": True}

        rollback_layer_plan = {
            m_key: (planned_managed[m_key], True)
            for m_key in (restore_spec.get("masks") or {})
            if m_key in planned_managed
        }

        script = build_apply_script(
            restore_spec,
            layer_plan=rollback_layer_plan,
            allow_layer_wb=allow_wb_restore,
        )

        preserve_receipt_ownership_before_clear(entry)

        if target_wal.get("status") != "rollback_in_flight":
            target_wal["status_before_rollback"] = target_wal.get("status")
        target_wal["status"] = "rollback_in_flight"
        target_wal["verified"] = False

        new_seq = int(entry.get("adjustment_seq") or 0) + 1
        entry["adjustment_seq"] = new_seq
        entry["export_verified"] = False
        entry["export_receipt"] = None
        if entry.get("handoff_status") == "passed":
            entry["handoff_status"] = "stale"
        save_batch_ledger(ledger_path, ledger)

        invalidate_photo_in_linked_handoff_files(
            ledger=ledger,
            photo_id=photo_id,
            new_adjustment_seq=new_seq,
            reason="rolled_back_after_handoff",
        )

        try:
            restored_state = json.loads(runner_fn(script, [clone_id]))
        except Exception as exc:
            target_wal["status"] = "rollback_failed"
            target_wal["verified"] = False
            target_wal["rollback_error"] = [str(exc)]
            save_batch_ledger(ledger_path, ledger)
            raise

        rb_ok, rb_mismatches = verify_readback(
            restore_spec,
            restored_state,
            tolerance_scale=scale,
            managed_layer_names=planned_managed,
        )
        inh_ok, inh_violations = verify_inherited_layers_preserved(inherited_layers, restored_state)
        all_mismatches = rb_mismatches + inh_violations
        if not (rb_ok and inh_ok):
            target_wal["status"] = "rollback_failed"
            target_wal["verified"] = False
            target_wal["rollback_error"] = all_mismatches
            save_batch_ledger(ledger_path, ledger)
            raise BridgeError(
                f"Rollback verification failed for {photo_id!r}: {'; '.join(all_mismatches)}"
            )

        history.pop()
        active_managed = {
            m_key: l_name
            for m_key, l_name in prev_managed.items()
            if any(str(lyr.get("name") or "") == l_name for lyr in (restored_state.get("layers") or []))
        }
        entry["managed_layers"] = active_managed
        entry["managed_layer_records"] = snapshot_managed_layer_records(active_managed, restored_state)
        entry["orientation"] = restored_state.get("orientation", entry.get("orientation", 0))
        save_batch_ledger(ledger_path, ledger)

        return {
            "photo_id": photo_id,
            "variant_id": clone_id,
            "adjustment_seq": entry["adjustment_seq"],
            "rolled_back": True,
            "remaining_rounds": len(history),
            "state": restored_state,
        }


def execute_export_workflow(
    mode: str,
    variant_id: str,
    recipe: str,
    output_dir: Path,
    batch_state_path: Path | None = None,
    suffix: str | None = None,
    timeout: float = 90.0,
    overwrite_own: bool = False,
    versioned: bool = False,
    with_metrics: bool = False,
    export_runner_fn=None,
    pre_promote_hook=None,
    post_promote_hook=None,
    pre_swap_race_hook=None,
    post_swap_before_verify_hook=None,
    mid_verify_read_hook=None,
    during_receipt_save_hook=None,
    rollback_race_hook=None,
) -> dict[str, Any]:
    """Core export workflow with inode-bound crash-consistent commit and fail-closed preview check."""
    expanded_out = output_dir.expanduser()
    expanded_out.mkdir(mode=0o700, parents=True, exist_ok=True)
    out_dir = expanded_out.resolve()

    ledger_path: Path | None = batch_state_path.expanduser().resolve() if batch_state_path else None
    if mode == "handoff" and ledger_path is None:
        raise BridgeError("Formal `--mode handoff` export requires `--batch-state` to verify clone ownership.")

    started_seq = 0
    photo_id_hint = ""
    protected_handoff_paths: set[Path] = set()

    if ledger_path is not None:
        with file_lock(ledger_path):
            ledger = load_batch_ledger(ledger_path)
            entry = resolve_authorized_clone_entry(ledger, variant_id)
            if mode == "handoff":
                ensure_variant_healthy_for_handoff(entry, "run formal handoff export")

            started_seq = int(entry.get("adjustment_seq") or 0)
            photo_id_hint = str(entry.get("photo_id") or "")

            for p_entry in (ledger.get("photos") or {}).values():
                if p_entry.get("exported_jpg"):
                    protected_handoff_paths.add(canonicalize_leaf_path(str(p_entry["exported_jpg"])))
                for owned_p in (p_entry.get("owned_export_files") or {}):
                    protected_handoff_paths.add(canonicalize_leaf_path(str(owned_p)))
                rec = p_entry.get("export_receipt")
                if isinstance(rec, dict) and rec.get("output_path"):
                    protected_handoff_paths.add(canonicalize_leaf_path(str(rec["output_path"])))

    staging_dir = Path(tempfile.mkdtemp(prefix=".c1_staging_", dir=str(out_dir)))
    exact_token = f"c1exp_{uuid.uuid4().hex[:12]}"
    try:
        if export_runner_fn is not None:
            export_meta, staged_jpg = export_runner_fn(variant_id, recipe, staging_dir, exact_token)
        else:
            raw = run_applescript(
                build_export_script(),
                [variant_id, recipe, str(staging_dir), exact_token],
            )
            export_meta = json.loads(raw)
            job_id = str(export_meta.get("job_id") or "")
            if job_id.startswith("ERROR"):
                raise BridgeError(f"Capture One process command returned error: {job_id}")

            def is_job_still_queued() -> bool:
                if not job_id:
                    return False
                res_txt = run_applescript(build_check_job_script(), [job_id], timeout=15)
                return res_txt.strip().lower() == "true"

            staged_jpg = wait_for_staged_jpg(
                staging_dir=staging_dir,
                exact_stem=exact_token,
                timeout_sec=timeout,
                job_in_queue_fn=is_job_still_queued,
            )

        sips_info = inspect_jpg_with_sips(staged_jpg)
        if mode == "handoff":
            gate_errors = validate_handoff_export(export_meta, sips_info)
            if gate_errors:
                raise BridgeError("Handoff JPG failed validation gates: " + "; ".join(gate_errors))

        staged_sha256, staged_size = compute_file_sha256(staged_jpg)
        raw_stem = (
            photo_id_hint
            or Path(export_meta.get("image_name") or "").stem
            or Path(export_meta.get("raw_path") or "").stem
        )
        effective_suffix = suffix
        if versioned:
            ver_tag = f"v{started_seq}"
            effective_suffix = f"{suffix}_{ver_tag}" if suffix else ver_tag
        final_stem = f"{raw_stem}_{effective_suffix}" if effective_suffix else raw_stem
        final_filename = validate_safe_leaf_filename(f"{final_stem}.jpg")
        # Closes R8 P2 #4: `out_dir` is already resolved; NEVER call `.resolve()` on `final_path` leaf!
        final_path = out_dir / final_filename

        if mode == "preview":
            if ledger_path is not None:
                with file_lock(ledger_path):
                    ledger = load_batch_ledger(ledger_path)
                    for p_entry in (ledger.get("photos") or {}).values():
                        if p_entry.get("exported_jpg"):
                            protected_handoff_paths.add(canonicalize_leaf_path(str(p_entry["exported_jpg"])))
                        for owned_p in (p_entry.get("owned_export_files") or {}):
                            protected_handoff_paths.add(canonicalize_leaf_path(str(owned_p)))

            if final_path in protected_handoff_paths:
                raise BridgeError(
                    f"SAFETY BLOCK: Preview export refuses to overwrite formal handoff JPG at {final_path}"
                )
            prev_kind, _, _, _ = inspect_destination_state_unlocked(final_path)
            if prev_kind == "non_regular":
                raise BridgeError(
                    f"SAFETY BLOCK: Preview export refuses to write to symlink or non-regular path at {final_path}"
                )
            if prev_kind not in ("missing", "regular"):
                raise BridgeError(
                    f"SAFETY BLOCK: Preview destination {final_path} is changing concurrently ({prev_kind})"
                )
            allow_overwrite = False
            expected_prev_sha: str | None = None
            if prev_kind == "regular" and not versioned:
                verify_exclusive_output_dir(out_dir)
                manifest_lock_target = out_dir / PREVIEW_MANIFEST_FILENAME
                with file_lock(manifest_lock_target):
                    prev_manifest = load_preview_manifest(out_dir)
                    recorded_prev_sha = prev_manifest.get(final_path.name)
                if not recorded_prev_sha:
                    raise BridgeError(
                        f"SAFETY BLOCK: Preview export refuses to overwrite non-preview existing file at {final_path}"
                    )
                allow_overwrite = True
                expected_prev_sha = recorded_prev_sha

            if pre_promote_hook is not None:
                pre_promote_hook()

            _, prev_published_id = promote_staged_file(
                staged_jpg,
                final_path,
                allow_overwrite_same_owner=allow_overwrite,
                expected_existing_sha256=expected_prev_sha,
                expected_new_sha256=staged_sha256,
                expected_new_size=staged_size,
                pre_swap_race_hook=pre_swap_race_hook,
            )

            if post_promote_hook is not None:
                post_promote_hook()

            manifest_lock_target = out_dir / PREVIEW_MANIFEST_FILENAME
            with file_lock(manifest_lock_target):
                post_prev_kind, post_prev_sha, post_prev_size, post_prev_id = inspect_destination_state_unlocked(
                    final_path,
                    mid_read_hook=mid_verify_read_hook,
                )
                # Closes R9 P2 #5: Fail closed immediately (preserving live destination) if preview file was
                # replaced, altered, or removed after publish! NEVER return staged hash with a mismatched file!
                if (
                    post_prev_kind != "regular"
                    or post_prev_id != prev_published_id
                    or post_prev_sha != staged_sha256
                    or post_prev_size != staged_size
                ):
                    raise BridgeError(
                        f"Preview export post-publish verification failed at {final_path} "
                        f"(kind={post_prev_kind!r}, live_id={post_prev_id}, expected_id={prev_published_id}, "
                        f"live_sha={str(post_prev_sha)[:12] if post_prev_sha else 'None'}); "
                        "preserving destination untouched and aborting."
                    )
                prev_manifest = load_preview_manifest(out_dir)
                prev_manifest[final_path.name] = staged_sha256
                save_preview_manifest(out_dir, prev_manifest)
        else:
            assert ledger_path is not None
            commit_handoff_export_atomically(
                ledger_path=ledger_path,
                variant_id=variant_id,
                started_adjustment_seq=started_seq,
                staged_jpg=staged_jpg,
                final_path=final_path,
                overwrite_own=(overwrite_own and not versioned),
                sha256_hex=staged_sha256,
                byte_size=staged_size,
                export_meta=export_meta,
                sips_info=sips_info,
                pre_promote_hook=pre_promote_hook,
                post_promote_hook=post_promote_hook,
                pre_swap_race_hook=pre_swap_race_hook,
                post_swap_before_verify_hook=post_swap_before_verify_hook,
                mid_verify_read_hook=mid_verify_read_hook,
                during_receipt_save_hook=during_receipt_save_hook,
                rollback_race_hook=rollback_race_hook,
            )

        sips_info["path"] = str(final_path)
        sips_info["sha256"] = staged_sha256
        sips_info["byte_size"] = staged_size
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    result: dict[str, Any] = {
        "mode": mode,
        "export": export_meta,
        "file": sips_info,
    }
    if with_metrics:
        pm = load_preview_metrics_module()
        with TemporaryDirectory(prefix="c1-bridge-metrics-") as td:
            result["metrics"] = pm.measure(Path(sips_info["path"]), Path(td))

    return result


def cmd_status(args: argparse.Namespace) -> int:
    browse_dir = str(Path(args.browse).expanduser().resolve()) if args.browse else ""
    raw = run_applescript(build_status_script(), [browse_dir])
    print(json.dumps(json.loads(raw), ensure_ascii=False, indent=2))
    return 0


def cmd_init_batch(args: argparse.Namespace) -> int:
    ledger_path = Path(args.batch_state).expanduser().resolve()
    browse_dir = str(Path(args.raw_dir).expanduser().resolve()) if (args.raw_dir and args.browse) else ""
    status_data = json.loads(run_applescript(build_status_script(), [browse_dir]))
    if not status_data.get("has_document"):
        raise BridgeError("No open Capture One document.")

    selected_ids: list[str] | None = None
    if args.selected_ids:
        selected_ids = [s.strip() for s in args.selected_ids.split(",") if s.strip()]

    raw_dir = Path(args.raw_dir) if args.raw_dir else None
    ledger = register_batch_entries(
        ledger_path=ledger_path,
        c1_variants=status_data.get("variants") or [],
        raw_dir=raw_dir,
        selected_ids=selected_ids,
        record_missing_selected=args.record_missing_selected,
    )
    missing_sources = sorted(pid for pid, p in ledger["photos"].items() if p.get("missing_source"))
    print(json.dumps({
        "batch_state": str(ledger_path),
        "batch_id": ledger["batch_id"],
        "expected_total": len(ledger["photos"]),
        "missing_source_photo_ids": missing_sources,
        "photo_ids": sorted(ledger["photos"].keys()),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    raw = run_applescript(build_inspect_script(), [args.variant_id])
    print(json.dumps(json.loads(raw), ensure_ascii=False, indent=2))
    return 0


def cmd_prepare_variant(args: argparse.Namespace) -> int:
    ledger_path = Path(args.batch_state).expanduser().resolve()
    with file_lock(ledger_path):
        ledger = load_batch_ledger(ledger_path)
        photos = ledger["photos"]
        if args.photo_id not in photos:
            raise BridgeError(f"photo_id {args.photo_id!r} not found in batch ledger {ledger_path}")
        entry = photos[args.photo_id]
        if entry.get("missing_source") or not entry.get("source_variant_id"):
            raise BridgeError(f"Cannot prepare variant for {args.photo_id!r}: source RAW is missing.")
        source_var_id = str(entry["source_variant_id"])
        existing_clone_id = str(entry.get("cloned_variant_id") or "")

        if args.retouch_source_variant_id and args.retouch_style:
            raise ValueError("Specify at most one of --retouch-source-variant-id or --retouch-style.")

        color_tag = str(args.color_tag) if args.color_tag is not None else ""
        raw = run_applescript(
            build_prepare_variant_script(),
            [
                source_var_id,
                existing_clone_id,
                args.retouch_source_variant_id or "",
                args.retouch_style or "",
                color_tag,
            ],
        )
        result = json.loads(raw)
        cloned = result["cloned_variant"]
        cloned_id = str(cloned["variant_id"])
        if cloned_id == source_var_id:
            raise BridgeError("Cloned variant ID matches source variant ID; aborting.")

        entry["cloned_variant_id"] = cloned_id
        entry["raw_dimensions"] = cloned.get("dimensions") or entry.get("raw_dimensions")
        entry["orientation"] = cloned.get("orientation", 0)
        if not result.get("reused_existing_clone"):
            entry["clone_token"] = entry.get("clone_token") or uuid.uuid4().hex[:6]
            entry["inherited_layers"] = cloned.get("layers") or []
            entry["managed_layers"] = {}
            entry["managed_layer_records"] = {}
            entry["owned_export_files"] = {}
            entry["layer_wb_used"] = False
            entry["adjustment_seq"] = 0
            entry["export_verified"] = False
            entry["export_receipt"] = None
            entry["retouch_mode"] = result.get("retouch_mode")
            entry["baseline_state"] = cloned
        save_batch_ledger(ledger_path, ledger)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    ledger_path = Path(args.batch_state).expanduser().resolve()
    spec = load_json_arg(args.spec)
    out = execute_apply_transaction(
        ledger_path=ledger_path,
        variant_id=args.variant_id,
        spec=spec,
        note=args.note or "",
        tolerance_scale=args.tolerance_scale,
        allow_layer_wb=args.allow_layer_wb,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["verified"] else 2


def cmd_rollback(args: argparse.Namespace) -> int:
    ledger_path = Path(args.batch_state).expanduser().resolve()
    out = execute_rollback_transaction(
        ledger_path=ledger_path,
        photo_id=args.photo_id,
        tolerance_scale=args.tolerance_scale,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_ensure_recipes(args: argparse.Namespace) -> int:
    raw = run_applescript(
        build_ensure_recipes_script(),
        [args.preview_recipe, args.handoff_recipe, args.srgb_profile, str(args.preview_long_edge)],
    )
    meta = json.loads(raw)
    for key in ("preview_recipe", "handoff_recipe"):
        prof = str((meta.get(key) or {}).get("color_profile") or "")
        if "srgb" not in prof.lower():
            raise BridgeError(f"{key} color profile read-back is not sRGB: {prof!r}")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    batch_state_path = Path(args.batch_state) if args.batch_state else None
    result = execute_export_workflow(
        mode=args.mode,
        variant_id=args.variant_id,
        recipe=args.recipe,
        output_dir=Path(args.output_dir),
        batch_state_path=batch_state_path,
        suffix=args.suffix,
        timeout=args.timeout,
        overwrite_own=args.overwrite_own,
        versioned=args.versioned,
        with_metrics=args.with_metrics,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_resolve_export_conflict(args: argparse.Namespace) -> int:
    out = resolve_export_conflict(
        ledger_path=Path(args.batch_state).expanduser().resolve(),
        photo_id=args.photo_id,
        resolution=args.resolution,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_record_handoff(args: argparse.Namespace) -> int:
    record = load_json_arg(args.record)
    out = upsert_handoff_record(
        jsonl_path=Path(args.handoff_file).expanduser().resolve(),
        record=record,
        ledger_path=Path(args.batch_state).expanduser().resolve(),
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Inspect Capture One document, recipes, and variants.")
    p_status.add_argument("--browse", help="Optional folder path to browse in Session mode before listing.")
    p_status.set_defaults(func=cmd_status)

    p_init = sub.add_parser("init-batch", help="Initialize or resume persistent batch state ledger.")
    p_init.add_argument("--batch-state", required=True, help="Path to batch-state.json.")
    p_init.add_argument("--raw-dir", help="Customer RAW directory to enumerate expected RAW files.")
    p_init.add_argument("--browse", action="store_true", help="Browse --raw-dir in Capture One Session first.")
    p_init.add_argument("--selected-ids", help="Optional comma-separated photo stems/filenames to verify & filter.")
    p_init.add_argument(
        "--record-missing-selected",
        action="store_true",
        help="Instead of aborting on missing selected IDs, record them as missing_source in ledger so balanced stays false.",
    )
    p_init.set_defaults(func=cmd_init_batch)

    p_inspect = sub.add_parser("inspect", help="Read all 28 global and layer adjustments of a variant.")
    p_inspect.add_argument("--variant-id", required=True, help="Capture One variant ID.")
    p_inspect.set_defaults(func=cmd_inspect)

    p_prep = sub.add_parser(
        "prepare-variant",
        help="Idempotently clone source variant, snapshot inherited layers, and apply default retouching.",
    )
    p_prep.add_argument("--batch-state", required=True, help="Path to batch-state.json.")
    p_prep.add_argument("--photo-id", required=True, help="Registered photo_id in batch ledger.")
    p_prep.add_argument(
        "--retouch-style",
        help="Recommended: Capture One Style/Preset name carrying only default portrait retouching.",
    )
    p_prep.add_argument(
        "--retouch-source-variant-id",
        help="Fallback: source variant ID carrying default retouching (blocked if client RAW has local layers).",
    )
    p_prep.add_argument("--color-tag", type=int, choices=range(0, 8), help="Optional color tag (0-7) on clone.")
    p_prep.set_defaults(func=cmd_prepare_variant)

    p_apply = sub.add_parser(
        "apply",
        help="Apply global WB/exposure and isolated AI masks with pre-write checks, WAL, and read-back verification.",
    )
    p_apply.add_argument("--batch-state", required=True, help="Path to batch-state.json.")
    p_apply.add_argument("--variant-id", required=True, help="Registered cloned_variant_id.")
    p_apply.add_argument("--spec", required=True, help="JSON string or @/path/to/spec.json.")
    p_apply.add_argument("--note", help="Audit note (target region, current reading, target absolute, check points).")
    p_apply.add_argument(
        "--tolerance-scale",
        type=parse_positive_finite_float,
        default=1.0,
        help="Finite positive multiplier on per-unit tolerances.",
    )
    p_apply.add_argument(
        "--allow-layer-wb",
        action="store_true",
        help="Experimental: allow temperature/tint inside local mask layers (requires non-null original read-back).",
    )
    p_apply.set_defaults(func=cmd_apply)

    p_rollback = sub.add_parser("rollback", help="Roll back the latest `apply` round and verify restored read-back.")
    p_rollback.add_argument("--batch-state", required=True, help="Path to batch-state.json.")
    p_rollback.add_argument("--photo-id", required=True, help="Registered photo_id in batch ledger.")
    p_rollback.add_argument(
        "--tolerance-scale",
        type=parse_positive_finite_float,
        default=1.0,
        help="Finite positive multiplier on per-unit tolerances.",
    )
    p_rollback.set_defaults(func=cmd_rollback)

    p_recipes = sub.add_parser(
        "ensure-recipes",
        help="Create or update standard preview (no metadata) and full-size sRGB 100 JPG recipes.",
    )
    p_recipes.add_argument("--preview-recipe", default=DEFAULT_PREVIEW_RECIPE)
    p_recipes.add_argument("--handoff-recipe", default=DEFAULT_HANDOFF_RECIPE)
    p_recipes.add_argument("--srgb-profile", default=DEFAULT_SRGB_PROFILE)
    p_recipes.add_argument("--preview-long-edge", type=int, default=1536)
    p_recipes.set_defaults(func=cmd_ensure_recipes)

    p_export = sub.add_parser(
        "export",
        help="Export variant via isolated staging directory, two-phase crash-safe promotion, and SHA-256 receipt.",
    )
    p_export.add_argument("--mode", choices=("preview", "handoff"), required=True, help="Export mode.")
    p_export.add_argument("--batch-state", help="Path to batch-state.json (required for --mode handoff).")
    p_export.add_argument("--variant-id", required=True, help="Registered cloned variant ID to export.")
    p_export.add_argument("--recipe", required=True, help="Recipe name to use.")
    p_export.add_argument("--output-dir", required=True, help="Destination directory for exported JPG.")
    p_export.add_argument("--suffix", help="Optional filename suffix (e.g. before, after).")
    p_export.add_argument("--timeout", type=float, default=90.0, help="Seconds to wait for JPG output.")
    p_export.add_argument("--overwrite-own", action="store_true", help="Allow overwriting prior verified export of same clone (requires exclusive directory & RENAME_SWAP).")
    p_export.add_argument(
        "--versioned",
        action="store_true",
        help="Encode `<stem>_v<adjustment_seq>.jpg` and publish strictly via non-overwriting kernel primitives.",
    )
    p_export.add_argument(
        "--with-metrics",
        action="store_true",
        help="Also compute p05/p50/p95 luminance percentiles via preview-metrics.py.",
    )
    p_export.set_defaults(func=cmd_export)

    p_resolve = sub.add_parser(
        "resolve-export-conflict",
        help="Explicitly clear a latched `pending_export_commit` conflict (`keep-external` or `restore-backup`).",
    )
    p_resolve.add_argument("--batch-state", required=True, help="Path to batch-state.json.")
    p_resolve.add_argument("--photo-id", required=True, help="Registered photo_id with latched conflict.")
    p_resolve.add_argument(
        "--resolution",
        choices=("keep-external", "restore-backup"),
        required=True,
        help="'keep-external' retains live destination; 'restore-backup' restores backup once destination is vacated.",
    )
    p_resolve.set_defaults(func=cmd_resolve_export_conflict)

    p_handoff = sub.add_parser("record-handoff", help="Verify SHA-256 receipt and upsert record into handoff.jsonl.")
    p_handoff.add_argument("--batch-state", required=True, help="Path to batch-state.json.")
    p_handoff.add_argument("--handoff-file", required=True, help="Path to handoff.jsonl.")
    p_handoff.add_argument("--record", required=True, help="JSON string or @/path/to/record.json.")
    p_handoff.set_defaults(func=cmd_record_handoff)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    try:
        return args.func(args)
    except (BridgeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
