"""Unit, concurrency, CLI, and Round 1-4 audit counter-example tests for scripts/c1_bridge.py."""
from __future__ import annotations

from contextlib import nullcontext as contextlib_nullcontext
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "c1_bridge.py"
SPEC = importlib.util.spec_from_file_location("c1_bridge", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
c1_bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(c1_bridge)


def make_bmp(path: Path, width: int, height: int, rgb: tuple[int, int, int] = (128, 128, 128)) -> None:
    r, g, b = rgb
    row_bytes = width * 3
    stride = (row_bytes + 3) & ~3
    pixel_data = (bytes([b, g, r]) * width + b"\x00" * (stride - row_bytes)) * height
    file_size = 54 + len(pixel_data)
    header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixel_data), 2835, 2835, 0, 0)
    path.write_bytes(header + dib + pixel_data)


def make_jpeg_with_profile(
    out_jpg: Path,
    width: int,
    height: int,
    icc_path: Path,
    rgb: tuple[int, int, int] = (128, 128, 128),
) -> None:
    with tempfile.TemporaryDirectory() as td:
        bmp = Path(td) / "src.bmp"
        make_bmp(bmp, width, height, rgb=rgb)
        subprocess.run(
            [
                "/usr/bin/sips",
                "-s", "format", "jpeg",
                "-s", "formatOptions", "100",
                "--embedProfile", str(icc_path),
                str(bmp),
                "--out", str(out_jpg),
            ],
            check=True,
            capture_output=True,
            text=True,
        )


def full_zero_adjustments(**overrides: float | None) -> dict[str, float | None]:
    base: dict[str, float | None] = {k: 0.0 for k in c1_bridge.ADJUSTMENT_PROP_MAP}
    base["temperature"] = 5000.0
    base["level_highlight_rgb"] = 255.0
    base.update(overrides)
    return base


