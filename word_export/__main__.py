"""Export saved OCR results and ordered PNG/JPEG pages to an editable Word file."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from word_export.export import export_word
from word_export.pages import load_pages


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m word_export", description=__doc__)
    parser.add_argument("images", nargs="+", type=Path, help="PNG/JPEG pages in document order")
    parser.add_argument("--results", required=True, type=Path, help="Saved parse response or batch JSON")
    parser.add_argument("--output", required=True, type=Path, help="Destination .docx file")
    parser.add_argument("--font", default="Arial", help="Word font and installed font used for measurements (default: Arial)")
    parser.add_argument("--no-recover-graphics", action="store_true", help="Report additional graphic candidates without embedding them")
    parser.add_argument("--no-deskew", action="store_true", help="Disable automatic small-angle scan correction")
    args = parser.parse_args(argv)
    try:
        if args.output.suffix.lower() != ".docx":
            raise ValueError("Output filename must end in .docx")
        if args.output.resolve() == args.results.resolve():
            raise ValueError("Output must not overwrite the OCR results")
        payload = json.loads(args.results.read_text(encoding="utf-8"))
        pages = load_pages(payload, args.images)
        warnings = export_word(pages, args.output, font=args.font, recover_graphics=not args.no_recover_graphics,
                               deskew=not args.no_deskew)
    except (TypeError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    print(f"Saved {args.output} ({len(pages)} pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
