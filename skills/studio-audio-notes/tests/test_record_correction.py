"""Unit tests for studio-audio-notes scripts/record_correction.py."""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "record_correction.py"


class RecordCorrectionTests(unittest.TestCase):
    def test_append_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio = root / "sample.m4a"
            audio.write_bytes(b"fake-audio")
            log = root / "logs" / "corrections.jsonl"

            res = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--log",
                    str(log),
                    "--audio",
                    str(audio),
                    "--original",
                    "美师",
                    "--corrected",
                    "门市",
                    "--evidence",
                    "用户确认门店岗位名称",
                    "--scope",
                    "store-term",
                    "--start",
                    "12.5",
                    "--end",
                    "14.0",
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertTrue(res.stdout.strip())
            entry = json.loads(log.read_text(encoding="utf-8").strip())
            self.assertEqual(entry["original"], "美师")
            self.assertEqual(entry["corrected"], "门市")
            self.assertEqual(entry["scope"], "store-term")

            # Invalid time range (end <= start) must fail without corrupting existing log
            bad = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--log",
                    str(log),
                    "--audio",
                    str(audio),
                    "--original",
                    "a",
                    "--corrected",
                    "b",
                    "--evidence",
                    "ok",
                    "--start",
                    "10",
                    "--end",
                    "5",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(bad.returncode, 0)
            self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