class C1BridgeAllAuditRoundsTests(unittest.TestCase):
    # =========================================================================
    # Round 1 Counter-Examples
    # =========================================================================

    def test_serializer_covers_every_mapped_adjustment_property(self) -> None:
        serializer_code = c1_bridge.build_serialize_adjustments_handler()
        for json_key in c1_bridge.ADJUSTMENT_PROP_MAP:
            self.assertIn(f'\\"{json_key}\\":', serializer_code, f"Missing {json_key} in serializer")

    def test_r1_readback_exposure_tolerance_and_missing_field(self) -> None:
        spec = {
            "global": {"exposure": 0.40},
            "masks": {"face_skin": {"exposure": 0.30}},
        }
        simulated_state = {
            "global_adjustments": {"exposure": 0.0},
            "layers": [
                {
                    "name": "AI_Face_Skin",
                    "kind": "adjustment",
                    "enabled": True,
                    "opacity": 100,
                    "adjustments": {},
                }
            ],
        }
        ok, mismatches = c1_bridge.verify_readback(spec, simulated_state)
        self.assertFalse(ok)
        self.assertEqual(len(mismatches), 2)
        self.assertTrue(any("global.exposure" in m for m in mismatches))
        self.assertTrue(any("masks.face_skin.exposure" in m and "missing/null" in m for m in mismatches))

    def test_r1_staging_blocks_prefix_collision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "staging"
            staging.mkdir()
            (staging / "IMG_0010.jpg").write_bytes(b"wrong-photo")
            with self.assertRaisesRegex(c1_bridge.BridgeError, "Unexpected file"):
                c1_bridge.wait_for_staged_jpg(staging, exact_stem="IMG_001", timeout_sec=1.0)

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r1_downscaled_and_wrong_icc_rejected(self) -> None:
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        p3_icc = Path("/System/Library/ColorSync/Profiles/Display P3.icc")
        base_meta = {
            "raw_dimensions": [64, 48],
            "orientation": 0,
            "recipe_format": "JPEG",
            "recipe_quality": 100,
            "recipe_profile": "sRGB Color Space Profile",
            "scaling_method": "Fixed",
            "primary_scaling_value": 100,
            "ignore_crop": True,
            "export_crop_method": "ignore",
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            good_jpg = root / "good.jpg"
            shrunk_jpg = root / "shrunk.jpg"
            wrong_icc_jpg = root / "p3.jpg"
            make_jpeg_with_profile(good_jpg, 64, 48, srgb_icc)
            make_jpeg_with_profile(shrunk_jpg, 32, 24, srgb_icc)
            make_jpeg_with_profile(wrong_icc_jpg, 64, 48, p3_icc)

            self.assertEqual(
                c1_bridge.validate_handoff_export(base_meta, c1_bridge.inspect_jpg_with_sips(good_jpg)),
                [],
            )
            self.assertTrue(
                c1_bridge.validate_handoff_export(base_meta, c1_bridge.inspect_jpg_with_sips(shrunk_jpg))
            )
            self.assertTrue(
                c1_bridge.validate_handoff_export(base_meta, c1_bridge.inspect_jpg_with_sips(wrong_icc_jpg))
            )

    def test_r1_source_variant_firewall(self) -> None:
        ledger = {
            "batch_id": "test-batch",
            "photos": {
                "7I3A3136": {
                    "photo_id": "7I3A3136",
                    "source_variant_id": "src-var-100",
                    "cloned_variant_id": "clone-var-200",
                }
            },
        }
        with self.assertRaisesRegex(c1_bridge.BridgeError, "SAFETY BLOCK.*original source variant"):
            c1_bridge.resolve_authorized_clone_entry(ledger, "src-var-100")
        with self.assertRaisesRegex(c1_bridge.BridgeError, "SAFETY BLOCK.*not registered"):
            c1_bridge.resolve_authorized_clone_entry(ledger, "stranger-var-999")

    # =========================================================================
    # Round 2 Counter-Examples
    # =========================================================================

    def test_r2_selected_ids_missing_photo_b_never_silently_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            raw_dir.mkdir()
            (raw_dir / "A.CR2").write_bytes(b"fake-raw-a")
            c1_variants = [
                {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
            ]
            strict_ledger = root / "strict-batch.json"
            with self.assertRaisesRegex(c1_bridge.BridgeError, r"missing from RAW source: \['B'\]"):
                c1_bridge.register_batch_entries(
                    strict_ledger,
                    c1_variants=c1_variants,
                    raw_dir=raw_dir,
                    selected_ids=["A", "B"],
                    record_missing_selected=False,
                )

            tracked_ledger = root / "tracked-batch.json"
            handoff_path = root / "handoff.jsonl"
            c1_bridge.register_batch_entries(
                tracked_ledger,
                c1_variants=c1_variants,
                raw_dir=raw_dir,
                selected_ids=["A", "B"],
                record_missing_selected=True,
            )
            res_a = c1_bridge.upsert_handoff_record(
                handoff_path,
                {"photo_id": "A", "status": "needs_review"},
                ledger_path=tracked_ledger,
            )
            summary = res_a["summary"]
            self.assertEqual(summary["expected_total"], 2)
            self.assertEqual(summary["missing_photo_ids"], ["B"])
            self.assertFalse(summary["balanced"])

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r2_mid_export_apply_and_same_path_jpg_swap_rejected(self) -> None:
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        valid_export_meta = {
            "raw_dimensions": [64, 48],
            "orientation": 0,
            "recipe_format": "JPEG",
            "recipe_quality": 100,
            "recipe_profile": "sRGB Color Space Profile",
            "scaling_method": "Fixed",
            "primary_scaling_value": 100,
            "ignore_crop": True,
            "export_crop_method": "ignore",
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            raw_dir.mkdir()
            (raw_dir / "IMG_0001.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"
            handoff_path = root / "handoff.jsonl"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-1", "position": 1, "raw_path": str(raw_dir / "IMG_0001.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["IMG_0001"]["cloned_variant_id"] = "clone-1"
            ledger["photos"]["IMG_0001"]["adjustment_seq"] = 2
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            stale_jpg = root / "out" / "IMG_0001.jpg"
            stale_jpg.parent.mkdir()
            make_jpeg_with_profile(stale_jpg, 64, 48, srgb_icc, rgb=(100, 100, 100))
            stale_sha, stale_sz = c1_bridge.compute_file_sha256(stale_jpg)
            with self.assertRaisesRegex(c1_bridge.BridgeError, "Stale export rejected"):
                c1_bridge.finalize_export_in_ledger(
                    ledger_path=ledger_path,
                    variant_id="clone-1",
                    started_adjustment_seq=1,
                    final_path=stale_jpg,
                    sha256_hex=stale_sha,
                    byte_size=stale_sz,
                    export_meta=valid_export_meta,
                    sips_info=c1_bridge.inspect_jpg_with_sips(stale_jpg),
                )

            fresh_jpg = root / "out" / "IMG_0001.jpg"
            make_jpeg_with_profile(fresh_jpg, 64, 48, srgb_icc, rgb=(110, 120, 130))
            fresh_sha, fresh_sz = c1_bridge.compute_file_sha256(fresh_jpg)
            c1_bridge.finalize_export_in_ledger(
                ledger_path=ledger_path,
                variant_id="clone-1",
                started_adjustment_seq=2,
                final_path=fresh_jpg,
                sha256_hex=fresh_sha,
                byte_size=fresh_sz,
                export_meta=valid_export_meta,
                sips_info=c1_bridge.inspect_jpg_with_sips(fresh_jpg),
            )
            make_jpeg_with_profile(fresh_jpg, 64, 48, srgb_icc, rgb=(210, 40, 40))
            with self.assertRaisesRegex(c1_bridge.BridgeError, "File identity mismatch"):
                c1_bridge.upsert_handoff_record(
                    handoff_path,
                    {"photo_id": "IMG_0001", "output_path": str(fresh_jpg), "status": "passed"},
                    ledger_path=ledger_path,
                )

    def test_r2_concurrent_promote_atomic_single_winner(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for iteration in range(15):
                dest = root / f"race_{iteration}.jpg"
                s1 = root / f"s1_{iteration}.jpg"
                s2 = root / f"s2_{iteration}.jpg"
                payload_1 = b"WINNER_PAYLOAD_ONE_" * 500
                payload_2 = b"WINNER_PAYLOAD_TWO_" * 700
                s1.write_bytes(payload_1)
                s2.write_bytes(payload_2)

                barrier = threading.Barrier(2)
                outcomes: list[tuple[int, str, Exception | None]] = []
                lock = threading.Lock()

                def worker(idx: int, staged: Path, tag: str) -> None:
                    barrier.wait()
                    try:
                        c1_bridge.promote_staged_file(staged, dest, allow_overwrite_same_owner=False)
                        with lock:
                            outcomes.append((idx, tag, None))
                    except Exception as exc:
                        with lock:
                            outcomes.append((idx, tag, exc))

                t1 = threading.Thread(target=worker, args=(1, s1, "one"))
                t2 = threading.Thread(target=worker, args=(2, s2, "two"))
                t1.start()
                t2.start()
                t1.join()
                t2.join()

                successes = [o for o in outcomes if o[2] is None]
                failures = [o for o in outcomes if o[2] is not None]
                self.assertEqual(len(successes), 1)
                self.assertEqual(len(failures), 1)
                expected_bytes = payload_1 if successes[0][1] == "one" else payload_2
                self.assertEqual(dest.read_bytes(), expected_bytes)

    def test_r2_preexisting_same_named_client_layer_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            raw_dir.mkdir()
            (raw_dir / "IMG_0001.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-1", "position": 1, "raw_path": str(raw_dir / "IMG_0001.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            client_face_layer = {
                "index": 2,
                "name": "AI_Face_Skin",
                "kind": "adjustment",
                "enabled": True,
                "opacity": 77,
                "adjustments": full_zero_adjustments(exposure=0.55),
            }
            bg_layer = {
                "index": 1,
                "name": "Background",
                "kind": "background",
                "enabled": True,
                "opacity": 100,
                "adjustments": full_zero_adjustments(exposure=0.0),
            }
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            entry = ledger["photos"]["IMG_0001"]
            entry["cloned_variant_id"] = "clone-1"
            entry["clone_token"] = "tok999"
            entry["inherited_layers"] = [bg_layer, client_face_layer]
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            calls = {"n": 0}

            def fake_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                calls["n"] += 1
                if calls["n"] == 1:
                    return json.dumps({
                        "variant_id": "clone-1",
                        "orientation": 0,
                        "dimensions": [64, 48],
                        "crop": [0, 0, 64, 48],
                        "global_adjustments": full_zero_adjustments(exposure=0.0),
                        "layers": [bg_layer, client_face_layer],
                    })
                self.assertIn("AI_Face_Skin__m_tok999", script_body)
                managed_layer = {
                    "index": 3,
                    "name": "AI_Face_Skin__m_tok999",
                    "kind": "adjustment",
                    "enabled": True,
                    "opacity": 90,
                    "adjustments": full_zero_adjustments(exposure=0.20),
                }
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(exposure=0.15),
                    "layers": [bg_layer, client_face_layer, managed_layer],
                })

            res = c1_bridge.execute_apply_transaction(
                ledger_path=ledger_path,
                variant_id="clone-1",
                spec={"global": {"exposure": 0.15}, "masks": {"face_skin": {"exposure": 0.20, "opacity": 90}}},
                runner_fn=fake_runner,
            )
            self.assertTrue(res["verified"])
            client_after = [l for l in res["state"]["layers"] if l["name"] == "AI_Face_Skin"][0]
            self.assertEqual(client_after["opacity"], 77)
            self.assertAlmostEqual(client_after["adjustments"]["exposure"], 0.55)

    def test_r2_mid_apply_failure_persists_wal_and_rollback_verifies_readback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            raw_dir.mkdir()
            (raw_dir / "IMG_0001.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"
            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-1", "position": 1, "raw_path": str(raw_dir / "IMG_0001.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            bg_zero = {
                "index": 1,
                "name": "Background",
                "kind": "background",
                "enabled": True,
                "opacity": 100,
                "adjustments": full_zero_adjustments(exposure=0.0),
            }
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            entry = ledger["photos"]["IMG_0001"]
            entry["cloned_variant_id"] = "clone-1"
            entry["inherited_layers"] = [bg_zero]
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            live_exp = {"val": 0.0}

            def mid_fail_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                if "create people mask" not in script_body:
                    return json.dumps({
                        "variant_id": "clone-1",
                        "orientation": 0,
                        "dimensions": [64, 48],
                        "crop": [0, 0, 64, 48],
                        "global_adjustments": full_zero_adjustments(exposure=live_exp["val"]),
                        "layers": [bg_zero],
                    })
                live_exp["val"] = 0.40
                raise c1_bridge.BridgeError("Capture One create people mask returned empty layer list")

            with self.assertRaisesRegex(c1_bridge.BridgeError, "empty layer list"):
                c1_bridge.execute_apply_transaction(
                    ledger_path=ledger_path,
                    variant_id="clone-1",
                    spec={"global": {"exposure": 0.40}, "masks": {"face_skin": {"exposure": 0.25}}},
                    runner_fn=mid_fail_runner,
                )

            def working_rb_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                if "set exposure of gAdj to 0" in script_body:
                    live_exp["val"] = 0.0
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(exposure=live_exp["val"]),
                    "layers": [bg_zero],
                })

            rb_res = c1_bridge.execute_rollback_transaction(
                ledger_path=ledger_path,
                photo_id="IMG_0001",
                runner_fn=working_rb_runner,
            )
            self.assertTrue(rb_res["rolled_back"])
            self.assertEqual(live_exp["val"], 0.0)

    # =========================================================================
    # Round 3 Counter-Examples
    # =========================================================================

    def test_r3_counterexample_1_same_stem_different_extension_selected_ids_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            raw_dir.mkdir()
            (raw_dir / "A.CR2").write_bytes(b"cr2-bytes")
            c1_variants = [
                {"variant_id": "src-cr2", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
            ]
            ledger_path = root / "tracked.json"
            handoff_path = root / "handoff.jsonl"
            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=c1_variants,
                raw_dir=raw_dir,
                selected_ids=["A.CR2", "A.NEF"],
                record_missing_selected=True,
            )
            res = c1_bridge.upsert_handoff_record(
                handoff_path,
                {"photo_id": "A.CR2", "status": "needs_review"},
                ledger_path=ledger_path,
            )
            summary = res["summary"]
            self.assertEqual(summary["expected_total"], 2)
            self.assertEqual(summary["missing_photo_ids"], ["A.NEF"])
            self.assertFalse(summary["balanced"])

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r3_counterexample_2_passed_photo_becomes_stale_immediately_on_subsequent_apply(self) -> None:
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            out_dir = root / "out"
            raw_dir.mkdir()
            out_dir.mkdir()
            (raw_dir / "A.CR2").write_bytes(b"a")
            (raw_dir / "B.CR2").write_bytes(b"b")
            ledger_path = root / "batch.json"
            handoff_path = root / "handoff.jsonl"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]},
                    {"variant_id": "src-b", "position": 1, "raw_path": str(raw_dir / "B.CR2"), "dimensions": [64, 48]},
                ],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
            ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
            ledger["photos"]["B"]["cloned_variant_id"] = "clone-b"
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            rgb_box = {"rgb": (120, 120, 120)}

            def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                staged = staging_dir / f"{exact_stem}.jpg"
                make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=rgb_box["rgb"])
                return ({
                    "job_id": "j1",
                    "variant_id": var_id,
                    "image_name": "A.CR2",
                    "raw_path": str(raw_dir / "A.CR2"),
                    "raw_dimensions": [64, 48],
                    "orientation": 0,
                    "recipe_format": "JPEG",
                    "recipe_quality": 100,
                    "recipe_profile": "sRGB Color Space Profile",
                    "scaling_method": "Fixed",
                    "primary_scaling_value": 100,
                    "ignore_crop": True,
                    "export_crop_method": "ignore",
                }, staged)

            exp1 = c1_bridge.execute_export_workflow(
                mode="handoff",
                variant_id="clone-a",
                recipe="AI_Handoff_Full_sRGB100",
                output_dir=out_dir,
                batch_state_path=ledger_path,
                export_runner_fn=export_runner,
            )
            jpg_a = Path(exp1["file"]["path"])
            c1_bridge.upsert_handoff_record(
                handoff_path,
                {"photo_id": "A", "output_path": str(jpg_a), "status": "passed"},
                ledger_path=ledger_path,
            )

            calls = {"n": 0}

            def apply_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                calls["n"] += 1
                exp = 0.0 if calls["n"] == 1 else 0.25
                return json.dumps({
                    "variant_id": "clone-a",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(exposure=exp),
                    "layers": [bg_layer],
                })

            c1_bridge.execute_apply_transaction(
                ledger_path=ledger_path,
                variant_id="clone-a",
                spec={"global": {"exposure": 0.25}},
                runner_fn=apply_runner,
            )

            r_b = c1_bridge.upsert_handoff_record(
                handoff_path,
                {"photo_id": "B", "status": "needs_review"},
                ledger_path=ledger_path,
            )
            self.assertEqual(r_b["summary"]["passed"], 0)
            self.assertEqual(r_b["summary"]["stale"], 1)
            self.assertFalse(r_b["summary"]["balanced"])

            # Re-export A in-place via real export workflow using `overwrite_own=True`!
            rgb_box["rgb"] = (160, 160, 160)
            c1_bridge.execute_export_workflow(
                mode="handoff",
                variant_id="clone-a",
                recipe="AI_Handoff_Full_sRGB100",
                output_dir=out_dir,
                batch_state_path=ledger_path,
                overwrite_own=True,
                export_runner_fn=export_runner,
            )
            r_a_restored = c1_bridge.upsert_handoff_record(
                handoff_path,
                {"photo_id": "A", "output_path": str(jpg_a), "status": "passed"},
                ledger_path=ledger_path,
            )
            self.assertEqual(r_a_restored["summary"]["passed"], 1)
            self.assertEqual(r_a_restored["summary"]["stale"], 0)
            self.assertTrue(r_a_restored["summary"]["balanced"])

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r3_counterexample_3_cli_preview_export_refuses_to_overwrite_formal_handoff_jpg(self) -> None:
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            out_dir = root / "shared_out"
            raw_dir.mkdir()
            out_dir.mkdir()
            (raw_dir / "IMG_0001.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-1", "position": 1, "raw_path": str(raw_dir / "IMG_0001.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["IMG_0001"]["cloned_variant_id"] = "clone-1"
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            export_mode_rgb = {"rgb": (40, 180, 90), "w": 64, "h": 48, "q": 100, "scaling": "Fixed", "val": 100}

            def fake_export_applescript(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                if args and len(args) == 4:
                    _var_id, _recipe, staging_dir_str, exact_stem = args
                    staged_path = Path(staging_dir_str) / f"{exact_stem}.jpg"
                    make_jpeg_with_profile(
                        staged_path,
                        export_mode_rgb["w"],
                        export_mode_rgb["h"],
                        srgb_icc,
                        rgb=export_mode_rgb["rgb"],
                    )
                    return json.dumps({
                        "job_id": "job-123",
                        "variant_id": "clone-1",
                        "image_name": "IMG_0001.CR2",
                        "raw_path": str(raw_dir / "IMG_0001.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": export_mode_rgb["q"],
                        "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": export_mode_rgb["scaling"],
                        "primary_scaling_value": export_mode_rgb["val"],
                        "ignore_crop": True,
                        "export_crop_method": "ignore",
                    })
                return "false"

            with mock.patch.object(c1_bridge, "run_applescript", side_effect=fake_export_applescript):
                with mock.patch("sys.stdout", io.StringIO()):
                    rc_handoff = c1_bridge.main([
                        "export",
                        "--mode", "handoff",
                        "--batch-state", str(ledger_path),
                        "--variant-id", "clone-1",
                        "--recipe", "AI_Handoff_Full_sRGB100",
                        "--output-dir", str(out_dir),
                    ])
                self.assertEqual(rc_handoff, 0)
                formal_jpg = out_dir / "IMG_0001.jpg"
                sha_before, size_before = c1_bridge.compute_file_sha256(formal_jpg)

                export_mode_rgb.update({"rgb": (250, 10, 10), "w": 32, "h": 24, "q": 85, "scaling": "Long_Edge", "val": 32})
                with mock.patch("sys.stderr", io.StringIO()):
                    rc_prev_with_ledger = c1_bridge.main([
                        "export",
                        "--mode", "preview",
                        "--batch-state", str(ledger_path),
                        "--variant-id", "clone-1",
                        "--recipe", "AI_Preview_1536_NoMeta",
                        "--output-dir", str(out_dir),
                    ])
                self.assertEqual(rc_prev_with_ledger, 1)
                self.assertEqual(c1_bridge.compute_file_sha256(formal_jpg), (sha_before, size_before))

                with mock.patch("sys.stderr", io.StringIO()):
                    rc_prev_no_ledger = c1_bridge.main([
                        "export",
                        "--mode", "preview",
                        "--variant-id", "clone-1",
                        "--recipe", "AI_Preview_1536_NoMeta",
                        "--output-dir", str(out_dir),
                    ])
                self.assertEqual(rc_prev_no_ledger, 1)
                self.assertEqual(c1_bridge.compute_file_sha256(formal_jpg), (sha_before, size_before))

    def test_r3_counterexample_4_renamed_managed_layer_and_duplicate_named_layers_abort_before_write(self) -> None:
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            raw_dir.mkdir()
            (raw_dir / "IMG_0001.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-1", "position": 1, "raw_path": str(raw_dir / "IMG_0001.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["IMG_0001"]["cloned_variant_id"] = "clone-1"
            ledger["photos"]["IMG_0001"]["inherited_layers"] = [bg_layer]
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            r1_calls = {"n": 0}

            def r1_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                r1_calls["n"] += 1
                layers = [bg_layer] if r1_calls["n"] == 1 else [
                    bg_layer,
                    {
                        "index": 2,
                        "name": "AI_Face_Skin",
                        "kind": "adjustment",
                        "enabled": True,
                        "opacity": 90,
                        "adjustments": full_zero_adjustments(exposure=0.20),
                    },
                ]
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": layers,
                })

            c1_bridge.execute_apply_transaction(
                ledger_path=ledger_path,
                variant_id="clone-1",
                spec={"masks": {"face_skin": {"exposure": 0.20, "opacity": 90}}},
                runner_fn=r1_runner,
            )

            write_attempted = {"flag": False}

            def renamed_occupied_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                if "set opacity of targetLayer" in script_body or "set exposure of lAdj" in script_body:
                    write_attempted["flag"] = True
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": [
                        bg_layer,
                        {
                            "index": 2,
                            "name": "AI_Face_Skin",
                            "kind": "adjustment",
                            "enabled": True,
                            "opacity": 65,
                            "adjustments": full_zero_adjustments(exposure=0.50),
                        },
                        {
                            "index": 3,
                            "name": "User_Renamed_Face",
                            "kind": "adjustment",
                            "enabled": True,
                            "opacity": 90,
                            "adjustments": full_zero_adjustments(exposure=0.20),
                        },
                    ],
                })

            with self.assertRaisesRegex(c1_bridge.BridgeError, "SAFETY BLOCK.*aborting before write"):
                c1_bridge.execute_apply_transaction(
                    ledger_path=ledger_path,
                    variant_id="clone-1",
                    spec={"masks": {"face_skin": {"exposure": 0.30, "opacity": 95}}},
                    runner_fn=renamed_occupied_runner,
                )
            self.assertFalse(write_attempted["flag"])

    def test_r3_counterexample_5_allow_layer_wb_preserves_and_verifies_rollback_temperature_and_tint(self) -> None:
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            raw_dir.mkdir()
            (raw_dir / "IMG_0001.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-1", "position": 1, "raw_path": str(raw_dir / "IMG_0001.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["IMG_0001"]["cloned_variant_id"] = "clone-1"
            ledger["photos"]["IMG_0001"]["inherited_layers"] = [bg_layer]
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            r1_step = {"n": 0}

            def r1_wb_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                r1_step["n"] += 1
                layers = [bg_layer] if r1_step["n"] == 1 else [
                    bg_layer,
                    {
                        "index": 2,
                        "name": "AI_Background",
                        "kind": "background mask",
                        "enabled": True,
                        "opacity": 100,
                        "adjustments": full_zero_adjustments(temperature=-80.0, tint=1.5, exposure=-0.3),
                    },
                ]
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": layers,
                })

            c1_bridge.execute_apply_transaction(
                ledger_path=ledger_path,
                variant_id="clone-1",
                spec={"masks": {"background": {"temperature": -80.0, "tint": 1.5, "exposure": -0.3}}},
                allow_layer_wb=True,
                runner_fn=r1_wb_runner,
            )

            r2_step = {"n": 0}

            def r2_wb_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                r2_step["n"] += 1
                temp_v, tint_v = (-80.0, 1.5) if r2_step["n"] == 1 else (-200.0, 4.0)
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": [
                        bg_layer,
                        {
                            "index": 2,
                            "name": "AI_Background",
                            "kind": "background mask",
                            "enabled": True,
                            "opacity": 100,
                            "adjustments": full_zero_adjustments(temperature=temp_v, tint=tint_v, exposure=-0.3),
                        },
                    ],
                })

            c1_bridge.execute_apply_transaction(
                ledger_path=ledger_path,
                variant_id="clone-1",
                spec={"masks": {"background": {"temperature": -200.0, "tint": 4.0, "exposure": -0.3}}},
                allow_layer_wb=True,
                runner_fn=r2_wb_runner,
            )

            def stuck_wb_rollback_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": [
                        bg_layer,
                        {
                            "index": 2,
                            "name": "AI_Background",
                            "kind": "background mask",
                            "enabled": True,
                            "opacity": 100,
                            "adjustments": full_zero_adjustments(temperature=-200.0, tint=4.0, exposure=-0.3),
                        },
                    ],
                })

            with self.assertRaisesRegex(c1_bridge.BridgeError, "Rollback verification failed"):
                c1_bridge.execute_rollback_transaction(
                    ledger_path=ledger_path,
                    photo_id="IMG_0001",
                    runner_fn=stuck_wb_rollback_runner,
                )

    # =========================================================================
    # Round 4 Counter-Examples (All 4 Closing Conditions)
    # =========================================================================

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r4_counterexample_1_verification_failed_blocks_export_and_passed_until_verified_rollback(self) -> None:
        """GPT R4 P1 #1: Request +1 EV, read back 0 EV -> verified=False -> changing dir/suffix MUST still block export & passed until rollback."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            out_dir_1 = root / "out1"
            out_dir_2 = root / "out2_alt"
            raw_dir.mkdir()
            out_dir_1.mkdir()
            out_dir_2.mkdir()
            (raw_dir / "IMG_0001.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"
            handoff_path = root / "handoff.jsonl"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-1", "position": 1, "raw_path": str(raw_dir / "IMG_0001.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["IMG_0001"]["cloned_variant_id"] = "clone-1"
            ledger["photos"]["IMG_0001"]["inherited_layers"] = [bg_layer]
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            # Request +1.0 EV, Capture One returns 0.0 EV -> `verified=False` (`verification_failed`)
            def fail_readback_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(exposure=0.0),
                    "layers": [bg_layer],
                })

            apply_res = c1_bridge.execute_apply_transaction(
                ledger_path=ledger_path,
                variant_id="clone-1",
                spec={"global": {"exposure": 1.0}},
                runner_fn=fail_readback_runner,
            )
            self.assertFalse(apply_res["verified"])

            def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                staged = staging_dir / f"{exact_stem}.jpg"
                make_jpeg_with_profile(staged, 64, 48, srgb_icc)
                return ({
                    "job_id": "j-alt",
                    "variant_id": var_id,
                    "image_name": "IMG_0001.CR2",
                    "raw_path": str(raw_dir / "IMG_0001.CR2"),
                    "raw_dimensions": [64, 48],
                    "orientation": 0,
                    "recipe_format": "JPEG",
                    "recipe_quality": 100,
                    "recipe_profile": "sRGB Color Space Profile",
                    "scaling_method": "Fixed",
                    "primary_scaling_value": 100,
                    "ignore_crop": True,
                    "export_crop_method": "ignore",
                }, staged)

            # Attempt formal export to a new directory AND new suffix -> MUST be blocked!
            with self.assertRaisesRegex(c1_bridge.BridgeError, "SAFETY BLOCK.*unresolved failed/interrupted"):
                c1_bridge.execute_export_workflow(
                    mode="handoff",
                    variant_id="clone-1",
                    recipe="AI_Handoff_Full_sRGB100",
                    output_dir=out_dir_2,
                    suffix="new_suffix",
                    batch_state_path=ledger_path,
                    export_runner_fn=export_runner,
                )

            # Even if a valid JPG exists at alt path, `record-handoff` with status='passed' MUST be blocked!
            manual_jpg = out_dir_2 / "IMG_0001_new_suffix.jpg"
            make_jpeg_with_profile(manual_jpg, 64, 48, srgb_icc)
            with self.assertRaisesRegex(c1_bridge.BridgeError, "SAFETY BLOCK.*unresolved failed/interrupted"):
                c1_bridge.upsert_handoff_record(
                    handoff_path,
                    {"photo_id": "IMG_0001", "output_path": str(manual_jpg), "status": "passed"},
                    ledger_path=ledger_path,
                )

            # Stacking another `apply` without rolling back first MUST also be blocked!
            with self.assertRaisesRegex(c1_bridge.BridgeError, "SAFETY BLOCK.*unresolved failed/interrupted"):
                c1_bridge.execute_apply_transaction(
                    ledger_path=ledger_path,
                    variant_id="clone-1",
                    spec={"global": {"exposure": 0.2}},
                    runner_fn=fail_readback_runner,
                )

            # Perform verified `rollback` -> restores clean state -> now export & handoff `passed` succeed!
            rb_res = c1_bridge.execute_rollback_transaction(
                ledger_path=ledger_path,
                photo_id="IMG_0001",
                runner_fn=fail_readback_runner,
            )
            self.assertTrue(rb_res["rolled_back"])

            exp_clean = c1_bridge.execute_export_workflow(
                mode="handoff",
                variant_id="clone-1",
                recipe="AI_Handoff_Full_sRGB100",
                output_dir=out_dir_1,
                batch_state_path=ledger_path,
                export_runner_fn=export_runner,
            )
            ho_res = c1_bridge.upsert_handoff_record(
                handoff_path,
                {"photo_id": "IMG_0001", "output_path": exp_clean["file"]["path"], "status": "passed"},
                ledger_path=ledger_path,
            )
            self.assertEqual(ho_res["summary"]["passed"], 1)
            self.assertTrue(ho_res["summary"]["balanced"])

    def test_r4_counterexample_2_failed_round_rollback_aborts_with_zero_write_calls_when_layer_renamed_and_occupied(self) -> None:
        """GPT R4 P1 #2: After `verification_failed` (and `failed_mid_apply`), rename managed layer & occupy old name -> rollback write_calls == 0."""
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            raw_dir.mkdir()
            (raw_dir / "IMG_0001.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-1", "position": 1, "raw_path": str(raw_dir / "IMG_0001.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["IMG_0001"]["cloned_variant_id"] = "clone-1"
            ledger["photos"]["IMG_0001"]["inherited_layers"] = [bg_layer]
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            # Produce `verification_failed`: face_skin created as `AI_Face_Skin`, but global exposure +1.0 read back as 0.0
            step = {"n": 0}

            def produce_verif_failed_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                step["n"] += 1
                layers = [bg_layer] if step["n"] == 1 else [
                    bg_layer,
                    {
                        "index": 2,
                        "name": "AI_Face_Skin",
                        "kind": "adjustment",
                        "enabled": True,
                        "opacity": 90,
                        "adjustments": full_zero_adjustments(exposure=0.25),
                    },
                ]
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(exposure=0.0),  # Mismatch vs +1.0!
                    "layers": layers,
                })

            res_vf = c1_bridge.execute_apply_transaction(
                ledger_path=ledger_path,
                variant_id="clone-1",
                spec={
                    "global": {"exposure": 1.0},
                    "masks": {"face_skin": {"exposure": 0.25, "opacity": 90}},
                },
                runner_fn=produce_verif_failed_runner,
            )
            self.assertFalse(res_vf["verified"])
            self.assertEqual(
                c1_bridge.load_batch_ledger(ledger_path)["photos"]["IMG_0001"]["history"][-1]["status"],
                "verification_failed",
            )

            # Subcase A: Original `AI_Face_Skin` renamed to `Original_Renamed`, another layer occupies `AI_Face_Skin`
            counts_a = {"read": 0, "write": 0}

            def rollback_renamed_occupied_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                if "set enabled of" in script_body or "set opacity of" in script_body or "set exposure of" in script_body:
                    counts_a["write"] += 1
                else:
                    counts_a["read"] += 1
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(exposure=0.0),
                    "layers": [
                        bg_layer,
                        {
                            "index": 2,
                            "name": "AI_Face_Skin",  # Impostor occupying old name!
                            "kind": "adjustment",
                            "enabled": True,
                            "opacity": 55,
                            "adjustments": full_zero_adjustments(exposure=0.60),
                        },
                        {
                            "index": 3,
                            "name": "Original_Renamed",
                            "kind": "adjustment",
                            "enabled": True,
                            "opacity": 90,
                            "adjustments": full_zero_adjustments(exposure=0.25),
                        },
                    ],
                })

            with self.assertRaisesRegex(c1_bridge.BridgeError, "SAFETY BLOCK.*aborting before write"):
                c1_bridge.execute_rollback_transaction(
                    ledger_path=ledger_path,
                    photo_id="IMG_0001",
                    runner_fn=rollback_renamed_occupied_runner,
                )
            self.assertEqual(counts_a["read"], 1)
            self.assertEqual(counts_a["write"], 0, "Zero write calls allowed when layer was renamed & occupied!")

            # Subcase B: Original `AI_Face_Skin` deleted and replaced by single impostor named `AI_Face_Skin` (same total layer count!)
            counts_b = {"read": 0, "write": 0}

            def rollback_replaced_same_count_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                if "set enabled of" in script_body or "set opacity of" in script_body or "set exposure of" in script_body:
                    counts_b["write"] += 1
                else:
                    counts_b["read"] += 1
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(exposure=0.0),
                    "layers": [
                        bg_layer,
                        {
                            "index": 2,
                            "name": "AI_Face_Skin",
                            "kind": "adjustment",
                            "enabled": True,
                            "opacity": 42,  # Drifted from 90!
                            "adjustments": full_zero_adjustments(exposure=0.25),
                        },
                    ],
                })

            with self.assertRaisesRegex(c1_bridge.BridgeError, "SAFETY BLOCK.*aborting before write"):
                c1_bridge.execute_rollback_transaction(
                    ledger_path=ledger_path,
                    photo_id="IMG_0001",
                    runner_fn=rollback_replaced_same_count_runner,
                )
            self.assertEqual(counts_b["read"], 1)
            self.assertEqual(counts_b["write"], 0, "Zero write calls allowed when impostor layer has drifted signature!")

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r4_counterexample_3_cli_overwrite_own_reexport_works_after_apply_and_blocks_external_tamper(self) -> None:
        """GPT R4 P2 #3: CLI handoff export A.jpg -> apply clears export_receipt -> CLI `--overwrite-own` updates A.jpg if hash matches, blocks if externally modified."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            out_dir = root / "out"
            raw_dir.mkdir()
            out_dir.mkdir()
            (raw_dir / "A.CR2").write_bytes(b"raw-a")
            ledger_path = root / "batch.json"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
            ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            render_rgb = {"rgb": (80, 90, 100)}

            def fake_export_as(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                if args and len(args) == 4:
                    _vid, _rec, staging_str, exact_stem = args
                    make_jpeg_with_profile(Path(staging_str) / f"{exact_stem}.jpg", 64, 48, srgb_icc, rgb=render_rgb["rgb"])
                    return json.dumps({
                        "job_id": "job-a",
                        "variant_id": "clone-a",
                        "image_name": "A.CR2",
                        "raw_path": str(raw_dir / "A.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": 100,
                        "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": "Fixed",
                        "primary_scaling_value": 100,
                        "ignore_crop": True,
                        "export_crop_method": "ignore",
                    })
                return "false"

            # 1. First CLI handoff export creates `out/A.jpg`
            with mock.patch.object(c1_bridge, "run_applescript", side_effect=fake_export_as), \
                 mock.patch("sys.stdout", io.StringIO()):
                rc1 = c1_bridge.main([
                    "export", "--mode", "handoff",
                    "--batch-state", str(ledger_path),
                    "--variant-id", "clone-a",
                    "--recipe", "AI_Handoff_Full_sRGB100",
                    "--output-dir", str(out_dir),
                ])
            self.assertEqual(rc1, 0)
            a_jpg = out_dir / "A.jpg"
            sha_v1, _ = c1_bridge.compute_file_sha256(a_jpg)

            # 2. Run `apply` on A -> clears `export_receipt` (while keeping `owned_export_files`!)
            app_calls = {"n": 0}

            def apply_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                app_calls["n"] += 1
                exp = 0.0 if app_calls["n"] == 1 else 0.30
                return json.dumps({
                    "variant_id": "clone-a",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(exposure=exp),
                    "layers": [bg_layer],
                })

            c1_bridge.execute_apply_transaction(
                ledger_path=ledger_path,
                variant_id="clone-a",
                spec={"global": {"exposure": 0.30}},
                runner_fn=apply_runner,
            )
            self.assertIsNone(c1_bridge.load_batch_ledger(ledger_path)["photos"]["A"]["export_receipt"])

            # 3. Re-export to SAME path `out/A.jpg` via CLI `--overwrite-own` -> MUST succeed!
            render_rgb["rgb"] = (140, 150, 160)
            with mock.patch.object(c1_bridge, "run_applescript", side_effect=fake_export_as), \
                 mock.patch("sys.stdout", io.StringIO()):
                rc2 = c1_bridge.main([
                    "export", "--mode", "handoff", "--overwrite-own",
                    "--batch-state", str(ledger_path),
                    "--variant-id", "clone-a",
                    "--recipe", "AI_Handoff_Full_sRGB100",
                    "--output-dir", str(out_dir),
                ])
            self.assertEqual(rc2, 0)
            sha_v2, _ = c1_bridge.compute_file_sha256(a_jpg)
            self.assertNotEqual(sha_v1, sha_v2)

            # 4. Now apply again, then externally modify `out/A.jpg` -> CLI `--overwrite-own` MUST refuse overwrite!
            def apply_runner_2(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                app_calls["n"] += 1
                exp = 0.30 if app_calls["n"] == 3 else 0.45
                return json.dumps({
                    "variant_id": "clone-a",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(exposure=exp),
                    "layers": [bg_layer],
                })

            c1_bridge.execute_apply_transaction(
                ledger_path=ledger_path,
                variant_id="clone-a",
                spec={"global": {"exposure": 0.45}},
                runner_fn=apply_runner_2,
            )
            # External modification of `out/A.jpg`
            make_jpeg_with_profile(a_jpg, 64, 48, srgb_icc, rgb=(10, 20, 240))
            ext_sha, ext_sz = c1_bridge.compute_file_sha256(a_jpg)

            render_rgb["rgb"] = (200, 200, 200)
            with mock.patch.object(c1_bridge, "run_applescript", side_effect=fake_export_as), \
                 mock.patch("sys.stderr", io.StringIO()):
                rc3 = c1_bridge.main([
                    "export", "--mode", "handoff", "--overwrite-own",
                    "--batch-state", str(ledger_path),
                    "--variant-id", "clone-a",
                    "--recipe", "AI_Handoff_Full_sRGB100",
                    "--output-dir", str(out_dir),
                ])
            self.assertEqual(rc3, 1, "Externally modified JPG must NOT be overwritten even with --overwrite-own!")
            self.assertEqual(c1_bridge.compute_file_sha256(a_jpg), (ext_sha, ext_sz))

    def test_r4_counterexample_4_missing_or_null_original_layer_wb_aborts_before_write(self) -> None:
        """GPT R4 P2 #4: Overwriting existing layer temperature/tint when original is missing or null MUST stop BEFORE write."""
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        for case_name, bad_adj in (
            ("null_temperature", full_zero_adjustments(temperature=None)),
            ("missing_temperature", {k: v for k, v in full_zero_adjustments().items() if k != "temperature"}),
            ("null_tint", full_zero_adjustments(tint=None)),
        ):
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                raw_dir = root / "raws"
                raw_dir.mkdir()
                (raw_dir / "IMG_0001.CR2").write_bytes(b"raw")
                ledger_path = root / "batch.json"

                c1_bridge.register_batch_entries(
                    ledger_path,
                    c1_variants=[
                        {"variant_id": "src-1", "position": 1, "raw_path": str(raw_dir / "IMG_0001.CR2"), "dimensions": [64, 48]}
                    ],
                    raw_dir=raw_dir,
                )
                existing_bg_mask = {
                    "index": 2,
                    "name": "AI_Background",
                    "kind": "background mask",
                    "enabled": True,
                    "opacity": 100,
                    "adjustments": bad_adj,
                }
                ledger = c1_bridge.load_batch_ledger(ledger_path)
                entry = ledger["photos"]["IMG_0001"]
                entry["cloned_variant_id"] = "clone-1"
                entry["inherited_layers"] = [bg_layer]
                entry["managed_layers"] = {"background": "AI_Background"}
                entry["managed_layer_records"] = {
                    "background": {
                        "name": "AI_Background",
                        "kind": "background mask",
                        "index": 2,
                        "enabled": True,
                        "opacity": 100,
                        "adjustments": dict(bad_adj),
                    }
                }
                c1_bridge.save_batch_ledger(ledger_path, ledger)

                counts = {"read": 0, "write": 0}

                def inspect_only_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                    if "set temperature of" in script_body or "set tint of" in script_body:
                        counts["write"] += 1
                    else:
                        counts["read"] += 1
                    return json.dumps({
                        "variant_id": "clone-1",
                        "orientation": 0,
                        "dimensions": [64, 48],
                        "crop": [0, 0, 64, 48],
                        "global_adjustments": full_zero_adjustments(),
                        "layers": [bg_layer, existing_bg_mask],
                    })

                req_field = "tint" if case_name == "null_tint" else "temperature"
                with self.assertRaisesRegex(c1_bridge.BridgeError, "SAFETY BLOCK.*missing or null.*aborting before write"):
                    c1_bridge.execute_apply_transaction(
                        ledger_path=ledger_path,
                        variant_id="clone-1",
                        spec={"masks": {"background": {req_field: -80.0}}},
                        allow_layer_wb=True,
                        runner_fn=inspect_only_runner,
                    )
                self.assertEqual(counts["read"], 1)
                self.assertEqual(counts["write"], 0, f"Must NOT issue write command for {case_name}!")

    # =========================================================================
    # Round 5 Counter-Examples (All 5 Closing Conditions)
    # =========================================================================

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r5_counterexample_1_mid_rollback_process_exit_persists_in_flight_and_blocks_until_resumed(self) -> None:
        """GPT R5 P1 #1: Simulate process exit mid-rollback write -> reload ledger -> apply/export/passed blocked until resumed rollback verifies."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            out_dir = root / "out"
            raw_dir.mkdir()
            out_dir.mkdir()
            (raw_dir / "IMG_0001.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"
            handoff_path = root / "handoff.jsonl"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-1", "position": 1, "raw_path": str(raw_dir / "IMG_0001.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["IMG_0001"]["cloned_variant_id"] = "clone-1"
            ledger["photos"]["IMG_0001"]["inherited_layers"] = [bg_layer]
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            # Round 1 succeeds (`applied`, `verified=True`)
            r1_step = {"n": 0}

            def r1_ok_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                r1_step["n"] += 1
                exp = 0.0 if r1_step["n"] == 1 else 0.40
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(exposure=exp),
                    "layers": [bg_layer],
                })

            res1 = c1_bridge.execute_apply_transaction(
                ledger_path=ledger_path,
                variant_id="clone-1",
                spec={"global": {"exposure": 0.40}},
                runner_fn=r1_ok_runner,
            )
            self.assertTrue(res1["verified"])

            # Simulate process crash (`SystemExit`, bypassing `except Exception:`) during rollback write!
            rb_calls = {"n": 0}

            def crashing_rollback_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                rb_calls["n"] += 1
                if rb_calls["n"] == 1:
                    return json.dumps({
                        "variant_id": "clone-1",
                        "orientation": 0,
                        "dimensions": [64, 48],
                        "crop": [0, 0, 64, 48],
                        "global_adjustments": full_zero_adjustments(exposure=0.40),
                        "layers": [bg_layer],
                    })
                # Simulate hard process exit mid-write (NOT an `Exception` subclass!)
                raise SystemExit(99)

            with self.assertRaises(SystemExit):
                c1_bridge.execute_rollback_transaction(
                    ledger_path=ledger_path,
                    photo_id="IMG_0001",
                    runner_fn=crashing_rollback_runner,
                )

            # Reload ledger after simulated restart: unresolved issues MUST NOT be empty!
            reloaded = c1_bridge.load_batch_ledger(ledger_path)
            issues = c1_bridge.find_unresolved_history_issues(reloaded["photos"]["IMG_0001"])
            self.assertEqual(len(issues), 1)
            self.assertIn("rollback_in_flight", issues[0])

            # New `apply`, formal `handoff` export, and `passed` handoff MUST all be blocked!
            with self.assertRaisesRegex(c1_bridge.BridgeError, "SAFETY BLOCK.*rollback_in_flight"):
                c1_bridge.execute_apply_transaction(
                    ledger_path=ledger_path,
                    variant_id="clone-1",
                    spec={"global": {"exposure": 0.20}},
                    runner_fn=r1_ok_runner,
                )

            def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                staged = staging_dir / f"{exact_stem}.jpg"
                make_jpeg_with_profile(staged, 64, 48, srgb_icc)
                return ({
                    "job_id": "j1",
                    "variant_id": var_id,
                    "image_name": "IMG_0001.CR2",
                    "raw_path": str(raw_dir / "IMG_0001.CR2"),
                    "raw_dimensions": [64, 48],
                    "orientation": 0,
                    "recipe_format": "JPEG",
                    "recipe_quality": 100,
                    "recipe_profile": "sRGB Color Space Profile",
                    "scaling_method": "Fixed",
                    "primary_scaling_value": 100,
                    "ignore_crop": True,
                    "export_crop_method": "ignore",
                }, staged)

            with self.assertRaisesRegex(c1_bridge.BridgeError, "SAFETY BLOCK.*rollback_in_flight"):
                c1_bridge.execute_export_workflow(
                    mode="handoff",
                    variant_id="clone-1",
                    recipe="AI_Handoff_Full_sRGB100",
                    output_dir=out_dir,
                    batch_state_path=ledger_path,
                    export_runner_fn=export_runner,
                )

            dummy_jpg = out_dir / "IMG_0001.jpg"
            make_jpeg_with_profile(dummy_jpg, 64, 48, srgb_icc)
            with self.assertRaisesRegex(c1_bridge.BridgeError, "SAFETY BLOCK.*rollback_in_flight"):
                c1_bridge.upsert_handoff_record(
                    handoff_path,
                    {"photo_id": "IMG_0001", "output_path": str(dummy_jpg), "status": "passed"},
                    ledger_path=ledger_path,
                )
            dummy_jpg.unlink()

            # Resume `rollback` and complete verified read-back -> unblocks export & `passed`!
            resume_step = {"n": 0}

            def resumed_rollback_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                resume_step["n"] += 1
                exp = 0.15 if resume_step["n"] == 1 else 0.0  # Partial mid-rollback value -> restored 0.0
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(exposure=exp),
                    "layers": [bg_layer],
                })

            rb_done = c1_bridge.execute_rollback_transaction(
                ledger_path=ledger_path,
                photo_id="IMG_0001",
                runner_fn=resumed_rollback_runner,
            )
            self.assertTrue(rb_done["rolled_back"])
            exp_ok = c1_bridge.execute_export_workflow(
                mode="handoff",
                variant_id="clone-1",
                recipe="AI_Handoff_Full_sRGB100",
                output_dir=out_dir,
                batch_state_path=ledger_path,
                export_runner_fn=export_runner,
            )
            ho_ok = c1_bridge.upsert_handoff_record(
                handoff_path,
                {"photo_id": "IMG_0001", "output_path": exp_ok["file"]["path"], "status": "passed"},
                ledger_path=ledger_path,
            )
            self.assertEqual(ho_ok["summary"]["passed"], 1)

    def test_r5_counterexample_2_interrupted_layer_creation_does_not_disable_substitute_layer(self) -> None:
        """GPT R5 P1 #2: Request face +0.3 EV -> interrupted mid-create (no post-create signature) -> substitute layer (+3 EV, opacity 100) stays enabled with 0 writes."""
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            raw_dir.mkdir()
            (raw_dir / "IMG_0001.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-1", "position": 1, "raw_path": str(raw_dir / "IMG_0001.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["IMG_0001"]["cloned_variant_id"] = "clone-1"
            ledger["photos"]["IMG_0001"]["inherited_layers"] = [bg_layer]
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            # Request face_skin +0.3 EV, interrupt mid-apply so `after_managed_layer_records` is empty
            def interrupt_create_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                if "create people mask" not in script_body:
                    return json.dumps({
                        "variant_id": "clone-1",
                        "orientation": 0,
                        "dimensions": [64, 48],
                        "crop": [0, 0, 64, 48],
                        "global_adjustments": full_zero_adjustments(),
                        "layers": [bg_layer],
                    })
                raise c1_bridge.BridgeError("AppleEvent timed out during create people mask")

            with self.assertRaisesRegex(c1_bridge.BridgeError, "timed out"):
                c1_bridge.execute_apply_transaction(
                    ledger_path=ledger_path,
                    variant_id="clone-1",
                    spec={"masks": {"face_skin": {"exposure": 0.30}}},
                    runner_fn=interrupt_create_runner,
                )

            # Place a substitute layer with same name `AI_Face_Skin`, same kind `adjustment`, opacity 100, exposure +3.0 EV
            substitute_layer = {
                "index": 2,
                "name": "AI_Face_Skin",
                "kind": "adjustment",
                "enabled": True,
                "opacity": 100,
                "adjustments": full_zero_adjustments(exposure=3.0),
            }
            rb_counts = {"read": 0, "write": 0}

            def rb_substitute_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                if "set enabled of" in script_body or "set opacity of" in script_body or "set exposure of" in script_body:
                    rb_counts["write"] += 1
                    substitute_layer["enabled"] = False
                else:
                    rb_counts["read"] += 1
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": [bg_layer, substitute_layer],
                })

            with self.assertRaisesRegex(c1_bridge.BridgeError, "SAFETY BLOCK.*without post-creation signature proof"):
                c1_bridge.execute_rollback_transaction(
                    ledger_path=ledger_path,
                    photo_id="IMG_0001",
                    runner_fn=rb_substitute_runner,
                )
            self.assertEqual(rb_counts["read"], 1)
            self.assertEqual(rb_counts["write"], 0, "Zero write calls allowed when layer ownership lacks proof!")
            self.assertTrue(substitute_layer["enabled"], "Substitute layer must remain enabled!")
            # Pending recovery state retained in ledger
            self.assertEqual(
                len(c1_bridge.load_batch_ledger(ledger_path)["photos"]["IMG_0001"]["history"]),
                1,
            )

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r5_counterexample_3_concurrent_apply_during_overwrite_own_preserves_previous_formal_jpg(self) -> None:
        """GPT R5 P1 #3: Concurrent `apply` (both succeeding & failing) during `--overwrite-own` re-export MUST preserve original JPG SHA-256."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        for mode_case in ("concurrent_apply_succeeds", "concurrent_apply_fails"):
            with self.subTest(case=mode_case), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                raw_dir = root / "raws"
                out_dir = root / "out"
                raw_dir.mkdir()
                out_dir.mkdir()
                (raw_dir / "A.CR2").write_bytes(b"raw")
                ledger_path = root / "batch.json"

                c1_bridge.register_batch_entries(
                    ledger_path,
                    c1_variants=[
                        {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                    ],
                    raw_dir=raw_dir,
                )
                ledger = c1_bridge.load_batch_ledger(ledger_path)
                ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
                ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
                c1_bridge.save_batch_ledger(ledger_path, ledger)

                # Initial formal export `out/A.jpg`
                def initial_export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                    staged = staging_dir / f"{exact_stem}.jpg"
                    make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=(50, 100, 150))
                    return ({
                        "job_id": "j-init",
                        "variant_id": var_id,
                        "image_name": "A.CR2",
                        "raw_path": str(raw_dir / "A.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": 100,
                        "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": "Fixed",
                        "primary_scaling_value": 100,
                        "ignore_crop": True,
                        "export_crop_method": "ignore",
                    }, staged)

                c1_bridge.execute_export_workflow(
                    mode="handoff",
                    variant_id="clone-a",
                    recipe="AI_Handoff_Full_sRGB100",
                    output_dir=out_dir,
                    batch_state_path=ledger_path,
                    export_runner_fn=initial_export_runner,
                )
                a_jpg = out_dir / "A.jpg"
                orig_sha, orig_sz = c1_bridge.compute_file_sha256(a_jpg)

                # Now start `--overwrite-own` re-export; WHILE rendering staged file, a concurrent `apply` runs!
                def racing_export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                    staged = staging_dir / f"{exact_stem}.jpg"
                    make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=(220, 30, 30))

                    # Trigger concurrent `apply` before `execute_export_workflow` reaches commit!
                    c_step = {"n": 0}

                    def conc_apply_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                        c_step["n"] += 1
                        if c_step["n"] == 1:
                            exp_v = 0.0
                        else:
                            exp_v = 0.35 if mode_case == "concurrent_apply_succeeds" else 0.0  # 0.0 != 0.35 -> verification_failed
                        return json.dumps({
                            "variant_id": "clone-a",
                            "orientation": 0,
                            "dimensions": [64, 48],
                            "crop": [0, 0, 64, 48],
                            "global_adjustments": full_zero_adjustments(exposure=exp_v),
                            "layers": [bg_layer],
                        })

                    c1_bridge.execute_apply_transaction(
                        ledger_path=ledger_path,
                        variant_id="clone-a",
                        spec={"global": {"exposure": 0.35}},
                        runner_fn=conc_apply_runner,
                    )

                    return ({
                        "job_id": "j-race",
                        "variant_id": var_id,
                        "image_name": "A.CR2",
                        "raw_path": str(raw_dir / "A.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": 100,
                        "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": "Fixed",
                        "primary_scaling_value": 100,
                        "ignore_crop": True,
                        "export_crop_method": "ignore",
                    }, staged)

                with self.assertRaises(c1_bridge.BridgeError):
                    c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        overwrite_own=True,
                        export_runner_fn=racing_export_runner,
                    )

                # Original `out/A.jpg` MUST still exist with exact original SHA-256!
                self.assertTrue(a_jpg.is_file(), f"Original JPG lost in {mode_case}!")
                self.assertEqual(c1_bridge.compute_file_sha256(a_jpg), (orig_sha, orig_sz))

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r5_counterexample_4_legacy_ledger_migrates_ownership_before_apply_and_rollback(self) -> None:
        """GPT R5 P2 #4: Legacy ledger with ONLY `export_receipt` (no `owned_export_files`) preserves `--overwrite-own` after `apply` and `rollback`."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        for op_mode in ("after_apply", "after_rollback"):
            with self.subTest(op=op_mode), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                raw_dir = root / "raws"
                out_dir = root / "out"
                raw_dir.mkdir()
                out_dir.mkdir()
                (raw_dir / "A.CR2").write_bytes(b"raw")
                a_jpg = out_dir / "A.jpg"
                make_jpeg_with_profile(a_jpg, 64, 48, srgb_icc, rgb=(70, 80, 90))
                orig_sha, orig_sz = c1_bridge.compute_file_sha256(a_jpg)

                # Construct a legacy v26.11.17/18 ledger WITHOUT `owned_export_files`!
                legacy_ledger = {
                    "batch_id": "legacy-batch",
                    "created_at": "2026-11-26T00:00:00+00:00",
                    "handoff_files": [],
                    "photos": {
                        "A": {
                            "photo_id": "A",
                            "raw_path": str(raw_dir / "A.CR2"),
                            "source_variant_id": "src-a",
                            "raw_dimensions": [64, 48],
                            "orientation": 0,
                            "missing_source": False,
                            "cloned_variant_id": "clone-a",
                            "clone_token": "leg123",
                            "inherited_layers": [bg_layer],
                            "managed_layers": {},
                            "adjustment_seq": 1,
                            "history": [
                                {
                                    "status": "applied",
                                    "verified": True,
                                    "requested_spec": {"global": {"exposure": 0.2}},
                                    "planned_managed_layers": {},
                                    "previous_managed_layers": {},
                                    "before_spec_snapshot": {"global": full_zero_adjustments(exposure=0.0), "masks": {}},
                                }
                            ] if op_mode == "after_rollback" else [],
                            "exported_jpg": str(a_jpg),
                            "export_verified": True,
                            "export_receipt": {
                                "adjustment_seq": 1,
                                "output_path": str(a_jpg),
                                "sha256": orig_sha,
                                "byte_size": orig_sz,
                            },
                        }
                    },
                }
                ledger_path = root / "legacy-batch.json"
                ledger_path.write_text(json.dumps(legacy_ledger), encoding="utf-8")

                if op_mode == "after_apply":
                    step = {"n": 0}

                    def app_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                        step["n"] += 1
                        exp = 0.0 if step["n"] == 1 else 0.25
                        return json.dumps({
                            "variant_id": "clone-a",
                            "orientation": 0,
                            "dimensions": [64, 48],
                            "crop": [0, 0, 64, 48],
                            "global_adjustments": full_zero_adjustments(exposure=exp),
                            "layers": [bg_layer],
                        })

                    c1_bridge.execute_apply_transaction(
                        ledger_path=ledger_path,
                        variant_id="clone-a",
                        spec={"global": {"exposure": 0.25}},
                        runner_fn=app_runner,
                    )
                else:
                    rb_step = {"n": 0}

                    def rb_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                        rb_step["n"] += 1
                        exp = 0.2 if rb_step["n"] == 1 else 0.0
                        return json.dumps({
                            "variant_id": "clone-a",
                            "orientation": 0,
                            "dimensions": [64, 48],
                            "crop": [0, 0, 64, 48],
                            "global_adjustments": full_zero_adjustments(exposure=exp),
                            "layers": [bg_layer],
                        })

                    c1_bridge.execute_rollback_transaction(
                        ledger_path=ledger_path,
                        photo_id="A",
                        runner_fn=rb_runner,
                    )

                # Confirm `export_receipt` was cleared, yet `owned_export_files` was migrated!
                post_op_entry = c1_bridge.load_batch_ledger(ledger_path)["photos"]["A"]
                self.assertIsNone(post_op_entry["export_receipt"])
                self.assertIn(str(a_jpg.resolve()), post_op_entry["owned_export_files"])

                # 1. Re-export via formal export workflow with `overwrite_own=True` -> succeeds!
                def new_export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                    staged = staging_dir / f"{exact_stem}.jpg"
                    make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=(180, 180, 180))
                    return ({
                        "job_id": "j-new",
                        "variant_id": var_id,
                        "image_name": "A.CR2",
                        "raw_path": str(raw_dir / "A.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": 100,
                        "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": "Fixed",
                        "primary_scaling_value": 100,
                        "ignore_crop": True,
                        "export_crop_method": "ignore",
                    }, staged)

                c1_bridge.execute_export_workflow(
                    mode="handoff",
                    variant_id="clone-a",
                    recipe="AI_Handoff_Full_sRGB100",
                    output_dir=out_dir,
                    batch_state_path=ledger_path,
                    overwrite_own=True,
                    export_runner_fn=new_export_runner,
                )
                updated_sha, _ = c1_bridge.compute_file_sha256(a_jpg)
                self.assertNotEqual(updated_sha, orig_sha)

                # 2. External modification of `A.jpg` is still rejected!
                make_jpeg_with_profile(a_jpg, 64, 48, srgb_icc, rgb=(5, 250, 5))
                ext_sha, ext_sz = c1_bridge.compute_file_sha256(a_jpg)
                with self.assertRaisesRegex(c1_bridge.BridgeError, "Refusing overwrite"):
                    c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        overwrite_own=True,
                        export_runner_fn=new_export_runner,
                    )
                self.assertEqual(c1_bridge.compute_file_sha256(a_jpg), (ext_sha, ext_sz))

    def test_r5_counterexample_5_nan_and_inf_tolerance_or_readback_rejected_before_write_and_on_verify(self) -> None:
        """GPT R5 P2 #5: `--tolerance-scale nan` (and `inf`) rejected before write; `+1 EV -> 0 EV` never passes."""
        # 1. Direct `verify_readback` with +1 EV requested vs 0 EV actual and NaN/Inf tolerance MUST raise ValueError
        for bad_tol in (float("nan"), float("inf"), float("-inf"), 0.0, -1.0):
            with self.assertRaises(ValueError):
                c1_bridge.verify_readback(
                    {"global": {"exposure": 1.0}},
                    {"global_adjustments": {"exposure": 0.0}, "layers": []},
                    tolerance_scale=bad_tol,
                )

        # 2. Non-finite read-back value (`nan` / `inf`) under valid tolerance MUST fail verification
        ok_nan_rb, msgs_nan = c1_bridge.verify_readback(
            {"global": {"exposure": 1.0}},
            {"global_adjustments": {"exposure": float("nan")}, "layers": []},
            tolerance_scale=1.0,
        )
        self.assertFalse(ok_nan_rb)
        self.assertEqual(len(msgs_nan), 1)

        # +1 EV -> 0 EV under valid tolerance fails
        ok_zero, msgs_zero = c1_bridge.verify_readback(
            {"global": {"exposure": 1.0}},
            {"global_adjustments": {"exposure": 0.0}, "layers": []},
            tolerance_scale=1.0,
        )
        self.assertFalse(ok_zero)

        # 3. CLI `--tolerance-scale nan` and transaction pre-write check -> 0 calls to runner!
        with tempfile.TemporaryDirectory() as td:
            ledger_path = Path(td) / "batch.json"
            calls = {"n": 0}

            def never_called_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                calls["n"] += 1
                return "{}"

            for bad_tol in (float("nan"), float("inf"), 0.0):
                with self.assertRaises(ValueError):
                    c1_bridge.execute_apply_transaction(
                        ledger_path=ledger_path,
                        variant_id="clone-1",
                        spec={"global": {"exposure": 1.0}},
                        tolerance_scale=bad_tol,
                        runner_fn=never_called_runner,
                    )
                with self.assertRaises(ValueError):
                    c1_bridge.execute_rollback_transaction(
                        ledger_path=ledger_path,
                        photo_id="A",
                        tolerance_scale=bad_tol,
                        runner_fn=never_called_runner,
                    )
            self.assertEqual(calls["n"], 0, "Non-finite tolerance must be rejected BEFORE any bridge call!")

            with mock.patch("sys.stderr", io.StringIO()):
                rc_cli_nan = c1_bridge.main([
                    "apply",
                    "--batch-state", str(ledger_path),
                    "--variant-id", "clone-1",
                    "--spec", '{"global":{"exposure":1.0}}',
                    "--tolerance-scale", "nan",
                ])
            self.assertNotEqual(rc_cli_nan, 0)

    # =========================================================================
    # Round 6 Counter-Examples (All 4 Closing Conditions)
    # =========================================================================

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r6_counterexample_1_two_phase_export_recovers_from_receipt_save_failure_and_mid_commit_crashes(self) -> None:
        """GPT R6 P1 #1: Simulate (a) crash before promote, (b) receipt save failure after promote, (c) crash after promote -> consistent file+receipt & `--overwrite-own` continues working."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            out_dir = root / "out"
            raw_dir.mkdir()
            out_dir.mkdir()
            (raw_dir / "A.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
            ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            rgb_box = {"rgb": (60, 70, 80)}

            def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                staged = staging_dir / f"{exact_stem}.jpg"
                make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=rgb_box["rgb"])
                return ({
                    "job_id": "j-r6",
                    "variant_id": var_id,
                    "image_name": "A.CR2",
                    "raw_path": str(raw_dir / "A.CR2"),
                    "raw_dimensions": [64, 48],
                    "orientation": 0,
                    "recipe_format": "JPEG",
                    "recipe_quality": 100,
                    "recipe_profile": "sRGB Color Space Profile",
                    "scaling_method": "Fixed",
                    "primary_scaling_value": 100,
                    "ignore_crop": True,
                    "export_crop_method": "ignore",
                }, staged)

            # Version 1 initial export
            c1_bridge.execute_export_workflow(
                mode="handoff",
                variant_id="clone-a",
                recipe="AI_Handoff_Full_sRGB100",
                output_dir=out_dir,
                batch_state_path=ledger_path,
                export_runner_fn=export_runner,
            )
            a_jpg = out_dir / "A.jpg"
            sha_v1, sz_v1 = c1_bridge.compute_file_sha256(a_jpg)

            # Bump adjustment_seq to 1 via `apply`
            step = {"n": 0, "exp": 0.20}

            def bump_apply_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                step["n"] += 1
                val = 0.0 if step["n"] == 1 else step["exp"]
                return json.dumps({
                    "variant_id": "clone-a",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(exposure=val),
                    "layers": [bg_layer],
                })

            c1_bridge.execute_apply_transaction(
                ledger_path=ledger_path,
                variant_id="clone-a",
                spec={"global": {"exposure": 0.20}},
                runner_fn=bump_apply_runner,
            )

            # (a) Simulate hard crash BEFORE file promotion (`pre_promote_hook` -> SystemExit)
            rgb_box["rgb"] = (110, 120, 130)
            with self.assertRaises(SystemExit):
                c1_bridge.execute_export_workflow(
                    mode="handoff",
                    variant_id="clone-a",
                    recipe="AI_Handoff_Full_sRGB100",
                    output_dir=out_dir,
                    batch_state_path=ledger_path,
                    overwrite_own=True,
                    export_runner_fn=export_runner,
                    pre_promote_hook=lambda: (_ for _ in ()).throw(SystemExit(81)),
                )
            reloaded_a = c1_bridge.load_batch_ledger(ledger_path)["photos"]["A"]
            self.assertNotIn("pending_export_commit", reloaded_a)
            self.assertEqual(c1_bridge.compute_file_sha256(a_jpg), (sha_v1, sz_v1))
            self.assertEqual(reloaded_a["owned_export_files"][str(a_jpg.resolve())]["sha256"], sha_v1)

            # (b) Simulate receipt save `OSError` AFTER `promote_staged_file` has replaced `A.jpg`!
            orig_save = c1_bridge.save_batch_ledger
            save_calls = {"n": 0}

            def fail_on_receipt_save(lp: Path, data: dict) -> None:
                save_calls["n"] += 1
                # Call 1 inside commit is `pending_export_commit` journal; Call 2 is final receipt save!
                if save_calls["n"] == 2:
                    raise OSError("Simulated disk full while saving final export_receipt")
                orig_save(lp, data)

            with mock.patch.object(c1_bridge, "save_batch_ledger", side_effect=fail_on_receipt_save):
                with self.assertRaisesRegex(OSError, "Simulated disk full"):
                    c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        overwrite_own=True,
                        export_runner_fn=export_runner,
                    )
            # Previous formal JPG MUST have been restored from backup, matching ledger `sha_v1`!
            self.assertEqual(c1_bridge.compute_file_sha256(a_jpg), (sha_v1, sz_v1))
            reloaded_b = c1_bridge.load_batch_ledger(ledger_path)["photos"]["A"]
            self.assertEqual(reloaded_b["owned_export_files"][str(a_jpg.resolve())]["sha256"], sha_v1)

            # Confirm `--overwrite-own` STILL works right after!
            # And in this run, test (c): hard crash (`SystemExit`) AFTER `promote_staged_file` before receipt write!
            with self.assertRaises(SystemExit):
                c1_bridge.execute_export_workflow(
                    mode="handoff",
                    variant_id="clone-a",
                    recipe="AI_Handoff_Full_sRGB100",
                    output_dir=out_dir,
                    batch_state_path=ledger_path,
                    overwrite_own=True,
                    export_runner_fn=export_runner,
                    post_promote_hook=lambda: (_ for _ in ()).throw(SystemExit(82)),
                )
            sha_v2, sz_v2 = c1_bridge.compute_file_sha256(a_jpg)
            self.assertNotEqual(sha_v2, sha_v1)

            # On restart, `load_batch_ledger` auto-rolls forward the interrupted commit so file == receipt!
            reloaded_c = c1_bridge.load_batch_ledger(ledger_path)["photos"]["A"]
            self.assertTrue(reloaded_c["export_verified"])
            self.assertEqual(reloaded_c["export_receipt"]["sha256"], sha_v2)
            self.assertEqual(reloaded_c["owned_export_files"][str(a_jpg.resolve())]["sha256"], sha_v2)

            # Confirm subsequent `--overwrite-own` continues to work cleanly!
            rgb_box["rgb"] = (190, 200, 210)
            res_final = c1_bridge.execute_export_workflow(
                mode="handoff",
                variant_id="clone-a",
                recipe="AI_Handoff_Full_sRGB100",
                output_dir=out_dir,
                batch_state_path=ledger_path,
                overwrite_own=True,
                export_runner_fn=export_runner,
            )
            self.assertNotEqual(res_final["file"]["sha256"], sha_v2)

    def test_r6_counterexample_2_partial_multi_slider_layer_rollback_resumes_and_blocks_anomalies(self) -> None:
        """GPT R6 P1 #2: Layer rolling back `(exposure=1, contrast=20) -> (0, 0)` interrupted at `(0, 20)` -> resumes to `(0, 0)` while still blocking substitute/uninvolved anomalies with 0 writes."""
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            raw_dir.mkdir()
            (raw_dir / "IMG_0001.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-1", "position": 1, "raw_path": str(raw_dir / "IMG_0001.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["IMG_0001"]["cloned_variant_id"] = "clone-1"
            ledger["photos"]["IMG_0001"]["inherited_layers"] = [bg_layer]
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            # Round 1: Create `AI_Face_Skin` at `(exposure=0.0, contrast=0.0, saturation=0.0)`
            r1_n = {"n": 0}

            def r1_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                r1_n["n"] += 1
                layers = [bg_layer] if r1_n["n"] == 1 else [
                    bg_layer,
                    {
                        "index": 2,
                        "name": "AI_Face_Skin",
                        "kind": "adjustment",
                        "enabled": True,
                        "opacity": 100,
                        "adjustments": full_zero_adjustments(exposure=0.0, contrast=0.0, saturation=0.0),
                    },
                ]
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": layers,
                })

            c1_bridge.execute_apply_transaction(
                ledger_path=ledger_path,
                variant_id="clone-1",
                spec={"masks": {"face_skin": {"exposure": 0.0, "contrast": 0.0}}},
                runner_fn=r1_runner,
            )

            # Round 2: Modify `AI_Face_Skin` to `(exposure=1.0, contrast=20.0)`
            r2_n = {"n": 0}

            def r2_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                r2_n["n"] += 1
                exp_v, con_v = (0.0, 0.0) if r2_n["n"] == 1 else (1.0, 20.0)
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": [
                        bg_layer,
                        {
                            "index": 2,
                            "name": "AI_Face_Skin",
                            "kind": "adjustment",
                            "enabled": True,
                            "opacity": 100,
                            "adjustments": full_zero_adjustments(exposure=exp_v, contrast=con_v, saturation=0.0),
                        },
                    ],
                })

            c1_bridge.execute_apply_transaction(
                ledger_path=ledger_path,
                variant_id="clone-1",
                spec={"masks": {"face_skin": {"exposure": 1.0, "contrast": 20.0}}},
                runner_fn=r2_runner,
            )

            # First rollback attempt crashes mid-write AFTER restoring exposure=0.0, leaving `(exposure=0.0, contrast=20.0)`!
            rb1_n = {"n": 0}

            def crashing_rb_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                rb1_n["n"] += 1
                if rb1_n["n"] == 1:
                    return json.dumps({
                        "variant_id": "clone-1",
                        "orientation": 0,
                        "dimensions": [64, 48],
                        "crop": [0, 0, 64, 48],
                        "global_adjustments": full_zero_adjustments(),
                        "layers": [
                            bg_layer,
                            {
                                "index": 2,
                                "name": "AI_Face_Skin",
                                "kind": "adjustment",
                                "enabled": True,
                                "opacity": 100,
                                "adjustments": full_zero_adjustments(exposure=1.0, contrast=20.0, saturation=0.0),
                            },
                        ],
                    })
                raise SystemExit(77)

            with self.assertRaises(SystemExit):
                c1_bridge.execute_rollback_transaction(
                    ledger_path=ledger_path,
                    photo_id="IMG_0001",
                    runner_fn=crashing_rb_runner,
                )

            # Negative check A: Uninvolved field `saturation` drifted (e.g. 15.0) -> 0 write calls!
            neg_writes = {"w": 0}

            def uninvolved_drift_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                if "set exposure of" in script_body or "set contrast of" in script_body:
                    neg_writes["w"] += 1
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": [
                        bg_layer,
                        {
                            "index": 2,
                            "name": "AI_Face_Skin",
                            "kind": "adjustment",
                            "enabled": True,
                            "opacity": 100,
                            "adjustments": full_zero_adjustments(exposure=0.0, contrast=20.0, saturation=15.0),
                        },
                    ],
                })

            with self.assertRaisesRegex(c1_bridge.BridgeError, "SAFETY BLOCK.*saturation.*aborting before write"):
                c1_bridge.execute_rollback_transaction(
                    ledger_path=ledger_path,
                    photo_id="IMG_0001",
                    runner_fn=uninvolved_drift_runner,
                )
            self.assertEqual(neg_writes["w"], 0)

            # Negative check B: Substitute layer with exposure=3.0 (outside {1.0, 0.0}) -> 0 write calls!
            def substitute_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                if "set exposure of" in script_body or "set contrast of" in script_body:
                    neg_writes["w"] += 1
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": [
                        bg_layer,
                        {
                            "index": 2,
                            "name": "AI_Face_Skin",
                            "kind": "adjustment",
                            "enabled": True,
                            "opacity": 100,
                            "adjustments": full_zero_adjustments(exposure=3.0, contrast=20.0, saturation=0.0),
                        },
                    ],
                })

            with self.assertRaisesRegex(c1_bridge.BridgeError, "SAFETY BLOCK.*exposure.*aborting before write"):
                c1_bridge.execute_rollback_transaction(
                    ledger_path=ledger_path,
                    photo_id="IMG_0001",
                    runner_fn=substitute_runner,
                )
            self.assertEqual(neg_writes["w"], 0)

            # Positive check: Legitimate partial state `(exposure=0.0, contrast=20.0, saturation=0.0)` resumes & completes to `(0, 0)`!
            res_n = {"n": 0}

            def resume_partial_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                res_n["n"] += 1
                con_v = 20.0 if res_n["n"] == 1 else 0.0
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": [
                        bg_layer,
                        {
                            "index": 2,
                            "name": "AI_Face_Skin",
                            "kind": "adjustment",
                            "enabled": True,
                            "opacity": 100,
                            "adjustments": full_zero_adjustments(exposure=0.0, contrast=con_v, saturation=0.0),
                        },
                    ],
                })

            rb_ok = c1_bridge.execute_rollback_transaction(
                ledger_path=ledger_path,
                photo_id="IMG_0001",
                runner_fn=resume_partial_runner,
            )
            self.assertTrue(rb_ok["rolled_back"])
            self.assertEqual(rb_ok["remaining_rounds"], 1)

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r6_counterexample_3_late_stale_export_does_not_clear_newer_valid_export_verified(self) -> None:
        """GPT R6 P2 #3: Version 2 finishes export first, then Version 1 late commit is rejected -> Version 2 receipt & `export_verified=True` remain valid and pass handoff."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            out_dir = root / "out"
            raw_dir.mkdir()
            out_dir.mkdir()
            (raw_dir / "A.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"
            handoff_path = root / "handoff.jsonl"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
            ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
            ledger["photos"]["A"]["adjustment_seq"] = 1
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            # Start slow Version 1 export (`started_seq=1`); while it renders, Version 2 `apply` AND Version 2 `export` complete!
            v2_result_holder: dict[str, dict] = {}

            def slow_v1_export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                staged_v1 = staging_dir / f"{exact_stem}.jpg"
                make_jpeg_with_profile(staged_v1, 64, 48, srgb_icc, rgb=(30, 30, 30))

                # Meanwhile: Version 2 `apply` runs (`adjustment_seq` -> 2)
                app_n = {"n": 0}

                def v2_apply_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                    app_n["n"] += 1
                    exp = 0.0 if app_n["n"] == 1 else 0.30
                    return json.dumps({
                        "variant_id": "clone-a",
                        "orientation": 0,
                        "dimensions": [64, 48],
                        "crop": [0, 0, 64, 48],
                        "global_adjustments": full_zero_adjustments(exposure=exp),
                        "layers": [bg_layer],
                    })

                c1_bridge.execute_apply_transaction(
                    ledger_path=ledger_path,
                    variant_id="clone-a",
                    spec={"global": {"exposure": 0.30}},
                    runner_fn=v2_apply_runner,
                )

                # And Version 2 export finishes FIRST!
                def fast_v2_export_runner(vid2: str, rec2: str, stg2: Path, stem2: str):
                    staged_v2 = stg2 / f"{stem2}.jpg"
                    make_jpeg_with_profile(staged_v2, 64, 48, srgb_icc, rgb=(180, 200, 220))
                    return ({
                        "job_id": "j-v2",
                        "variant_id": vid2,
                        "image_name": "A.CR2",
                        "raw_path": str(raw_dir / "A.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": 100,
                        "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": "Fixed",
                        "primary_scaling_value": 100,
                        "ignore_crop": True,
                        "export_crop_method": "ignore",
                    }, staged_v2)

                v2_result_holder["res"] = c1_bridge.execute_export_workflow(
                    mode="handoff",
                    variant_id="clone-a",
                    recipe="AI_Handoff_Full_sRGB100",
                    output_dir=out_dir,
                    batch_state_path=ledger_path,
                    export_runner_fn=fast_v2_export_runner,
                )

                return ({
                    "job_id": "j-v1-late",
                    "variant_id": var_id,
                    "image_name": "A.CR2",
                    "raw_path": str(raw_dir / "A.CR2"),
                    "raw_dimensions": [64, 48],
                    "orientation": 0,
                    "recipe_format": "JPEG",
                    "recipe_quality": 100,
                    "recipe_profile": "sRGB Color Space Profile",
                    "scaling_method": "Fixed",
                    "primary_scaling_value": 100,
                    "ignore_crop": True,
                    "export_crop_method": "ignore",
                }, staged_v1)

            with self.assertRaisesRegex(c1_bridge.BridgeError, "Stale export rejected"):
                c1_bridge.execute_export_workflow(
                    mode="handoff",
                    variant_id="clone-a",
                    recipe="AI_Handoff_Full_sRGB100",
                    output_dir=out_dir,
                    batch_state_path=ledger_path,
                    overwrite_own=True,
                    export_runner_fn=slow_v1_export_runner,
                )

            # Assert Version 2 file hash, receipt, and `export_verified=True` ALL remained valid!
            v2_path = Path(v2_result_holder["res"]["file"]["path"])
            v2_sha = v2_result_holder["res"]["file"]["sha256"]
            self.assertEqual(c1_bridge.compute_file_sha256(v2_path)[0], v2_sha)

            after_ledger = c1_bridge.load_batch_ledger(ledger_path)["photos"]["A"]
            self.assertTrue(after_ledger["export_verified"])
            self.assertEqual(after_ledger["export_receipt"]["adjustment_seq"], 2)
            self.assertEqual(after_ledger["export_receipt"]["sha256"], v2_sha)

            # Actually pass `record-handoff` with status="passed"!
            ho_res = c1_bridge.upsert_handoff_record(
                handoff_path,
                {"photo_id": "A", "output_path": str(v2_path), "status": "passed"},
                ledger_path=ledger_path,
            )
            self.assertEqual(ho_res["summary"]["passed"], 1)
            self.assertTrue(ho_res["summary"]["balanced"])

    def test_r6_counterexample_4_feather_and_refine_verified_on_readback_blocked_before_write_and_new_layer_deleted_on_rollback(self) -> None:
        """GPT R6 P2 #4 & R7 P2 #5: (a) `feather=20` with official-serializer read-back fails `verify_readback`; (b) `feather`/`refine` blocked BEFORE write on BOTH new and existing layers; (c) new-layer rollback deletes the layer."""
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        # (a) Request `feather=20`, official-serializer read-back lacks feather result -> MUST return `(False, [...])`!
        ok_f, msgs_f = c1_bridge.verify_readback(
            {"masks": {"face_skin": {"exposure": 0.2, "feather": 20}}},
            {
                "global_adjustments": full_zero_adjustments(),
                "layers": [
                    {
                        "index": 2,
                        "name": "AI_Face_Skin",
                        "kind": "adjustment",
                        "enabled": True,
                        "opacity": 100,
                        "adjustments": full_zero_adjustments(exposure=0.2),
                    }
                ],
            },
        )
        self.assertFalse(ok_f)
        self.assertTrue(any("masks.face_skin.feather" in m for m in msgs_f))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            raw_dir.mkdir()
            (raw_dir / "IMG_0001.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-1", "position": 1, "raw_path": str(raw_dir / "IMG_0001.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["IMG_0001"]["cloned_variant_id"] = "clone-1"
            ledger["photos"]["IMG_0001"]["inherited_layers"] = [bg_layer]
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            # (b1) Requesting `feather` or `refine` on a BRAND-NEW layer MUST stop BEFORE write (`write == 0`)!
            new_op_counts = {"read": 0, "write": 0}

            def block_new_feather_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                if "create people mask" in script_body or "feather mask" in script_body or "refine mask" in script_body:
                    new_op_counts["write"] += 1
                else:
                    new_op_counts["read"] += 1
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": [bg_layer],
                })

            for op_name in ("feather", "refine"):
                with self.assertRaisesRegex(c1_bridge.BridgeError, f"SAFETY BLOCK.*Mask '{op_name}'.*aborting before write"):
                    c1_bridge.execute_apply_transaction(
                        ledger_path=ledger_path,
                        variant_id="clone-1",
                        spec={"masks": {"face_skin": {"exposure": 0.25, op_name: 20.0}}},
                        runner_fn=block_new_feather_runner,
                    )
            self.assertEqual(new_op_counts["write"], 0)

            # (c) Create brand-new layer with verifiable `exposure=0.25` using strictly official serializer fields!
            app_n = {"n": 0}

            def new_layer_official_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                app_n["n"] += 1
                layers = [bg_layer] if app_n["n"] == 1 else [
                    bg_layer,
                    {
                        "index": 2,
                        "name": "AI_Face_Skin",
                        "kind": "adjustment",
                        "enabled": True,
                        "opacity": 100,
                        "adjustments": full_zero_adjustments(exposure=0.25),
                    },
                ]
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": layers,
                })

            res_new = c1_bridge.execute_apply_transaction(
                ledger_path=ledger_path,
                variant_id="clone-1",
                spec={"masks": {"face_skin": {"exposure": 0.25}}},
                runner_fn=new_layer_official_runner,
            )
            self.assertTrue(res_new["verified"])

            # (b2) Now `AI_Face_Skin` is an EXISTING managed layer: requesting `feather` or `refine` MUST stop BEFORE write (`write == 0`)!
            existing_counts = {"read": 0, "write": 0}

            def block_existing_feather_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                if "feather mask" in script_body or "refine mask" in script_body or "set exposure" in script_body:
                    existing_counts["write"] += 1
                else:
                    existing_counts["read"] += 1
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": [
                        bg_layer,
                        {
                            "index": 2,
                            "name": "AI_Face_Skin",
                            "kind": "adjustment",
                            "enabled": True,
                            "opacity": 100,
                            "adjustments": full_zero_adjustments(exposure=0.25),
                        },
                    ],
                })

            for op_name in ("feather", "refine"):
                with self.assertRaisesRegex(c1_bridge.BridgeError, f"SAFETY BLOCK.*Mask '{op_name}'.*aborting before write"):
                    c1_bridge.execute_apply_transaction(
                        ledger_path=ledger_path,
                        variant_id="clone-1",
                        spec={"masks": {"face_skin": {op_name: 15.0}}},
                        runner_fn=block_existing_feather_runner,
                    )
            self.assertEqual(existing_counts["write"], 0)

            # Roll back Round 1 -> `delete targetLayer` removes `AI_Face_Skin` and verifies deletion!
            rb_n = {"n": 0}

            def delete_rb_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                rb_n["n"] += 1
                if rb_n["n"] == 1:
                    return json.dumps({
                        "variant_id": "clone-1",
                        "orientation": 0,
                        "dimensions": [64, 48],
                        "crop": [0, 0, 64, 48],
                        "global_adjustments": full_zero_adjustments(),
                        "layers": [
                            bg_layer,
                            {
                                "index": 2,
                                "name": "AI_Face_Skin",
                                "kind": "adjustment",
                                "enabled": True,
                                "opacity": 100,
                                "adjustments": full_zero_adjustments(exposure=0.25),
                            },
                        ],
                    })
                self.assertIn("delete targetLayer", script_body)
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": [bg_layer],
                })

            rb_del_res = c1_bridge.execute_rollback_transaction(
                ledger_path=ledger_path,
                photo_id="IMG_0001",
                runner_fn=delete_rb_runner,
            )
            self.assertTrue(rb_del_res["rolled_back"])
            self.assertEqual(
                c1_bridge.load_batch_ledger(ledger_path)["photos"]["IMG_0001"]["managed_layers"],
                {},
            )

    # =========================================================================
    # Round 7 Counter-Examples (All 5 Closing Conditions)
    # =========================================================================

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r7_counterexample_1_recovery_preserves_external_third_party_jpg_and_reports_conflict(self) -> None:
        """GPT R7 P1 #1: Crash after writing `pending_export_commit` before replacement, then client replaces formal path with a 3rd file -> reload preserves 3rd file byte-for-byte, backup, and pending record while reporting conflict."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        for export_case in ("overwrite_own_export", "initial_export"):
            with self.subTest(case=export_case), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                raw_dir = root / "raws"
                out_dir = root / "out"
                raw_dir.mkdir()
                out_dir.mkdir()
                (raw_dir / "A.CR2").write_bytes(b"raw")
                ledger_path = root / "batch.json"

                c1_bridge.register_batch_entries(
                    ledger_path,
                    c1_variants=[
                        {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                    ],
                    raw_dir=raw_dir,
                )
                ledger = c1_bridge.load_batch_ledger(ledger_path)
                ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
                ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
                c1_bridge.save_batch_ledger(ledger_path, ledger)

                rgb_box = {"rgb": (40, 50, 60)}

                def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                    staged = staging_dir / f"{exact_stem}.jpg"
                    make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=rgb_box["rgb"])
                    return ({
                        "job_id": "j-r7-1",
                        "variant_id": var_id,
                        "image_name": "A.CR2",
                        "raw_path": str(raw_dir / "A.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": 100,
                        "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": "Fixed",
                        "primary_scaling_value": 100,
                        "ignore_crop": True,
                        "export_crop_method": "ignore",
                    }, staged)

                a_jpg = out_dir / "A.jpg"
                old_bytes: bytes | None = None
                if export_case == "overwrite_own_export":
                    c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        export_runner_fn=export_runner,
                    )
                    old_bytes = a_jpg.read_bytes()

                # Interrupt AFTER writing `pending_export_commit` and BEFORE replacing `out/A.jpg`
                rgb_box["rgb"] = (120, 130, 140)
                with self.assertRaises(SystemExit):
                    c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        overwrite_own=(export_case == "overwrite_own_export"),
                        export_runner_fn=export_runner,
                        pre_promote_hook=lambda: (_ for _ in ()).throw(SystemExit(91)),
                    )

                raw_on_disk = json.loads(ledger_path.read_text(encoding="utf-8"))
                pending_before = raw_on_disk["photos"]["A"]["pending_export_commit"]
                backup_path = Path(pending_before["backup_path"]) if pending_before.get("backup_path") else None

                # Client places a 3rd distinct file at `out/A.jpg`!
                third_bytes = b"\xff\xd8\xff\xe0CLIENT_THIRD_PARTY_CUSTOM_FILE_BYTES_9876543210\xff\xd9"
                a_jpg.write_bytes(third_bytes)

                # Reloading batch ledger MUST report conflict and NEVER overwrite `third_bytes`!
                with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                    c1_bridge.load_batch_ledger(ledger_path)

                # 1. Third-party file preserved byte-for-byte!
                self.assertEqual(a_jpg.read_bytes(), third_bytes)

                # 2. Backup preserved byte-for-byte (when prior file existed)!
                if export_case == "overwrite_own_export":
                    assert backup_path is not None and old_bytes is not None
                    self.assertTrue(backup_path.is_file(), "Backup file must be preserved on conflict!")
                    self.assertEqual(backup_path.read_bytes(), old_bytes)

                # 3. `pending_export_commit` preserved in ledger with conflict details!
                after_raw = json.loads(ledger_path.read_text(encoding="utf-8"))
                self.assertIn("pending_export_commit", after_raw["photos"]["A"])
                self.assertEqual(
                    after_raw["photos"]["A"]["pending_export_commit"]["conflict"]["reason"],
                    "external_file_conflict",
                )

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r7_counterexample_2_preview_manifest_save_failure_recovers_initial_and_overwrite_exports(self) -> None:
        """GPT R7 P1 #2: Simulate `save_preview_manifest` raising `OSError` (and mid-manifest crash) on BOTH initial export and `--overwrite-own` re-export -> restart has consistent file+receipt and completes next same-path re-export."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        for scenario in ("initial_export_oserror", "overwrite_export_oserror", "post_replace_crash_before_manifest"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                raw_dir = root / "raws"
                out_dir = root / "out"
                raw_dir.mkdir()
                out_dir.mkdir()
                (raw_dir / "A.CR2").write_bytes(b"raw")
                ledger_path = root / "batch.json"

                c1_bridge.register_batch_entries(
                    ledger_path,
                    c1_variants=[
                        {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                    ],
                    raw_dir=raw_dir,
                )
                ledger = c1_bridge.load_batch_ledger(ledger_path)
                ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
                ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
                c1_bridge.save_batch_ledger(ledger_path, ledger)

                rgb_box = {"rgb": (35, 65, 95)}

                def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                    staged = staging_dir / f"{exact_stem}.jpg"
                    make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=rgb_box["rgb"])
                    return ({
                        "job_id": "j-r7-2",
                        "variant_id": var_id,
                        "image_name": "A.CR2",
                        "raw_path": str(raw_dir / "A.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": 100,
                        "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": "Fixed",
                        "primary_scaling_value": 100,
                        "ignore_crop": True,
                        "export_crop_method": "ignore",
                    }, staged)

                a_jpg = out_dir / "A.jpg"

                if scenario == "initial_export_oserror":
                    # Seed preview manifest with `A.jpg` so `unregister_from_preview_manifest` invokes `save_preview_manifest`
                    c1_bridge.save_preview_manifest(out_dir, {"A.jpg": "stale-preview-hash"})

                    with mock.patch.object(
                        c1_bridge,
                        "save_preview_manifest",
                        side_effect=OSError("Simulated preview manifest write failure"),
                    ):
                        with self.assertRaisesRegex(OSError, "Simulated preview manifest write failure"):
                            c1_bridge.execute_export_workflow(
                                mode="handoff",
                                variant_id="clone-a",
                                recipe="AI_Handoff_Full_sRGB100",
                                output_dir=out_dir,
                                batch_state_path=ledger_path,
                                export_runner_fn=export_runner,
                            )

                    # Restart / reload ledger -> no unreceipted file stranded at `A.jpg`, ledger & disk 100% consistent!
                    reloaded = c1_bridge.load_batch_ledger(ledger_path)["photos"]["A"]
                    self.assertFalse(a_jpg.exists())
                    self.assertFalse(reloaded["export_verified"])
                    self.assertIsNone(reloaded["export_receipt"])
                    self.assertNotIn("pending_export_commit", reloaded)

                    # Now complete initial export at same path AND then `--overwrite-own` re-export at same path!
                    exp1 = c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        export_runner_fn=export_runner,
                    )
                    self.assertEqual(c1_bridge.compute_file_sha256(a_jpg)[0], exp1["file"]["sha256"])

                    rgb_box["rgb"] = (155, 165, 175)
                    exp2 = c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        overwrite_own=True,
                        export_runner_fn=export_runner,
                    )
                    self.assertNotEqual(exp2["file"]["sha256"], exp1["file"]["sha256"])
                    reloaded2 = c1_bridge.load_batch_ledger(ledger_path)["photos"]["A"]
                    self.assertEqual(reloaded2["export_receipt"]["sha256"], exp2["file"]["sha256"])

                elif scenario == "overwrite_export_oserror":
                    # Complete v1 export first
                    exp_v1 = c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        export_runner_fn=export_runner,
                    )
                    sha_v1 = exp_v1["file"]["sha256"]

                    # Seed preview manifest entry so `unregister_from_preview_manifest` calls `save_preview_manifest`
                    c1_bridge.save_preview_manifest(out_dir, {"A.jpg": "stale-preview-hash"})
                    rgb_box["rgb"] = (200, 100, 50)

                    with mock.patch.object(
                        c1_bridge,
                        "save_preview_manifest",
                        side_effect=OSError("Simulated preview manifest disk error on overwrite"),
                    ):
                        with self.assertRaisesRegex(OSError, "Simulated preview manifest disk error"):
                            c1_bridge.execute_export_workflow(
                                mode="handoff",
                                variant_id="clone-a",
                                recipe="AI_Handoff_Full_sRGB100",
                                output_dir=out_dir,
                                batch_state_path=ledger_path,
                                overwrite_own=True,
                                export_runner_fn=export_runner,
                            )

                    # Restart / reload ledger -> `A.jpg` restored to `sha_v1`, matching `export_receipt`!
                    reloaded = c1_bridge.load_batch_ledger(ledger_path)["photos"]["A"]
                    self.assertEqual(c1_bridge.compute_file_sha256(a_jpg)[0], sha_v1)
                    self.assertEqual(reloaded["export_receipt"]["sha256"], sha_v1)
                    self.assertEqual(reloaded["owned_export_files"][str(a_jpg.resolve())]["sha256"], sha_v1)

                    # Actually complete the next `--overwrite-own` re-export at the exact same path!
                    exp_v2 = c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        overwrite_own=True,
                        export_runner_fn=export_runner,
                    )
                    self.assertNotEqual(exp_v2["file"]["sha256"], sha_v1)
                    self.assertEqual(c1_bridge.compute_file_sha256(a_jpg)[0], exp_v2["file"]["sha256"])
                    self.assertNotIn("A.jpg", c1_bridge.load_preview_manifest(out_dir))

                else:
                    # Crash inside `save_preview_manifest` (`SystemExit`) -> restart rolls forward & cleans manifest!
                    c1_bridge.save_preview_manifest(out_dir, {"A.jpg": "stale-preview-hash"})
                    with mock.patch.object(
                        c1_bridge,
                        "save_preview_manifest",
                        side_effect=SystemExit(92),
                    ):
                        with self.assertRaises(SystemExit):
                            c1_bridge.execute_export_workflow(
                                mode="handoff",
                                variant_id="clone-a",
                                recipe="AI_Handoff_Full_sRGB100",
                                output_dir=out_dir,
                                batch_state_path=ledger_path,
                                export_runner_fn=export_runner,
                            )

                    reloaded = c1_bridge.load_batch_ledger(ledger_path)["photos"]["A"]
                    live_sha = c1_bridge.compute_file_sha256(a_jpg)[0]
                    self.assertTrue(reloaded["export_verified"])
                    self.assertEqual(reloaded["export_receipt"]["sha256"], live_sha)
                    self.assertNotIn("A.jpg", c1_bridge.load_preview_manifest(out_dir))

                    # Complete next `--overwrite-own` re-export at same path!
                    rgb_box["rgb"] = (90, 190, 90)
                    exp_next = c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        overwrite_own=True,
                        export_runner_fn=export_runner,
                    )
                    self.assertNotEqual(exp_next["file"]["sha256"], live_sha)

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r7_counterexample_3_no_hardlink_subprocess_kill_leaves_no_partial_jpg_and_resumes_cleanly(self) -> None:
        """GPT R7 P1 #3: Force non-hardlink copy branch, kill real subprocess (`SIGKILL`) after writing partial bytes -> zero partial file at formal path, recovery cleans journal, and same-path initial export succeeds."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            out_dir = root / "out"
            raw_dir.mkdir()
            out_dir.mkdir()
            (raw_dir / "A.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"
            handoff_path = root / "handoff.jsonl"
            sample_jpg = root / "sample_source.jpg"
            make_jpeg_with_profile(sample_jpg, 64, 48, srgb_icc, rgb=(80, 120, 160))

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
            ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            # Spawn a REAL child process that disables `os.link`, writes 128 partial bytes during copy, and gets SIGKILLed!
            child_code = """
