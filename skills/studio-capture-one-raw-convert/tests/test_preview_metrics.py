"""Unit tests for studio-capture-one-raw-convert scripts/preview-metrics.py."""
from __future__ import annotations
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "preview-metrics.py"


def write_solid_bmp(path: Path, r: int, g: int, b: int, width: int = 8, height: int = 8) -> None:
    row_bytes = width * 3
    stride = (row_bytes + 3) & ~3
    pixel_data = (bytes([b, g, r]) * width + b"\x00" * (stride - row_bytes)) * height
    file_size = 54 + len(pixel_data)
    header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixel_data), 2835, 2835, 0, 0)
    path.write_bytes(header + dib + pixel_data)


@unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sips").is_file(), "Requires macOS sips")
class PreviewMetricsTests(unittest.TestCase):
    def test_black_mid_and_white_percentiles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            black = root / "black.bmp"
            mid = root / "mid.bmp"
            white = root / "white.bmp"
            write_solid_bmp(black, 0, 0, 0)
            write_solid_bmp(mid, 128, 128, 128)
            write_solid_bmp(white, 255, 255, 255)

            res = subprocess.run(
                [sys.executable, str(SCRIPT), str(black), str(mid), str(white)],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            rows = [json.loads(line) for line in res.stdout.strip().splitlines()]
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["p50"], 0)
            self.assertAlmostEqual(rows[0]["nearBlackFraction"], 1.0)
            self.assertTrue(120 <= rows[1]["p50"] <= 135)
            self.assertEqual(rows[2]["p50"], 255)
            self.assertAlmostEqual(rows[2]["nearWhiteFraction"], 1.0)


if __name__ == "__main__":
    unittest.main()
