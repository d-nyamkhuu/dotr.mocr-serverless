"""Deskew ordered PNG/JPEG scans locally before sending them to OCR."""

from __future__ import annotations

import argparse
import json
import math
import os
from collections.abc import Sequence
from pathlib import Path
import shutil
import sys
import tempfile

from word_export.pages import load_image
from word_export.scan import estimate_skew, rotate_scan


def prepare_scans(paths: Sequence[Path], output: Path, *, angle: float | None = None) -> list[dict]:
    """Publish a new directory of oriented PNGs and an ordered transform manifest."""
    if not paths:
        raise ValueError("At least one image is required")
    if output.exists():
        raise ValueError("Output directory already exists; choose a new directory")
    if angle is not None and (not math.isfinite(angle) or abs(angle) > 5):
        raise ValueError("Correction angle must be between -5 and +5 degrees")
    # Read every input before writing any output; invalid batches leave no directory.
    for path in paths:
        with load_image(path):
            pass
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    records = []
    try:
        for index, path in enumerate(paths, start=1):
            with load_image(path) as image:
                estimate = estimate_skew(image)
                correction = angle if angle is not None else estimate.angle if estimate.confident else 0
                name = f"{index:04d}-{path.stem}.png"
                matrix = [[1, 0, 0], [0, 1, 0]]
                size = image.size
                if correction:
                    corrected, transform = rotate_scan(image, correction)
                    with corrected:
                        corrected.save(temporary / name)
                        size = corrected.size
                    matrix = transform.tolist()
                else:
                    image.save(temporary / name)
                records.append({"source": str(path.resolve()), "image": name,
                                "original_size": list(image.size), "size": list(size),
                                "correction_degrees": correction, "matrix": matrix,
                                "confident": angle is not None or estimate.confident,
                                "reason": "manual correction" if angle is not None else estimate.reason})
        (temporary / "manifest.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return records


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m word_export.prepare", description=__doc__)
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path, help="New directory for corrected PNGs and manifest.json")
    parser.add_argument("--angle", type=float, help="Override automatic estimation; counterclockwise correction in degrees (±5)")
    args = parser.parse_args(argv)
    try:
        records = prepare_scans(args.images, args.output, angle=args.angle)
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    for record in records:
        if not record["confident"]:
            print(f'Warning: {record["image"]}: {record["reason"]}; no rotation applied', file=sys.stderr)
        print(args.output / record["image"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