import errno, importlib.util, os, shutil, signal, sys
from pathlib import Path

script_path = Path(sys.argv[1])
ledger_path = Path(sys.argv[2])
out_dir = Path(sys.argv[3])
raw_path = Path(sys.argv[4])
sample_jpg = Path(sys.argv[5])

spec = importlib.util.spec_from_file_location("c1_bridge", script_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

orig_copyfileobj = shutil.copyfileobj

def no_hardlink(src, dst, *a, **kw):
    raise OSError(errno.ENOTSUP, "Simulated filesystem without hardlink support")

def crashing_copyfileobj(fsrc, fdst, length=0):
    chunk = fsrc.read(128)
    fdst.write(chunk)
    fdst.flush()
    os.fsync(fdst.fileno())
    os.kill(os.getpid(), signal.SIGKILL)

def export_runner(var_id, recipe, staging_dir, exact_stem):
    staged = staging_dir / f"{exact_stem}.jpg"
    with sample_jpg.open("rb") as sf, staged.open("wb") as df:
        orig_copyfileobj(sf, df)
    return ({
        "job_id": "j-subproc",
        "variant_id": var_id,
        "image_name": "A.CR2",
        "raw_path": str(raw_path),
        "raw_dimensions": [64, 48],
        "orientation": 0,
        "recipe_format": "JPEG",
        "recipe_quality": 100,
        "recipe_profile": "sRGB Color Space Profile",
        "scaling_method": "Fixed",
        "primary_scaling_value": 100,
        "ignore_crop": True,
        "export_crop_method": "ignore",
    }, staged)

os.link = no_hardlink
shutil.copyfileobj = crashing_copyfileobj

mod.execute_export_workflow(
    mode="handoff",
    variant_id="clone-a",
    recipe="AI_Handoff_Full_sRGB100",
    output_dir=out_dir,
    batch_state_path=ledger_path,
    export_runner_fn=export_runner,
)
"""
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    child_code,
                    str(SCRIPT_PATH),
                    str(ledger_path),
                    str(out_dir),
                    str(raw_dir / "A.CR2"),
                    str(sample_jpg),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertLess(proc.returncode, 0, f"Expected SIGKILL negative returncode, got {proc.returncode}: {proc.stderr}")

            a_jpg = out_dir / "A.jpg"
            # Before reload: journal exists in ledger, and NO partial file occupies `out/A.jpg`!
            pre_reload_raw = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertIn("pending_export_commit", pre_reload_raw["photos"]["A"])
            temp_pub_recorded = Path(pre_reload_raw["photos"]["A"]["pending_export_commit"]["temp_publish_path"])
            self.assertTrue(temp_pub_recorded.is_file(), "Partial bytes must live ONLY in hidden temp_publish_path!")
            self.assertFalse(a_jpg.exists(), "Formal output path MUST NOT have a half-written JPG!")

            # Reload ledger -> cleans `pending_export_commit` and deletes partial hidden temp file!
            reloaded = c1_bridge.load_batch_ledger(ledger_path)
            self.assertNotIn("pending_export_commit", reloaded["photos"]["A"])
            self.assertFalse(temp_pub_recorded.exists())
            self.assertFalse(a_jpg.exists())

            # Still with `os.link` disabled (`ENOTSUP`), verify:
            # 1) Pre-existing foreign file at `A.jpg` is NEVER overwritten;
            # 2) Once clear, initial export to `out/A.jpg` succeeds cleanly!
            def no_link_side_effect(*_a, **_kw):
                raise OSError(c1_bridge.errno.ENOTSUP, "Hardlinks disabled")

            def clean_export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                staged = staging_dir / f"{exact_stem}.jpg"
                staged.write_bytes(sample_jpg.read_bytes())
                return ({
                    "job_id": "j-retry",
                    "variant_id": var_id,
                    "image_name": "A.CR2",
                    "raw_path": str(raw_dir / "A.CR2"),
                    "raw_dimensions": [64, 48],
                    "orientation": 0,
                    "recipe_format": "JPEG",
                    "recipe_quality": 100,
                    "recipe_profile": "sRGB Color Space Profile",
                    "scaling_method": "Fixed",
                    "primary_scaling_value": 100,
                    "ignore_crop": True,
                    "export_crop_method": "ignore",
                }, staged)

            with mock.patch("os.link", side_effect=no_link_side_effect):
                foreign_bytes = b"FOREIGN_OCCUPANT"
                a_jpg.write_bytes(foreign_bytes)
                with self.assertRaisesRegex(c1_bridge.BridgeError, "Refusing to overwrite existing file"):
                    c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        export_runner_fn=clean_export_runner,
                    )
                self.assertEqual(a_jpg.read_bytes(), foreign_bytes)
                a_jpg.unlink()

                res_ok = c1_bridge.execute_export_workflow(
                    mode="handoff",
                    variant_id="clone-a",
                    recipe="AI_Handoff_Full_sRGB100",
                    output_dir=out_dir,
                    batch_state_path=ledger_path,
                    export_runner_fn=clean_export_runner,
                )

            self.assertTrue(a_jpg.is_file())
            self.assertEqual(a_jpg.read_bytes(), sample_jpg.read_bytes())
            ho = c1_bridge.upsert_handoff_record(
                handoff_path,
                {"photo_id": "A", "output_path": res_ok["file"]["path"], "status": "passed"},
                ledger_path=ledger_path,
            )
            self.assertEqual(ho["summary"]["passed"], 1)
            self.assertTrue(ho["summary"]["balanced"])

    def test_r7_counterexample_4_multi_new_layer_partial_deletion_rollback_resumes_and_blocks_reorder_or_substitute(self) -> None:
        """GPT R7 P2 #4: Same round creates layer 2 (`AI_Face_Skin`) and layer 3 (`AI_Clothes`); rollback deletes layer 2 then crashes so clothes moves `3 -> 2` -> resume deletes clothes while still blocking unrelated reorders & substitute layers with 0 writes."""
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }
        # Include an inherited layer at index 4 in a sub-case or test exact 2,3 + inherited reorder block!
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            raw_dir.mkdir()
            (raw_dir / "IMG_0001.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-1", "position": 1, "raw_path": str(raw_dir / "IMG_0001.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["IMG_0001"]["cloned_variant_id"] = "clone-1"
            ledger["photos"]["IMG_0001"]["inherited_layers"] = [bg_layer]
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            # Create `AI_Face_Skin` (index 2, +0.25 EV) and `AI_Clothes` (index 3, -0.20 EV) in the same round!
            face_lyr = {
                "index": 2,
                "name": "AI_Face_Skin",
                "kind": "adjustment",
                "enabled": True,
                "opacity": 100,
                "adjustments": full_zero_adjustments(exposure=0.25),
            }
            clothes_lyr_idx3 = {
                "index": 3,
                "name": "AI_Clothes",
                "kind": "adjustment",
                "enabled": True,
                "opacity": 100,
                "adjustments": full_zero_adjustments(exposure=-0.20),
            }

            app_n = {"n": 0}

            def create_two_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                app_n["n"] += 1
                layers = [bg_layer] if app_n["n"] == 1 else [bg_layer, face_lyr, clothes_lyr_idx3]
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": layers,
                })

            res_app = c1_bridge.execute_apply_transaction(
                ledger_path=ledger_path,
                variant_id="clone-1",
                spec={"masks": {"face_skin": {"exposure": 0.25}, "clothes": {"exposure": -0.20}}},
                runner_fn=create_two_runner,
            )
            self.assertTrue(res_app["verified"])

            # First rollback crashes AFTER deleting layer 2 (`AI_Face_Skin`), so `AI_Clothes` shifts from 3 -> 2!
            rb1_n = {"n": 0}

            def crash_between_deletes_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                rb1_n["n"] += 1
                if rb1_n["n"] == 1:
                    return json.dumps({
                        "variant_id": "clone-1",
                        "orientation": 0,
                        "dimensions": [64, 48],
                        "crop": [0, 0, 64, 48],
                        "global_adjustments": full_zero_adjustments(),
                        "layers": [bg_layer, face_lyr, clothes_lyr_idx3],
                    })
                raise SystemExit(94)

            with self.assertRaises(SystemExit):
                c1_bridge.execute_rollback_transaction(
                    ledger_path=ledger_path,
                    photo_id="IMG_0001",
                    runner_fn=crash_between_deletes_runner,
                )

            # Negative check A: Unrelated reorder where NEITHER layer was deleted, yet layer 2 & 3 swapped (`AI_Clothes` at 2, `AI_Face_Skin` at 3) -> 0 writes!
            neg_writes = {"w": 0}

            def swapped_reorder_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                if "delete targetLayer" in script_body:
                    neg_writes["w"] += 1
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": [
                        bg_layer,
                        dict(clothes_lyr_idx3, index=2),
                        dict(face_lyr, index=3),
                    ],
                })

            with self.assertRaisesRegex(c1_bridge.BridgeError, "SAFETY BLOCK.*stack index changed"):
                c1_bridge.execute_rollback_transaction(
                    ledger_path=ledger_path,
                    photo_id="IMG_0001",
                    runner_fn=swapped_reorder_runner,
                )
            self.assertEqual(neg_writes["w"], 0)

            # Negative check B: `AI_Face_Skin` deleted, `AI_Clothes` is at index 2, but replaced by a same-name substitute (+1.5 EV != -0.2 EV) -> 0 writes!
            def substitute_clothes_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                if "delete targetLayer" in script_body:
                    neg_writes["w"] += 1
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": [
                        bg_layer,
                        {
                            "index": 2,
                            "name": "AI_Clothes",
                            "kind": "adjustment",
                            "enabled": True,
                            "opacity": 100,
                            "adjustments": full_zero_adjustments(exposure=1.50),
                        },
                    ],
                })

            with self.assertRaisesRegex(c1_bridge.BridgeError, "SAFETY BLOCK.*exposure.*aborting before write"):
                c1_bridge.execute_rollback_transaction(
                    ledger_path=ledger_path,
                    photo_id="IMG_0001",
                    runner_fn=substitute_clothes_runner,
                )
            self.assertEqual(neg_writes["w"], 0)

            # Positive check: Legitimate state after deleting layer 2 (`AI_Clothes` shifted from 3 -> 2) resumes & completes rollback!
            res_rb_n = {"n": 0}

            def resume_remaining_delete_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                res_rb_n["n"] += 1
                if res_rb_n["n"] == 1:
                    return json.dumps({
                        "variant_id": "clone-1",
                        "orientation": 0,
                        "dimensions": [64, 48],
                        "crop": [0, 0, 64, 48],
                        "global_adjustments": full_zero_adjustments(),
                        "layers": [bg_layer, dict(clothes_lyr_idx3, index=2)],
                    })
                self.assertIn("delete targetLayer", script_body)
                return json.dumps({
                    "variant_id": "clone-1",
                    "orientation": 0,
                    "dimensions": [64, 48],
                    "crop": [0, 0, 64, 48],
                    "global_adjustments": full_zero_adjustments(),
                    "layers": [bg_layer],
                })

            rb_done = c1_bridge.execute_rollback_transaction(
                ledger_path=ledger_path,
                photo_id="IMG_0001",
                runner_fn=resume_remaining_delete_runner,
            )
            self.assertTrue(rb_done["rolled_back"])
            self.assertEqual(rb_done["remaining_rounds"], 0)
            self.assertEqual(
                c1_bridge.load_batch_ledger(ledger_path)["photos"]["IMG_0001"]["managed_layers"],
                {},
            )

    def test_r7_counterexample_5_serializer_layer_schema_parity_and_prewrite_rejection_of_feather_refine(self) -> None:
        """GPT R7 P2 #5: Official AppleScript serializer outputs strictly `{index, name, kind, enabled, opacity, adjustments}` (no `feather`/`refine`); `build_apply_script` and `execute_apply_transaction` reject `feather`/`refine` before write on new layers too."""
        # Verify exact layer keys emitted by `serializeVariantFull` in APPLESCRIPT_STATIC_HELPERS
        helper_src = c1_bridge.APPLESCRIPT_STATIC_HELPERS
        self.assertNotIn('\\"feather\\":', helper_src)
        self.assertNotIn('\\"refine\\":', helper_src)
        for expected_key in ("index", "name", "kind", "enabled", "opacity", "adjustments"):
            self.assertIn(f'\\"{expected_key}\\":', helper_src)

        # Direct `build_apply_script` MUST reject `feather` and `refine` on brand-new layers too
        for op_name in ("feather", "refine"):
            with self.assertRaisesRegex(c1_bridge.BridgeError, f"SAFETY BLOCK.*Mask '{op_name}'"):
                c1_bridge.build_apply_script({"masks": {"background": {"exposure": -0.3, op_name: 10}}})

    # =========================================================================
    # Round 8 Counter-Examples (All 4 Closing Conditions)
    # =========================================================================

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r8_counterexample_1_in_process_and_restart_overwrite_failure_both_preserve_third_file_backup_and_pending(self) -> None:
        """GPT R8 P1 #1: Swap formal JPG to 3rd file after `pending_export_commit` before publish -> BOTH in-process error handling AND process restart MUST preserve 3rd file byte-for-byte, old backup, and `pending_export_commit` while reporting conflict."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        for exec_mode in ("in_process_continue", "process_restart"):
            with self.subTest(mode=exec_mode), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                raw_dir = root / "raws"
                out_dir = root / "out"
                raw_dir.mkdir()
                out_dir.mkdir()
                (raw_dir / "A.CR2").write_bytes(b"raw")
                ledger_path = root / "batch.json"

                c1_bridge.register_batch_entries(
                    ledger_path,
                    c1_variants=[
                        {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                    ],
                    raw_dir=raw_dir,
                )
                ledger = c1_bridge.load_batch_ledger(ledger_path)
                ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
                ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
                c1_bridge.save_batch_ledger(ledger_path, ledger)

                rgb_box = {"rgb": (45, 55, 65)}

                def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                    staged = staging_dir / f"{exact_stem}.jpg"
                    make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=rgb_box["rgb"])
                    return ({
                        "job_id": "j-r8-1",
                        "variant_id": var_id,
                        "image_name": "A.CR2",
                        "raw_path": str(raw_dir / "A.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": 100,
                        "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": "Fixed",
                        "primary_scaling_value": 100,
                        "ignore_crop": True,
                        "export_crop_method": "ignore",
                    }, staged)

                # Version 1 initial export
                c1_bridge.execute_export_workflow(
                    mode="handoff",
                    variant_id="clone-a",
                    recipe="AI_Handoff_Full_sRGB100",
                    output_dir=out_dir,
                    batch_state_path=ledger_path,
                    export_runner_fn=export_runner,
                )
                a_jpg = out_dir / "A.jpg"
                old_bytes = a_jpg.read_bytes()

                third_bytes = b"\xff\xd8\xff\xe0EXTERNAL_CLIENT_REPLACEMENT_DURING_COMMIT_00112233\xff\xd9"
                rgb_box["rgb"] = (175, 185, 195)

                if exec_mode == "in_process_continue":
                    def swap_without_exit() -> None:
                        # Process stays alive! `promote_staged_file` will reject overwrite and enter `except Exception`!
                        a_jpg.write_bytes(third_bytes)

                    with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                        c1_bridge.execute_export_workflow(
                            mode="handoff",
                            variant_id="clone-a",
                            recipe="AI_Handoff_Full_sRGB100",
                            output_dir=out_dir,
                            batch_state_path=ledger_path,
                            overwrite_own=True,
                            export_runner_fn=export_runner,
                            pre_promote_hook=swap_without_exit,
                        )
                else:
                    def swap_and_crash() -> None:
                        a_jpg.write_bytes(third_bytes)
                        raise SystemExit(101)

                    with self.assertRaises(SystemExit):
                        c1_bridge.execute_export_workflow(
                            mode="handoff",
                            variant_id="clone-a",
                            recipe="AI_Handoff_Full_sRGB100",
                            output_dir=out_dir,
                            batch_state_path=ledger_path,
                            overwrite_own=True,
                            export_runner_fn=export_runner,
                            pre_promote_hook=swap_and_crash,
                        )

                    with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                        c1_bridge.load_batch_ledger(ledger_path)

                # BOTH paths MUST preserve: (1) 3rd file byte-for-byte, (2) old backup byte-for-byte, (3) pending_export_commit!
                self.assertEqual(a_jpg.read_bytes(), third_bytes, f"3rd file overwritten in {exec_mode}!")

                disk_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                self.assertIn("pending_export_commit", disk_ledger["photos"]["A"])
                pending_rec = disk_ledger["photos"]["A"]["pending_export_commit"]
                self.assertEqual(pending_rec["conflict"]["reason"], "external_file_conflict")

                backup_path = Path(pending_rec["backup_path"])
                self.assertTrue(backup_path.is_file(), f"Backup deleted prematurely in {exec_mode}!")
                self.assertEqual(backup_path.read_bytes(), old_bytes)

                # Subsequent reload also consistently reports conflict while keeping all 3 artifacts intact
                with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                    c1_bridge.load_batch_ledger(ledger_path)
                self.assertEqual(a_jpg.read_bytes(), third_bytes)
                self.assertEqual(backup_path.read_bytes(), old_bytes)

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r8_counterexample_2_missing_atomic_no_overwrite_capability_aborts_cleanly_preserving_early_arrival_file(self) -> None:
        """GPT R8 P1 #2: When BOTH `os.link` and `RENAME_EXCL` are unavailable, fail closed WITHOUT calling `os.replace`; concurrent early-arriving same-name file retains exact bytes and NO receipt is issued."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        for sub_case in ("early_arrival_in_publish_window", "empty_dir_no_atomic_primitive"):
            with self.subTest(case=sub_case), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                raw_dir = root / "raws"
                out_dir = root / "out"
                raw_dir.mkdir()
                out_dir.mkdir()
                (raw_dir / "A.CR2").write_bytes(b"raw")
                ledger_path = root / "batch.json"

                c1_bridge.register_batch_entries(
                    ledger_path,
                    c1_variants=[
                        {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                    ],
                    raw_dir=raw_dir,
                )
                ledger = c1_bridge.load_batch_ledger(ledger_path)
                ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
                ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
                c1_bridge.save_batch_ledger(ledger_path, ledger)

                def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                    staged = staging_dir / f"{exact_stem}.jpg"
                    make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=(70, 110, 150))
                    return ({
                        "job_id": "j-r8-2",
                        "variant_id": var_id,
                        "image_name": "A.CR2",
                        "raw_path": str(raw_dir / "A.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": 100,
                        "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": "Fixed",
                        "primary_scaling_value": 100,
                        "ignore_crop": True,
                        "export_crop_method": "ignore",
                    }, staged)

                a_jpg = out_dir / "A.jpg"
                early_bytes = b"\xff\xd8\xff\xe0EARLY_ARRIVAL_EXTERNAL_FILE_BYTES_KEEP_SAFE\xff\xd9"

                def no_link(*_a, **_kw):
                    raise OSError(c1_bridge.errno.ENOTSUP, "Hardlinks unsupported")

                def no_renamex_with_race(src_p: Path, dst_p: Path) -> tuple[bool, int]:
                    if sub_case == "early_arrival_in_publish_window":
                        # External process creates `A.jpg` right inside the publish fallback window!
                        dst_p.write_bytes(early_bytes)
                    return (False, c1_bridge.errno.ENOTSUP)

                with mock.patch("os.link", side_effect=no_link), mock.patch.object(
                    c1_bridge, "call_macos_renamex_excl", side_effect=no_renamex_with_race
                ):
                    with self.assertRaisesRegex(
                        c1_bridge.BridgeError,
                        "EXPORT RECOVERY CONFLICT|supports neither hardlinks",
                    ):
                        c1_bridge.execute_export_workflow(
                            mode="handoff",
                            variant_id="clone-a",
                            recipe="AI_Handoff_Full_sRGB100",
                            output_dir=out_dir,
                            batch_state_path=ledger_path,
                            export_runner_fn=export_runner,
                        )

                raw_after = json.loads(ledger_path.read_text(encoding="utf-8"))["photos"]["A"]
                self.assertFalse(raw_after["export_verified"], "Must NOT issue export_verified=True!")
                self.assertIsNone(raw_after["export_receipt"], "Must NOT issue export_receipt!")

                if sub_case == "early_arrival_in_publish_window":
                    self.assertTrue(a_jpg.is_file())
                    self.assertEqual(a_jpg.read_bytes(), early_bytes, "Early-arriving file bytes MUST be preserved!")
                else:
                    self.assertFalse(a_jpg.exists())
                    self.assertNotIn("pending_export_commit", raw_after)

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r8_counterexample_3_post_promote_swap_before_receipt_blocks_commit_and_preserves_swapped_file(self) -> None:
        """GPT R8 P1 #3: Swap formal file in `post_promote_hook` (after publish, before receipt save) on BOTH initial export and `--overwrite-own` re-export -> reject commit, preserve swapped file byte-for-byte, report conflict, NEVER issue mismatched receipt."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        for export_case in ("initial_export", "overwrite_own_export", "swapped_to_symlink"):
            with self.subTest(case=export_case), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                raw_dir = root / "raws"
                out_dir = root / "out"
                raw_dir.mkdir()
                out_dir.mkdir()
                (raw_dir / "A.CR2").write_bytes(b"raw")
                ledger_path = root / "batch.json"

                c1_bridge.register_batch_entries(
                    ledger_path,
                    c1_variants=[
                        {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                    ],
                    raw_dir=raw_dir,
                )
                ledger = c1_bridge.load_batch_ledger(ledger_path)
                ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
                ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
                c1_bridge.save_batch_ledger(ledger_path, ledger)

                rgb_box = {"rgb": (30, 60, 90)}

                def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                    staged = staging_dir / f"{exact_stem}.jpg"
                    make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=rgb_box["rgb"])
                    return ({
                        "job_id": "j-r8-3",
                        "variant_id": var_id,
                        "image_name": "A.CR2",
                        "raw_path": str(raw_dir / "A.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": 100,
                        "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": "Fixed",
                        "primary_scaling_value": 100,
                        "ignore_crop": True,
                        "export_crop_method": "ignore",
                    }, staged)

                a_jpg = out_dir / "A.jpg"
                v1_bytes: bytes | None = None
                v1_sha: str | None = None

                if export_case == "overwrite_own_export":
                    exp_v1 = c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        export_runner_fn=export_runner,
                    )
                    v1_bytes = a_jpg.read_bytes()
                    v1_sha = exp_v1["file"]["sha256"]

                    # Bump adjustment_seq to 1 via `apply` so v1 receipt is cleared
                    step = {"n": 0}

                    def bump_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                        step["n"] += 1
                        val = 0.0 if step["n"] == 1 else 0.25
                        return json.dumps({
                            "variant_id": "clone-a",
                            "orientation": 0,
                            "dimensions": [64, 48],
                            "crop": [0, 0, 64, 48],
                            "global_adjustments": full_zero_adjustments(exposure=val),
                            "layers": [bg_layer],
                        })

                    c1_bridge.execute_apply_transaction(
                        ledger_path=ledger_path,
                        variant_id="clone-a",
                        spec={"global": {"exposure": 0.25}},
                        runner_fn=bump_runner,
                    )

                swapped_bytes = b"\xff\xd8\xff\xe0POST_PROMOTE_SWAPPED_EXTERNAL_BYTES_778899\xff\xd9"
                ext_link_target = root / "elsewhere.jpg"
                rgb_box["rgb"] = (160, 170, 180)

                def post_promote_tamper() -> None:
                    if export_case == "swapped_to_symlink":
                        a_jpg.unlink()
                        a_jpg.symlink_to(ext_link_target)
                    else:
                        a_jpg.write_bytes(swapped_bytes)

                with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                    c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        overwrite_own=(export_case == "overwrite_own_export"),
                        export_runner_fn=export_runner,
                        post_promote_hook=post_promote_tamper,
                    )

                # 1. Live destination preserved untouched!
                if export_case == "swapped_to_symlink":
                    self.assertTrue(a_jpg.is_symlink())
                else:
                    self.assertEqual(a_jpg.read_bytes(), swapped_bytes)

                # 2. Ledger MUST NOT have `export_verified == True` or a mismatched receipt!
                after_entry = json.loads(ledger_path.read_text(encoding="utf-8"))["photos"]["A"]
                self.assertFalse(after_entry["export_verified"])
                self.assertIsNone(after_entry["export_receipt"])
                self.assertIn("pending_export_commit", after_entry)

                if export_case == "overwrite_own_export":
                    assert v1_bytes is not None and v1_sha is not None
                    bak_p = Path(after_entry["pending_export_commit"]["backup_path"])
                    self.assertTrue(bak_p.is_file())
                    self.assertEqual(bak_p.read_bytes(), v1_bytes)
                    self.assertEqual(after_entry["owned_export_files"][str(a_jpg.resolve())]["sha256"], v1_sha)

                # 3. Reloading ledger also reports `EXPORT RECOVERY CONFLICT`
                with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                    c1_bridge.load_batch_ledger(ledger_path)

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r8_counterexample_4_dangling_symlink_to_outside_dir_blocked_without_creating_external_target(self) -> None:
        """GPT R8 P2 #4: Dangling symlink at output filename pointing outside `output_dir` -> blocked on BOTH handoff & preview, symlink preserved, outside target/dir NEVER created, ledger NOT marked verified."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        for exp_mode in ("handoff", "preview"):
            with self.subTest(mode=exp_mode), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                raw_dir = root / "raws"
                out_dir = root / "out"
                outside_vault = root / "outside_vault"
                outside_target = outside_vault / "nested" / "escaped.jpg"
                raw_dir.mkdir()
                out_dir.mkdir()
                (raw_dir / "A.CR2").write_bytes(b"raw")
                ledger_path = root / "batch.json"

                c1_bridge.register_batch_entries(
                    ledger_path,
                    c1_variants=[
                        {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                    ],
                    raw_dir=raw_dir,
                )
                ledger = c1_bridge.load_batch_ledger(ledger_path)
                ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
                ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
                c1_bridge.save_batch_ledger(ledger_path, ledger)

                # Create a dangling symlink inside `out_dir` pointing to non-existent `outside_target`!
                dangling_link = out_dir / "A.jpg"
                dangling_link.symlink_to(outside_target)
                self.assertTrue(dangling_link.is_symlink())
                self.assertFalse(dangling_link.exists())
                self.assertFalse(outside_vault.exists())

                def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                    staged = staging_dir / f"{exact_stem}.jpg"
                    make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=(100, 120, 140))
                    return ({
                        "job_id": "j-r8-4",
                        "variant_id": var_id,
                        "image_name": "A.CR2",
                        "raw_path": str(raw_dir / "A.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": 100,
                        "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": "Fixed",
                        "primary_scaling_value": 100,
                        "ignore_crop": True,
                        "export_crop_method": "ignore",
                    }, staged)

                with self.assertRaisesRegex(c1_bridge.BridgeError, "symlink or non-regular"):
                    c1_bridge.execute_export_workflow(
                        mode=exp_mode,
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100" if exp_mode == "handoff" else "AI_Preview_1536_NoMeta",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        overwrite_own=True,
                        export_runner_fn=export_runner,
                    )

                # 1. Dangling symlink itself is preserved intact!
                self.assertTrue(dangling_link.is_symlink())
                self.assertFalse(dangling_link.exists())
                self.assertEqual(Path(c1_bridge.os.readlink(dangling_link)), outside_target)

                # 2. Outside target and outside parent directory were NEVER created!
                self.assertFalse(outside_target.exists())
                self.assertFalse(outside_vault.exists())

                # 3. Ledger was NEVER marked verified!
                reloaded_a = c1_bridge.load_batch_ledger(ledger_path)["photos"]["A"]
                self.assertFalse(reloaded_a["export_verified"])
                self.assertIsNone(reloaded_a["export_receipt"])
                self.assertNotIn("pending_export_commit", reloaded_a)

    # =========================================================================
    # Round 9 Counter-Examples (All 5 Closing Conditions)
    # =========================================================================

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r9_counterexample_1_same_byte_early_arrival_file_preserved_without_receipt_on_abort_and_restart(self) -> None:
        """GPT R9 P1 #1: External writer places an IDENTICAL-BYTE JPG at formal path before publish -> BOTH in-process EEXIST abort AND pre-publish crash restart MUST preserve the early-arriving file (same inode & bytes) and MUST NOT grant it an export receipt."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        with tempfile.TemporaryDirectory() as td_sample:
            canonical_jpg = Path(td_sample) / "canonical.jpg"
            make_jpeg_with_profile(canonical_jpg, 64, 48, srgb_icc, rgb=(88, 128, 168))
            identical_bytes = canonical_jpg.read_bytes()

        for path_mode in ("in_process_hardlink_eexist", "in_process_no_hardlink_eexist", "crash_before_publish_restart"):
            with self.subTest(mode=path_mode), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                raw_dir = root / "raws"
                out_dir = root / "out"
                raw_dir.mkdir()
                out_dir.mkdir()
                (raw_dir / "A.CR2").write_bytes(b"raw")
                ledger_path = root / "batch.json"

                c1_bridge.register_batch_entries(
                    ledger_path,
                    c1_variants=[
                        {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                    ],
                    raw_dir=raw_dir,
                )
                ledger = c1_bridge.load_batch_ledger(ledger_path)
                ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
                ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
                c1_bridge.save_batch_ledger(ledger_path, ledger)

                def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                    staged = staging_dir / f"{exact_stem}.jpg"
                    staged.write_bytes(identical_bytes)  # Exact same bytes as external file!
                    return ({
                        "job_id": "j-r9-1",
                        "variant_id": var_id,
                        "image_name": "A.CR2",
                        "raw_path": str(raw_dir / "A.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": 100,
                        "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": "Fixed",
                        "primary_scaling_value": 100,
                        "ignore_crop": True,
                        "export_crop_method": "ignore",
                    }, staged)

                a_jpg = out_dir / "A.jpg"
                ext_inode_box: dict[str, tuple[int, int]] = {}

                if path_mode in ("in_process_hardlink_eexist", "in_process_no_hardlink_eexist"):
                    def place_identical_early_file() -> None:
                        a_jpg.write_bytes(identical_bytes)
                        st = c1_bridge.os.lstat(a_jpg)
                        ext_inode_box["id"] = (int(st.st_dev), int(st.st_ino))

                    ctx = (
                        mock.patch("os.link", side_effect=OSError(c1_bridge.errno.ENOTSUP, "No hardlinks"))
                        if path_mode == "in_process_no_hardlink_eexist"
                        else contextlib_nullcontext()
                    )
                    with ctx:
                        with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                            c1_bridge.execute_export_workflow(
                                mode="handoff",
                                variant_id="clone-a",
                                recipe="AI_Handoff_Full_sRGB100",
                                output_dir=out_dir,
                                batch_state_path=ledger_path,
                                export_runner_fn=export_runner,
                                pre_promote_hook=place_identical_early_file,
                            )
                else:
                    with self.assertRaises(SystemExit):
                        c1_bridge.execute_export_workflow(
                            mode="handoff",
                            variant_id="clone-a",
                            recipe="AI_Handoff_Full_sRGB100",
                            output_dir=out_dir,
                            batch_state_path=ledger_path,
                            export_runner_fn=export_runner,
                            pre_promote_hook=lambda: (_ for _ in ()).throw(SystemExit(111)),
                        )
                    # External writer places IDENTICAL-BYTE file while crashed, then we restart!
                    a_jpg.write_bytes(identical_bytes)
                    st = c1_bridge.os.lstat(a_jpg)
                    ext_inode_box["id"] = (int(st.st_dev), int(st.st_ino))

                    with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                        c1_bridge.load_batch_ledger(ledger_path)

                # 1. Early-arriving identical-byte file STILL exists with exact original inode & bytes!
                self.assertTrue(a_jpg.is_file(), f"Identical-byte early file was wrongly deleted in {path_mode}!")
                self.assertEqual(a_jpg.read_bytes(), identical_bytes)
                st_after = c1_bridge.os.lstat(a_jpg)
                self.assertEqual((int(st_after.st_dev), int(st_after.st_ino)), ext_inode_box["id"])

                # 2. NO ownership or receipt was granted to the foreign file!
                entry_after = json.loads(ledger_path.read_text(encoding="utf-8"))["photos"]["A"]
                self.assertFalse(entry_after["export_verified"])
                self.assertIsNone(entry_after["export_receipt"])
                self.assertEqual(entry_after["owned_export_files"], {})

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r9_counterexample_2_overwrite_and_rollback_lossless_swap_and_exclusive_dir_versioned_contract(self) -> None:
        """GPT R9 P1 #2: Cover (a) same-volume overwrite race, (b) cross-volume overwrite race, (c) rollback restore race -> 3rd-party file swapped in right before replacement is atomically restored untouched; (d) shared writable dir blocks `--overwrite-own` while `--versioned` publishes non-overwriting `<stem>_v<seq>.jpg`."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        for race_path in ("same_volume_overwrite", "cross_volume_overwrite", "rollback_restore", "shared_dir_and_versioned"):
            with self.subTest(path=race_path), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                raw_dir = root / "raws"
                out_dir = root / "out"
                raw_dir.mkdir()
                out_dir.mkdir(mode=0o700)
                (raw_dir / "A.CR2").write_bytes(b"raw")
                ledger_path = root / "batch.json"
                handoff_path = root / "handoff.jsonl"

                c1_bridge.register_batch_entries(
                    ledger_path,
                    c1_variants=[
                        {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                    ],
                    raw_dir=raw_dir,
                )
                ledger = c1_bridge.load_batch_ledger(ledger_path)
                ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
                ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
                c1_bridge.save_batch_ledger(ledger_path, ledger)

                rgb_box = {"rgb": (25, 55, 85)}

                def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                    if race_path == "cross_volume_overwrite":
                        ext_stg = root / "simulated_other_vol_staging"
                        ext_stg.mkdir(exist_ok=True)
                        staged = ext_stg / f"{exact_stem}.jpg"
                    else:
                        staged = staging_dir / f"{exact_stem}.jpg"
                    make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=rgb_box["rgb"])
                    return ({
                        "job_id": "j-r9-2",
                        "variant_id": var_id,
                        "image_name": "A.CR2",
                        "raw_path": str(raw_dir / "A.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": 100,
                        "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": "Fixed",
                        "primary_scaling_value": 100,
                        "ignore_crop": True,
                        "export_crop_method": "ignore",
                    }, staged)

                # Initial v0 export
                c1_bridge.execute_export_workflow(
                    mode="handoff",
                    variant_id="clone-a",
                    recipe="AI_Handoff_Full_sRGB100",
                    output_dir=out_dir,
                    batch_state_path=ledger_path,
                    export_runner_fn=export_runner,
                )
                a_jpg = out_dir / "A.jpg"
                v0_bytes = a_jpg.read_bytes()
                rgb_box["rgb"] = (180, 90, 45)
                third_bytes = b"\xff\xd8\xff\xe0CONCURRENT_UNLOCKED_WRITER_THIRD_FILE_556677\xff\xd9"

                if race_path in ("same_volume_overwrite", "cross_volume_overwrite"):
                    def inject_third_right_before_swap() -> None:
                        # Unlocked external writer atomically replaces `A.jpg` AFTER ownership check, BEFORE swap!
                        tmp_ext = out_dir / ".ext_incoming.jpg"
                        tmp_ext.write_bytes(third_bytes)
                        c1_bridge.os.replace(tmp_ext, a_jpg)

                    with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                        c1_bridge.execute_export_workflow(
                            mode="handoff",
                            variant_id="clone-a",
                            recipe="AI_Handoff_Full_sRGB100",
                            output_dir=out_dir,
                            batch_state_path=ledger_path,
                            overwrite_own=True,
                            export_runner_fn=export_runner,
                            pre_swap_race_hook=inject_third_right_before_swap,
                        )
                    self.assertEqual(a_jpg.read_bytes(), third_bytes, f"3rd file lost in {race_path}!")
                    after_raw = json.loads(ledger_path.read_text(encoding="utf-8"))["photos"]["A"]
                    bak_p = Path(after_raw["pending_export_commit"]["backup_path"])
                    self.assertEqual(bak_p.read_bytes(), v0_bytes)

                elif race_path == "rollback_restore":
                    # Simulate receipt save failing AFTER publish, AND an unlocked writer swapping in `third_bytes`
                    # right inside the backup-restore window!
                    def inject_third_during_rollback() -> None:
                        tmp_ext = out_dir / ".ext_rb_incoming.jpg"
                        tmp_ext.write_bytes(third_bytes)
                        c1_bridge.os.replace(tmp_ext, a_jpg)

                    with mock.patch.object(
                        c1_bridge,
                        "unregister_from_preview_manifest",
                        side_effect=OSError("Trigger rollback after publish"),
                    ):
                        with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                            c1_bridge.execute_export_workflow(
                                mode="handoff",
                                variant_id="clone-a",
                                recipe="AI_Handoff_Full_sRGB100",
                                output_dir=out_dir,
                                batch_state_path=ledger_path,
                                overwrite_own=True,
                                export_runner_fn=export_runner,
                                rollback_race_hook=inject_third_during_rollback,
                            )
                    self.assertEqual(a_jpg.read_bytes(), third_bytes, "3rd file lost during rollback restore!")
                    after_raw = json.loads(ledger_path.read_text(encoding="utf-8"))["photos"]["A"]
                    bak_p = Path(after_raw["pending_export_commit"]["backup_path"])
                    self.assertEqual(bak_p.read_bytes(), v0_bytes)

                else:
                    # Bump adjustment_seq -> 1
                    step = {"n": 0}

                    def bump_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                        step["n"] += 1
                        val = 0.0 if step["n"] == 1 else 0.30
                        return json.dumps({
                            "variant_id": "clone-a",
                            "orientation": 0,
                            "dimensions": [64, 48],
                            "crop": [0, 0, 64, 48],
                            "global_adjustments": full_zero_adjustments(exposure=val),
                            "layers": [bg_layer],
                        })

                    c1_bridge.execute_apply_transaction(
                        ledger_path=ledger_path,
                        variant_id="clone-a",
                        spec={"global": {"exposure": 0.30}},
                        runner_fn=bump_runner,
                    )

                    # Make `out_dir` world-writable (`0o777`) -> `--overwrite-own` MUST be blocked!
                    out_dir.chmod(0o777)
                    try:
                        with self.assertRaisesRegex(c1_bridge.BridgeError, "SAFETY BLOCK.*world-writable"):
                            c1_bridge.execute_export_workflow(
                                mode="handoff",
                                variant_id="clone-a",
                                recipe="AI_Handoff_Full_sRGB100",
                                output_dir=out_dir,
                                batch_state_path=ledger_path,
                                overwrite_own=True,
                                export_runner_fn=export_runner,
                            )
                        self.assertEqual(a_jpg.read_bytes(), v0_bytes)

                        # Using `--versioned` publishes `A_v1.jpg` via non-overwriting atomic link without touching `A.jpg`!
                        ver_res = c1_bridge.execute_export_workflow(
                            mode="handoff",
                            variant_id="clone-a",
                            recipe="AI_Handoff_Full_sRGB100",
                            output_dir=out_dir,
                            batch_state_path=ledger_path,
                            versioned=True,
                            export_runner_fn=export_runner,
                        )
                        self.assertTrue(ver_res["file"]["path"].endswith("A_v1.jpg"))
                        self.assertEqual(a_jpg.read_bytes(), v0_bytes)
                        ho_ver = c1_bridge.upsert_handoff_record(
                            handoff_path,
                            {"photo_id": "A", "output_path": ver_res["file"]["path"], "status": "passed"},
                            ledger_path=ledger_path,
                        )
                        self.assertEqual(ho_ver["summary"]["passed"], 1)
                    finally:
                        out_dir.chmod(0o700)

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r9_counterexample_3_mid_read_replacement_blocks_receipt_on_initial_and_overwrite_exports(self) -> None:
        """GPT R9 P1 #3: Replace formal file WHILE `inspect_destination_state_unlocked` has `fd` open and is reading bytes -> post-read inode continuity check catches replacement on BOTH initial and overwrite exports, blocking `export_verified=True`."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        for export_case in ("initial_export", "overwrite_own_export"):
            with self.subTest(case=export_case), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                raw_dir = root / "raws"
                out_dir = root / "out"
                raw_dir.mkdir()
                out_dir.mkdir(mode=0o700)
                (raw_dir / "A.CR2").write_bytes(b"raw")
                ledger_path = root / "batch.json"

                c1_bridge.register_batch_entries(
                    ledger_path,
                    c1_variants=[
                        {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                    ],
                    raw_dir=raw_dir,
                )
                ledger = c1_bridge.load_batch_ledger(ledger_path)
                ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
                ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
                c1_bridge.save_batch_ledger(ledger_path, ledger)

                rgb_box = {"rgb": (40, 80, 120)}

                def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                    staged = staging_dir / f"{exact_stem}.jpg"
                    make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=rgb_box["rgb"])
                    return ({
                        "job_id": "j-r9-3",
                        "variant_id": var_id,
                        "image_name": "A.CR2",
                        "raw_path": str(raw_dir / "A.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": 100,
                        "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": "Fixed",
                        "primary_scaling_value": 100,
                        "ignore_crop": True,
                        "export_crop_method": "ignore",
                    }, staged)

                a_jpg = out_dir / "A.jpg"
                if export_case == "overwrite_own_export":
                    c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        export_runner_fn=export_runner,
                    )
                    step = {"n": 0}

                    def bump_runner(script_body: str, args: list[str] | None = None, timeout: int = 120) -> str:
                        step["n"] += 1
                        val = 0.0 if step["n"] == 1 else 0.20
                        return json.dumps({
                            "variant_id": "clone-a",
                            "orientation": 0,
                            "dimensions": [64, 48],
                            "crop": [0, 0, 64, 48],
                            "global_adjustments": full_zero_adjustments(exposure=val),
                            "layers": [bg_layer],
                        })

                    c1_bridge.execute_apply_transaction(
                        ledger_path=ledger_path,
                        variant_id="clone-a",
                        spec={"global": {"exposure": 0.20}},
                        runner_fn=bump_runner,
                    )

                rgb_box["rgb"] = (200, 150, 100)
                mid_swap_bytes = b"\xff\xd8\xff\xe0SWAPPED_WHILE_FD_WAS_OPEN_READING_BYTES\xff\xd9"

                def swap_while_fd_open() -> None:
                    # Replace `a_jpg` via atomic rename while the old `fd` is still open & reading!
                    tmp_other = out_dir / ".mid_read_swap.jpg"
                    tmp_other.write_bytes(mid_swap_bytes)
                    c1_bridge.os.replace(tmp_other, a_jpg)

                with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT.*replaced_during_read"):
                    c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        overwrite_own=(export_case == "overwrite_own_export"),
                        export_runner_fn=export_runner,
                        mid_verify_read_hook=swap_while_fd_open,
                    )

                self.assertEqual(a_jpg.read_bytes(), mid_swap_bytes)
                after_entry = json.loads(ledger_path.read_text(encoding="utf-8"))["photos"]["A"]
                self.assertFalse(after_entry["export_verified"])
                self.assertIsNone(after_entry["export_receipt"])

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r9_counterexample_4_latched_conflict_persists_across_multiple_reloads_until_explicit_resolution(self) -> None:
        """GPT R9 P2 #4: When `destination_removed_before_receipt` conflict is latched, TWO consecutive `load_batch_ledger` calls MUST keep destination vacant, backup intact, and conflict latched until explicit `resolve_export_conflict`."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        for export_case in ("initial_export", "overwrite_own_export"):
            with self.subTest(case=export_case), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                raw_dir = root / "raws"
                out_dir = root / "out"
                raw_dir.mkdir()
                out_dir.mkdir(mode=0o700)
                (raw_dir / "A.CR2").write_bytes(b"raw")
                ledger_path = root / "batch.json"

                c1_bridge.register_batch_entries(
                    ledger_path,
                    c1_variants=[
                        {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                    ],
                    raw_dir=raw_dir,
                )
                ledger = c1_bridge.load_batch_ledger(ledger_path)
                ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
                ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
                c1_bridge.save_batch_ledger(ledger_path, ledger)

                rgb_box = {"rgb": (50, 90, 130)}

                def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                    staged = staging_dir / f"{exact_stem}.jpg"
                    make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=rgb_box["rgb"])
                    return ({
                        "job_id": "j-r9-4",
                        "variant_id": var_id,
                        "image_name": "A.CR2",
                        "raw_path": str(raw_dir / "A.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": 100,
                        "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": "Fixed",
                        "primary_scaling_value": 100,
                        "ignore_crop": True,
                        "export_crop_method": "ignore",
                    }, staged)

                a_jpg = out_dir / "A.jpg"
                v1_bytes: bytes | None = None
                if export_case == "overwrite_own_export":
                    c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        export_runner_fn=export_runner,
                    )
                    v1_bytes = a_jpg.read_bytes()

                rgb_box["rgb"] = (190, 120, 60)
                with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT.*destination_removed_before_receipt|destination changed"):
                    c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        overwrite_own=(export_case == "overwrite_own_export"),
                        export_runner_fn=export_runner,
                        post_promote_hook=lambda: a_jpg.unlink(),
                    )

                # Reload #1 AND Reload #2 MUST BOTH keep conflict latched, destination vacant, and backup intact!
                for reload_idx in (1, 2):
                    with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                        c1_bridge.load_batch_ledger(ledger_path)

                    self.assertFalse(a_jpg.exists(), f"Destination unexpectedly restored on reload #{reload_idx}!")
                    raw_state = json.loads(ledger_path.read_text(encoding="utf-8"))["photos"]["A"]
                    self.assertIn("pending_export_commit", raw_state)
                    self.assertEqual(
                        raw_state["pending_export_commit"]["conflict"]["reason"],
                        "destination_removed_before_receipt",
                    )
                    if export_case == "overwrite_own_export":
                        assert v1_bytes is not None
                        bak_p = Path(raw_state["pending_export_commit"]["backup_path"])
                        self.assertTrue(bak_p.is_file(), f"Backup deleted on reload #{reload_idx}!")
                        self.assertEqual(bak_p.read_bytes(), v1_bytes)

                # Explicit conflict resolution clears the latch deterministically!
                res_mode = "keep-external" if export_case == "initial_export" else "restore-backup"
                res_out = c1_bridge.resolve_export_conflict(ledger_path, photo_id="A", resolution=res_mode)
                self.assertTrue(res_out["resolved"])

                clean_ledger = c1_bridge.load_batch_ledger(ledger_path)
                self.assertNotIn("pending_export_commit", clean_ledger["photos"]["A"])
                if export_case == "overwrite_own_export":
                    self.assertEqual(a_jpg.read_bytes(), v1_bytes)
                else:
                    self.assertFalse(a_jpg.exists())

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r9_counterexample_5_preview_post_publish_swap_or_removal_fails_closed_preserving_scene(self) -> None:
        """GPT R9 P2 #5: When preview file is replaced OR removed after publish, `export --mode preview` MUST fail closed (`BridgeError`), preserve live scene, and NEVER return staged hash with mismatched file."""
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")

        for tamper_mode in ("replaced_after_publish", "removed_after_publish"):
            with self.subTest(tamper=tamper_mode), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                out_dir = root / "previews"
                out_dir.mkdir(mode=0o700)

                def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                    staged = staging_dir / f"{exact_stem}.jpg"
                    make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=(110, 130, 150))
                    return ({
                        "job_id": "j-r9-5",
                        "variant_id": var_id,
                        "image_name": "IMG_0009.CR2",
                        "raw_path": str(root / "IMG_0009.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": 85,
                        "recipe_profile": "sRGB Color Space Profile",
                    }, staged)

                prev_jpg = out_dir / "IMG_0009_before.jpg"
                foreign_preview_bytes = b"\xff\xd8\xff\xe0FOREIGN_PREVIEW_BYTES_DO_NOT_TRUST\xff\xd9"

                def tamper_preview() -> None:
                    if tamper_mode == "replaced_after_publish":
                        prev_jpg.write_bytes(foreign_preview_bytes)
                    else:
                        prev_jpg.unlink()

                with self.assertRaisesRegex(c1_bridge.BridgeError, "Preview export post-publish verification failed"):
                    c1_bridge.execute_export_workflow(
                        mode="preview",
                        variant_id="clone-9",
                        recipe="AI_Preview_1536_NoMeta",
                        output_dir=out_dir,
                        suffix="before",
                        with_metrics=True,
                        export_runner_fn=export_runner,
                        post_promote_hook=tamper_preview,
                    )

                if tamper_mode == "replaced_after_publish":
                    self.assertEqual(prev_jpg.read_bytes(), foreign_preview_bytes)
                else:
                    self.assertFalse(prev_jpg.exists())
                self.assertNotIn("IMG_0009_before.jpg", c1_bridge.load_preview_manifest(out_dir))

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r10_counterexample_1_post_swap_interrupt_preserves_displaced_external_file_across_reloads(self) -> None:
        """
        Review Round 10 P1 #1:
        If a third-party external file replaces the official path right before RENAME_SWAP, and an
        interruption occurs right after RENAME_SWAP succeeds (before displaced-victim verification),
        the displaced external file resides at temp_publish_path. Reloading the ledger must inspect
        BOTH target_path and temp_publish_path, refuse to delete the displaced external file, latch
        EXPORT RECOVERY CONFLICT, deny export_verified=True / export_receipt, and keep the external
        file intact across consecutive reloads.
        """
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        for external_mode in ("different_content", "same_byte_different_inode"):
            with self.subTest(external_mode=external_mode), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                raw_dir = root / "raws"
                out_dir = root / "out"
                raw_dir.mkdir()
                out_dir.mkdir(mode=0o700)
                (raw_dir / "A.CR2").write_bytes(b"raw")
                ledger_path = root / "batch.json"

                c1_bridge.register_batch_entries(
                    ledger_path,
                    c1_variants=[
                        {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                    ],
                    raw_dir=raw_dir,
                )
                ledger = c1_bridge.load_batch_ledger(ledger_path)
                ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
                ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
                c1_bridge.save_batch_ledger(ledger_path, ledger)

                rgb_box = {"rgb": (40, 80, 120)}

                def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                    staged = staging_dir / f"{exact_stem}.jpg"
                    make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=rgb_box["rgb"])
                    return ({
                        "job_id": "j-r10-1",
                        "variant_id": var_id,
                        "image_name": "A.CR2",
                        "raw_path": str(raw_dir / "A.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": 100,
                        "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": "Fixed",
                        "primary_scaling_value": 100,
                        "ignore_crop": True,
                        "export_crop_method": "ignore",
                    }, staged)

                c1_bridge.execute_export_workflow(
                    mode="handoff",
                    variant_id="clone-a",
                    recipe="AI_Handoff_Full_sRGB100",
                    output_dir=out_dir,
                    batch_state_path=ledger_path,
                    export_runner_fn=export_runner,
                )
                a_jpg = out_dir / "A.jpg"
                v1_bytes = a_jpg.read_bytes()
                v1_st = c1_bridge.os.lstat(a_jpg)
                v1_inode = (int(v1_st.st_dev), int(v1_st.st_ino))

                if external_mode == "different_content":
                    jpg_ext = b"\xff\xd8\xff\xe0R10_P1_1_EXTERNAL_THIRD_PARTY_FILE_KEEP\xff\xd9"
                else:
                    jpg_ext = bytes(v1_bytes)

                ext_inode_holder: list[tuple[int, int]] = []

                def replace_with_external_before_swap() -> None:
                    ext_tmp = out_dir / ".external_incoming.tmp"
                    ext_tmp.write_bytes(jpg_ext)
                    c1_bridge.os.replace(ext_tmp, a_jpg)
                    st = c1_bridge.os.lstat(a_jpg)
                    ext_inode_holder.append((int(st.st_dev), int(st.st_ino)))

                def crash_immediately_after_swap() -> None:
                    raise SystemExit("simulated crash right after RENAME_SWAP before victim verification")

                rgb_box["rgb"] = (200, 140, 80)
                with self.assertRaises(SystemExit):
                    c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        overwrite_own=True,
                        export_runner_fn=export_runner,
                        pre_swap_race_hook=replace_with_external_before_swap,
                        post_swap_before_verify_hook=crash_immediately_after_swap,
                    )

                self.assertEqual(len(ext_inode_holder), 1)
                ext_inode = ext_inode_holder[0]
                self.assertNotEqual(ext_inode, v1_inode)

                raw_before_reload = json.loads(ledger_path.read_text(encoding="utf-8"))
                pending_before = raw_before_reload["photos"]["A"]["pending_export_commit"]
                temp_pub_path = Path(pending_before["temp_publish_path"])
                self.assertTrue(temp_pub_path.exists())
                self.assertEqual(temp_pub_path.read_bytes(), jpg_ext)

                # Consecutive reloads #1 and #2 must BOTH preserve the external file, keep conflict, and deny new receipt
                for reload_round in (1, 2):
                    with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                        c1_bridge.load_batch_ledger(ledger_path)

                    self.assertTrue(a_jpg.exists(), f"reload #{reload_round} lost target_path")
                    self.assertEqual(a_jpg.read_bytes(), jpg_ext)
                    st_cur = c1_bridge.os.lstat(a_jpg)
                    self.assertEqual((int(st_cur.st_dev), int(st_cur.st_ino)), ext_inode)
                    self.assertEqual(st_cur.st_nlink, 1, "restored external file must not share inode with temp file")
                    self.assertFalse(temp_pub_path.exists(), f"reload #{reload_round} left uncleaned candidate temp")

                    raw_after = json.loads(ledger_path.read_text(encoding="utf-8"))
                    rec = raw_after["photos"]["A"]
                    self.assertFalse(rec["export_verified"])
                    self.assertIsNone(rec["export_receipt"])
                    self.assertIn("pending_export_commit", rec)
                    self.assertIsInstance(rec["pending_export_commit"].get("conflict"), dict)

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r10_counterexample_2_in_place_equal_length_overwrite_during_receipt_save_revokes_receipt(self) -> None:
        """
        Review Round 10 P1 #2:
        If an external writer modifies target_path in place with equal-length content while
        save_batch_ledger is saving the receipt (keeping st_dev, st_ino, and st_size unchanged),
        content continuity verification after receipt persistence must detect the SHA-256 mismatch,
        revoke export_verified / export_receipt on disk, latch EXPORT RECOVERY CONFLICT, and raise.
        Tested for both initial publish and overwrite-own re-export.
        """
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        for export_phase in ("initial_publish", "overwrite_own"):
            with self.subTest(export_phase=export_phase), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                raw_dir = root / "raws"
                out_dir = root / "out"
                raw_dir.mkdir()
                out_dir.mkdir(mode=0o700)
                (raw_dir / "A.CR2").write_bytes(b"raw")
                ledger_path = root / "batch.json"

                c1_bridge.register_batch_entries(
                    ledger_path,
                    c1_variants=[
                        {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                    ],
                    raw_dir=raw_dir,
                )
                ledger = c1_bridge.load_batch_ledger(ledger_path)
                ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
                ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
                c1_bridge.save_batch_ledger(ledger_path, ledger)

                rgb_box = {"rgb": (60, 100, 140)}

                def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                    staged = staging_dir / f"{exact_stem}.jpg"
                    make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=rgb_box["rgb"])
                    return ({
                        "job_id": "j-r10-2",
                        "variant_id": var_id,
                        "image_name": "A.CR2",
                        "raw_path": str(raw_dir / "A.CR2"),
                        "raw_dimensions": [64, 48],
                        "orientation": 0,
                        "recipe_format": "JPEG",
                        "recipe_quality": 100,
                        "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": "Fixed",
                        "primary_scaling_value": 100,
                        "ignore_crop": True,
                        "export_crop_method": "ignore",
                    }, staged)

                a_jpg = out_dir / "A.jpg"
                if export_phase == "overwrite_own":
                    c1_bridge.execute_export_workflow(
                        mode="handoff",
                        variant_id="clone-a",
                        recipe="AI_Handoff_Full_sRGB100",
                        output_dir=out_dir,
                        batch_state_path=ledger_path,
                        export_runner_fn=export_runner,
                    )
                    rgb_box["rgb"] = (180, 120, 70)

                orig_save = c1_bridge.save_batch_ledger
                tamper_state: dict[str, Any] = {"done": False, "tampered_bytes": b""}

                def wrapped_save(lp: Path, data: dict[str, Any]) -> None:
                    orig_save(lp, data)
                    rec = data.get("photos", {}).get("A", {})
                    if (
                        not tamper_state["done"]
                        and rec.get("export_verified") is True
                        and "pending_export_commit" not in rec
                    ):
                        tamper_state["done"] = True
                        cur_bytes = a_jpg.read_bytes()
                        st_before = a_jpg.stat()
                        # Build an equal-length byte payload with altered content
                        mid = len(cur_bytes) // 2
                        flipped_byte = bytes([cur_bytes[mid] ^ 0xFF])
                        eq_len_bytes = cur_bytes[:mid] + flipped_byte + cur_bytes[mid + 1 :]
                        self.assertEqual(len(eq_len_bytes), len(cur_bytes))
                        self.assertNotEqual(eq_len_bytes, cur_bytes)
                        a_jpg.write_bytes(eq_len_bytes)
                        st_after = a_jpg.stat()
                        self.assertEqual(
                            (st_before.st_dev, st_before.st_ino, st_before.st_size),
                            (st_after.st_dev, st_after.st_ino, st_after.st_size),
                        )
                        tamper_state["tampered_bytes"] = eq_len_bytes

                with unittest.mock.patch.object(c1_bridge, "save_batch_ledger", side_effect=wrapped_save):
                    with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                        c1_bridge.execute_export_workflow(
                            mode="handoff",
                            variant_id="clone-a",
                            recipe="AI_Handoff_Full_sRGB100",
                            output_dir=out_dir,
                            batch_state_path=ledger_path,
                            overwrite_own=(export_phase == "overwrite_own"),
                            export_runner_fn=export_runner,
                        )

                self.assertTrue(tamper_state["done"])
                self.assertEqual(a_jpg.read_bytes(), tamper_state["tampered_bytes"])

                with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                    c1_bridge.load_batch_ledger(ledger_path)

                raw_after = json.loads(ledger_path.read_text(encoding="utf-8"))
                rec = raw_after["photos"]["A"]
                self.assertFalse(rec["export_verified"])
                self.assertIsNone(rec["export_receipt"])
                self.assertIn("pending_export_commit", rec)
                self.assertIsInstance(rec["pending_export_commit"].get("conflict"), dict)

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r10_counterexample_3_restore_backup_rejects_same_byte_different_inode_external_file_and_blocks_overwrite(self) -> None:
        """
        Review Round 10 P1 #3:
        If an overwrite conflict is triggered by a file that has identical bytes to the previous JPG
        but a different inode, running resolve-export-conflict --resolution restore-backup must verify
        both SHA-256 and file identity (st_dev, st_ino). It must refuse to adopt the foreign inode into
        owned_export_files, keep the conflict latched, and prevent a subsequent --overwrite-own from
        overwriting the external file.
        """
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            out_dir = root / "out"
            raw_dir.mkdir()
            out_dir.mkdir(mode=0o700)
            (raw_dir / "A.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
            ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            rgb_box = {"rgb": (70, 110, 150)}

            def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                staged = staging_dir / f"{exact_stem}.jpg"
                make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=rgb_box["rgb"])
                return ({
                    "job_id": "j-r10-3",
                    "variant_id": var_id,
                    "image_name": "A.CR2",
                    "raw_path": str(raw_dir / "A.CR2"),
                    "raw_dimensions": [64, 48],
                    "orientation": 0,
                    "recipe_format": "JPEG",
                    "recipe_quality": 100,
                    "recipe_profile": "sRGB Color Space Profile",
                    "scaling_method": "Fixed",
                    "primary_scaling_value": 100,
                    "ignore_crop": True,
                    "export_crop_method": "ignore",
                }, staged)

            c1_bridge.execute_export_workflow(
                mode="handoff",
                variant_id="clone-a",
                recipe="AI_Handoff_Full_sRGB100",
                output_dir=out_dir,
                batch_state_path=ledger_path,
                export_runner_fn=export_runner,
            )
            a_jpg = out_dir / "A.jpg"
            v1_bytes = a_jpg.read_bytes()
            v1_st = c1_bridge.os.lstat(a_jpg)
            v1_inode = (int(v1_st.st_dev), int(v1_st.st_ino))

            ext_inode_holder: list[tuple[int, int]] = []

            def swap_in_same_byte_different_inode() -> None:
                ext_tmp = out_dir / ".same_byte_external.tmp"
                ext_tmp.write_bytes(v1_bytes)
                c1_bridge.os.replace(ext_tmp, a_jpg)
                st = c1_bridge.os.lstat(a_jpg)
                ext_inode_holder.append((int(st.st_dev), int(st.st_ino)))

            rgb_box["rgb"] = (210, 150, 90)
            with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT|File identity mismatch"):
                c1_bridge.execute_export_workflow(
                    mode="handoff",
                    variant_id="clone-a",
                    recipe="AI_Handoff_Full_sRGB100",
                    output_dir=out_dir,
                    batch_state_path=ledger_path,
                    overwrite_own=True,
                    export_runner_fn=export_runner,
                    pre_swap_race_hook=swap_in_same_byte_different_inode,
                )

            ext_inode = ext_inode_holder[0]
            self.assertNotEqual(ext_inode, v1_inode)
            self.assertEqual(a_jpg.read_bytes(), v1_bytes)

            # Attempting restore-backup must fail because a_jpg has a foreign inode (even though bytes match v1)
            with self.assertRaisesRegex(c1_bridge.BridgeError, "Refusing 'restore-backup'"):
                c1_bridge.resolve_export_conflict(
                    ledger_path,
                    photo_id="A",
                    resolution="restore-backup",
                )

            # Subsequent --overwrite-own must remain blocked and preserve the external file's inode & bytes
            with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                c1_bridge.execute_export_workflow(
                    mode="handoff",
                    variant_id="clone-a",
                    recipe="AI_Handoff_Full_sRGB100",
                    output_dir=out_dir,
                    batch_state_path=ledger_path,
                    overwrite_own=True,
                    export_runner_fn=export_runner,
                )

            st_after = c1_bridge.os.lstat(a_jpg)
            self.assertEqual((int(st_after.st_dev), int(st_after.st_ino)), ext_inode)
            self.assertEqual(a_jpg.read_bytes(), v1_bytes)

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r10_counterexample_4_legacy_pending_commit_without_candidate_file_ids_latches_conflict_on_reloads(self) -> None:
        """
        Review Round 10 P1 #4:
        A legacy pending_export_commit created before upgrade (missing candidate_file_ids or
        missing previous_file_id on overwrite) lacks reliable file-identity proof. If an external file
        with matching SHA-256 sits at target_path, loading the ledger must NOT default ownership to True;
        it must transition to manual conflict recovery across initial and subsequent reloads without
        granting ownership or export_verified=True.
        """
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1,
            "name": "Background",
            "kind": "background",
            "enabled": True,
            "opacity": 100,
            "adjustments": full_zero_adjustments(exposure=0.0),
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            out_dir = root / "out"
            raw_dir.mkdir()
            out_dir.mkdir(mode=0o700)
            (raw_dir / "A.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[
                    {"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}
                ],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
            ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            a_jpg = out_dir / "A.jpg"
            make_jpeg_with_profile(a_jpg, 64, 48, srgb_icc, rgb=(90, 120, 150))
            jpg_ext = a_jpg.read_bytes()
            ext_sha = c1_bridge.hashlib.sha256(jpg_ext).hexdigest()
            st_ext = c1_bridge.os.lstat(a_jpg)
            ext_inode = (int(st_ext.st_dev), int(st_ext.st_ino))

            # Inject a legacy pending_export_commit without candidate_file_ids
            raw_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            raw_ledger["photos"]["A"]["pending_export_commit"] = {
                "photo_id": "A",
                "operation": "initial_export",
                "target_path": str(a_jpg.resolve()),
                "new_sha256": ext_sha,
                "new_file_size": len(jpg_ext),
                "new_dimensions": [64, 48],
                "new_icc_profile": "sRGB IEC61966-2.1",
                "recipe": "AI_Handoff_Full_sRGB100",
                "adjustment_generation": 0,
                "previous_sha256": None,
                "previous_file_id": None,
                "backup_path": None,
                "temp_publish_path": str(out_dir / ".c1_publish_A.tmp"),
                "pre_replace_voucher": None,
                "started_at": "2026-11-27T00:00:00+00:00",
            }
            ledger_path.write_text(json.dumps(raw_ledger, ensure_ascii=False, indent=2), encoding="utf-8")

            for reload_round in (1, 2):
                with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                    c1_bridge.load_batch_ledger(ledger_path)

                self.assertTrue(a_jpg.exists(), f"reload #{reload_round} lost external file")
                self.assertEqual(a_jpg.read_bytes(), jpg_ext)
                st_now = c1_bridge.os.lstat(a_jpg)
                self.assertEqual((int(st_now.st_dev), int(st_now.st_ino)), ext_inode)

                raw_after = json.loads(ledger_path.read_text(encoding="utf-8"))
                rec = raw_after["photos"]["A"]
                self.assertFalse(rec["export_verified"])
                self.assertIsNone(rec["export_receipt"])
                self.assertEqual(rec.get("owned_export_files", {}), {})
                self.assertIn("pending_export_commit", rec)
                self.assertIsInstance(rec["pending_export_commit"].get("conflict"), dict)

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r11_counterexample_1_in_process_exception_after_swap_preserves_external_file(self) -> None:
        """
        Review Round 11 P1 #1 + P2:
        If a normal Exception (e.g. OSError) is raised right after RENAME_SWAP before victim verification,
        `handle_in_process_abort_unlocked` must NOT blindly unlink `temp_publish_path`. The displaced
        external file must be atomically swapped back onto `target_path` with st_nlink == 1, preserved
        byte-for-byte, and EXPORT RECOVERY CONFLICT must be latched.
        """
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1, "name": "Background", "kind": "background",
            "enabled": True, "opacity": 100, "adjustments": full_zero_adjustments(exposure=0.0),
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            out_dir = root / "out"
            raw_dir.mkdir()
            out_dir.mkdir(mode=0o700)
            (raw_dir / "A.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[{"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
            ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            rgb_box = {"rgb": (30, 70, 110)}

            def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                staged = staging_dir / f"{exact_stem}.jpg"
                make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=rgb_box["rgb"])
                return ({
                    "job_id": "j-r11-1", "variant_id": var_id, "image_name": "A.CR2",
                    "raw_path": str(raw_dir / "A.CR2"), "raw_dimensions": [64, 48], "orientation": 0,
                    "recipe_format": "JPEG", "recipe_quality": 100, "recipe_profile": "sRGB Color Space Profile",
                    "scaling_method": "Fixed", "primary_scaling_value": 100, "ignore_crop": True, "export_crop_method": "ignore",
                }, staged)

            c1_bridge.execute_export_workflow(
                mode="handoff", variant_id="clone-a", recipe="AI_Handoff_Full_sRGB100",
                output_dir=out_dir, batch_state_path=ledger_path, export_runner_fn=export_runner,
            )
            a_jpg = out_dir / "A.jpg"
            ext_bytes = b"\xff\xd8\xff\xe0R11_P1_1_EXTERNAL_FILE_NEVER_DELETE\xff\xd9"
            ext_inode_box: list[tuple[int, int]] = []

            def put_ext() -> None:
                t = out_dir / ".ext.tmp"
                t.write_bytes(ext_bytes)
                c1_bridge.os.replace(t, a_jpg)
                st = c1_bridge.os.lstat(a_jpg)
                ext_inode_box.append((int(st.st_dev), int(st.st_ino)))

            def raise_io_error() -> None:
                raise OSError("Simulated I/O error after RENAME_SWAP before victim verification")

            rgb_box["rgb"] = (200, 140, 80)
            with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                c1_bridge.execute_export_workflow(
                    mode="handoff", variant_id="clone-a", recipe="AI_Handoff_Full_sRGB100",
                    output_dir=out_dir, batch_state_path=ledger_path, overwrite_own=True,
                    export_runner_fn=export_runner, pre_swap_race_hook=put_ext,
                    post_swap_before_verify_hook=raise_io_error,
                )

            self.assertEqual(a_jpg.read_bytes(), ext_bytes)
            st_after = c1_bridge.os.lstat(a_jpg)
            self.assertEqual((int(st_after.st_dev), int(st_after.st_ino)), ext_inode_box[0])
            self.assertEqual(st_after.st_nlink, 1)
            raw_after = json.loads(ledger_path.read_text(encoding="utf-8"))["photos"]["A"]
            self.assertFalse(raw_after["export_verified"])
            self.assertIsNone(raw_after["export_receipt"])
            self.assertIn("pending_export_commit", raw_after)

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r11_counterexample_2_resolve_conflict_preserves_unswapped_temp_external_file(self) -> None:
        """
        Review Round 11 P1 #2:
        If RENAME_SWAP displaced external file C into `temp_publish_path` and crashed, and an external
        writer placed file D at `target_path`, running `resolve-export-conflict --resolution keep-external`
        must NOT unlink `temp_publish_path`; BOTH file D (at `target_path`) and file C (preserved at a
        rescued path) must remain intact byte-for-byte.
        """
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1, "name": "Background", "kind": "background",
            "enabled": True, "opacity": 100, "adjustments": full_zero_adjustments(exposure=0.0),
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "raws"
            out_dir = root / "out"
            raw_dir.mkdir()
            out_dir.mkdir(mode=0o700)
            (raw_dir / "A.CR2").write_bytes(b"raw")
            ledger_path = root / "batch.json"

            c1_bridge.register_batch_entries(
                ledger_path,
                c1_variants=[{"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}],
                raw_dir=raw_dir,
            )
            ledger = c1_bridge.load_batch_ledger(ledger_path)
            ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
            ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
            c1_bridge.save_batch_ledger(ledger_path, ledger)

            rgb_box = {"rgb": (30, 70, 110)}

            def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                staged = staging_dir / f"{exact_stem}.jpg"
                make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=rgb_box["rgb"])
                return ({
                    "job_id": "j-r11-2", "variant_id": var_id, "image_name": "A.CR2",
                    "raw_path": str(raw_dir / "A.CR2"), "raw_dimensions": [64, 48], "orientation": 0,
                    "recipe_format": "JPEG", "recipe_quality": 100, "recipe_profile": "sRGB Color Space Profile",
                    "scaling_method": "Fixed", "primary_scaling_value": 100, "ignore_crop": True, "export_crop_method": "ignore",
                }, staged)

            c1_bridge.execute_export_workflow(
                mode="handoff", variant_id="clone-a", recipe="AI_Handoff_Full_sRGB100",
                output_dir=out_dir, batch_state_path=ledger_path, export_runner_fn=export_runner,
            )
            a_jpg = out_dir / "A.jpg"
            file_c = b"\xff\xd8\xff\xe0R11_P1_2_EXTERNAL_FILE_C_DISPLACED\xff\xd9"
            file_d = b"\xff\xd8\xff\xe0R11_P1_2_EXTERNAL_FILE_D_AT_TARGET\xff\xd9"

            def put_c() -> None:
                t = out_dir / ".c.tmp"
                t.write_bytes(file_c)
                c1_bridge.os.replace(t, a_jpg)

            rgb_box["rgb"] = (200, 140, 80)
            with self.assertRaises(SystemExit):
                c1_bridge.execute_export_workflow(
                    mode="handoff", variant_id="clone-a", recipe="AI_Handoff_Full_sRGB100",
                    output_dir=out_dir, batch_state_path=ledger_path, overwrite_own=True,
                    export_runner_fn=export_runner, pre_swap_race_hook=put_c,
                    post_swap_before_verify_hook=lambda: (_ for _ in ()).throw(SystemExit("crash")),
                )

            t_d = out_dir / ".d.tmp"
            t_d.write_bytes(file_d)
            c1_bridge.os.replace(t_d, a_jpg)

            with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                c1_bridge.load_batch_ledger(ledger_path)

            res = c1_bridge.resolve_export_conflict(ledger_path, photo_id="A", resolution="keep-external")
            self.assertTrue(res["resolved"])
            self.assertIsNotNone(res.get("preserved_temp_external_path"))
            rescued = Path(res["preserved_temp_external_path"])
            self.assertTrue(rescued.is_file())
            self.assertEqual(rescued.read_bytes(), file_c)
            self.assertEqual(a_jpg.read_bytes(), file_d)

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
    def test_r11_counterexample_3_two_stage_receipt_commit_and_rollforward_detect_in_place_overwrite(self) -> None:
        """
        Review Round 11 P1 #3:
        1) Equal-length in-place overwrite during `during_receipt_save_hook` followed by immediate
           process crash must still leave `pending_export_commit` active so restart detects the
           tampering and refuses to issue `export_verified=True`.
        2) Equal-length in-place overwrite during `load_batch_ledger` roll-forward receipt save must
           be detected immediately, revoking `export_verified=True` and latching conflict.
        """
        srgb_icc = Path("/System/Library/ColorSync/Profiles/sRGB Profile.icc")
        bg_layer = {
            "index": 1, "name": "Background", "kind": "background",
            "enabled": True, "opacity": 100, "adjustments": full_zero_adjustments(exposure=0.0),
        }

        for sub_case in ("crash_during_two_stage_save", "rollforward_save_in_place_overwrite"):
            with self.subTest(sub_case=sub_case), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                raw_dir = root / "raws"
                out_dir = root / "out"
                raw_dir.mkdir()
                out_dir.mkdir(mode=0o700)
                (raw_dir / "A.CR2").write_bytes(b"raw")
                ledger_path = root / "batch.json"

                c1_bridge.register_batch_entries(
                    ledger_path,
                    c1_variants=[{"variant_id": "src-a", "position": 1, "raw_path": str(raw_dir / "A.CR2"), "dimensions": [64, 48]}],
                    raw_dir=raw_dir,
                )
                ledger = c1_bridge.load_batch_ledger(ledger_path)
                ledger["photos"]["A"]["cloned_variant_id"] = "clone-a"
                ledger["photos"]["A"]["inherited_layers"] = [bg_layer]
                c1_bridge.save_batch_ledger(ledger_path, ledger)

                def export_runner(var_id: str, recipe: str, staging_dir: Path, exact_stem: str):
                    staged = staging_dir / f"{exact_stem}.jpg"
                    make_jpeg_with_profile(staged, 64, 48, srgb_icc, rgb=(55, 95, 135))
                    return ({
                        "job_id": "j-r11-3", "variant_id": var_id, "image_name": "A.CR2",
                        "raw_path": str(raw_dir / "A.CR2"), "raw_dimensions": [64, 48], "orientation": 0,
                        "recipe_format": "JPEG", "recipe_quality": 100, "recipe_profile": "sRGB Color Space Profile",
                        "scaling_method": "Fixed", "primary_scaling_value": 100, "ignore_crop": True, "export_crop_method": "ignore",
                    }, staged)

                a_jpg = out_dir / "A.jpg"

                if sub_case == "crash_during_two_stage_save":
                    def tamper_and_crash() -> None:
                        cur = a_jpg.read_bytes()
                        mid = len(cur) // 2
                        a_jpg.write_bytes(cur[:mid] + bytes([cur[mid] ^ 0xFF]) + cur[mid + 1 :])
                        raise SystemExit("crash right during two-stage receipt save")

                    with self.assertRaises(SystemExit):
                        c1_bridge.execute_export_workflow(
                            mode="handoff", variant_id="clone-a", recipe="AI_Handoff_Full_sRGB100",
                            output_dir=out_dir, batch_state_path=ledger_path,
                            export_runner_fn=export_runner, during_receipt_save_hook=tamper_and_crash,
                        )

                    with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                        c1_bridge.load_batch_ledger(ledger_path)
                else:
                    with self.assertRaises(SystemExit):
                        c1_bridge.execute_export_workflow(
                            mode="handoff", variant_id="clone-a", recipe="AI_Handoff_Full_sRGB100",
                            output_dir=out_dir, batch_state_path=ledger_path, export_runner_fn=export_runner,
                            post_promote_hook=lambda: (_ for _ in ()).throw(SystemExit("crash before receipt save")),
                        )

                    orig_save = c1_bridge.save_batch_ledger
                    tampered_once = {"done": False}

                    def tamper_on_rf_save(lp: Path, data: dict[str, Any]) -> None:
                        orig_save(lp, data)
                        rec = data.get("photos", {}).get("A", {})
                        if not tampered_once["done"] and rec.get("export_verified") is True:
                            tampered_once["done"] = True
                            cur = a_jpg.read_bytes()
                            mid = len(cur) // 2
                            a_jpg.write_bytes(cur[:mid] + bytes([cur[mid] ^ 0xFF]) + cur[mid + 1 :])

                    with unittest.mock.patch.object(c1_bridge, "save_batch_ledger", side_effect=tamper_on_rf_save):
                        with self.assertRaisesRegex(c1_bridge.BridgeError, "EXPORT RECOVERY CONFLICT"):
                            c1_bridge.load_batch_ledger(ledger_path)

                raw_after = json.loads(ledger_path.read_text(encoding="utf-8"))["photos"]["A"]
                self.assertFalse(raw_after["export_verified"])
                self.assertIsNone(raw_after["export_receipt"])
                self.assertIn("pending_export_commit", raw_after)

    @unittest.skipUnless(
        sys.platform == "darwin" and Path("/Applications/Capture One.app").is_dir() and Path("/usr/bin/osacompile").is_file(),
        "Requires macOS with Capture One installed for AppleScript static compilation check",
    )
    def test_generated_applescripts_compile_against_installed_capture_one_sdef(self) -> None:
        sample_spec = {
            "global": {"temperature": 5100, "tint": 1.2, "exposure": 0.3, "highlight_adjustment": -12, "shadow_recovery": 10},
            "masks": {
                "face_skin": {"exposure": 0.2, "opacity": 95},
                "clothes": {"highlight_adjustment": -15},
                "subject": {"highlight_adjustment": -10},
                "background": {"exposure": -0.4, "highlight_adjustment": -25},
            },
        }
        scripts_to_check = {
            "status": c1_bridge.build_status_script(),
            "inspect": c1_bridge.build_inspect_script(),
            "prepare_variant": c1_bridge.build_prepare_variant_script(),
            "apply_fresh": c1_bridge.build_apply_script(sample_spec),
            "apply_managed": c1_bridge.build_apply_script(
                sample_spec,
                layer_plan={
                    "face_skin": ("AI_Face_Skin__m_tok", True),
                    "clothes": ("AI_Clothes", True),
                    "subject": ("AI_Subject", True),
                    "background": ("AI_Background", True),
                },
            ),
            "rollback_delete": c1_bridge.build_apply_script(
                {"global": {"exposure": 0.0}, "masks": {"face_skin": {"_delete": True}}},
                layer_plan={"face_skin": ("AI_Face_Skin__m_tok", True)},
            ),
            "ensure_recipes": c1_bridge.build_ensure_recipes_script(),
            "export": c1_bridge.build_export_script(),
            "check_job": c1_bridge.build_check_job_script(),
        }
        with tempfile.TemporaryDirectory() as td:
            for name, body in scripts_to_check.items():
                with self.subTest(script=name):
                    src = Path(td) / f"{name}.applescript"
                    out = Path(td) / f"{name}.scpt"
                    src.write_text(body + "\n" + c1_bridge.APPLESCRIPT_HELPERS, encoding="utf-8")
                    proc = subprocess.run(
                        ["/usr/bin/osacompile", "-o", str(out), str(src)],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        timeout=30,
                    )
                    self.assertEqual(
                        proc.returncode,
                        0,
                        f"osacompile failed for {name}:\n{proc.stderr or proc.stdout}",
                    )


if __name__ == "__main__":
    unittest.main()
