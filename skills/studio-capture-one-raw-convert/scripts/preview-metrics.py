"""Measure luminance in Capture One sRGB JPG previews on macOS."""

import argparse
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory


def measure(path, scratch):
    bmp = scratch / "preview.bmp"
    result = subprocess.run(
        ["/usr/bin/sips", "-Z", "1024", "-s", "format", "bmp",
         str(path.resolve(strict=True)), "--out", str(bmp)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "sips conversion failed")

    data = bmp.read_bytes()
    if data[:2] != b"BM" or len(data) < 54:
        raise ValueError("invalid BMP preview")
    offset = struct.unpack_from("<I", data, 10)[0]
    header_size, width, height, planes, depth, compression = struct.unpack_from("<IiiHHI", data, 14)
    height = abs(height)
    if header_size < 40 or offset < 14 + header_size or planes != 1 or depth not in (24, 32):
        raise ValueError("unsupported BMP layout")
    if compression != 0 or not (0 < width <= 1024 and 0 < height <= 1024):
        raise ValueError("unsupported BMP dimensions or compression")

    pixel_size = depth // 8
    stride = (width * pixel_size + 3) & ~3
    if len(data) < offset + stride * height:
        raise ValueError("truncated BMP preview")

    histogram = [0] * 256
    for row in range(height):
        start = offset + row * stride
        for index in range(start, start + width * pixel_size, pixel_size):
            blue, green, red = data[index:index + 3]
            luminance = (2126 * red + 7152 * green + 722 * blue + 5000) // 10000
            histogram[luminance] += 1

    pixel_count = width * height

    def percentile(fraction):
        target = math.ceil(pixel_count * fraction)
        total = 0
        for level, count in enumerate(histogram):
            total += count
            if total >= target:
                return level

    return {
        "path": str(path),
        "p05": percentile(0.05),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "nearBlackFraction": sum(histogram[:6]) / pixel_count,
        "nearWhiteFraction": sum(histogram[250:]) / pixel_count,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    failed = False
    for path in args.paths:
        try:
            with TemporaryDirectory(prefix="captureone-preview-") as directory:
                print(json.dumps(measure(path, Path(directory)), ensure_ascii=False))
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            print(f"{path}: {error}", file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
