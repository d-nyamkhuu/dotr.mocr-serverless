"""Compare a rendered Word PDF against source images and editable DOCX regions.

LibreOffice is optional: --pdf accepts a PDF exported by Microsoft Word or any
other renderer. The report detects likely defects; it cannot certify fidelity.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile

import cv2
import numpy as np
import pypdfium2 as pdfium
from lxml import etree
from PIL import Image

from word_export.export import EMU_PER_POINT
from word_export.pages import load_image

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
}


def render_word(document: Path, output: Path, executable: str = "libreoffice") -> str:
    command = shutil.which(executable)
    if command is None:
        raise ValueError("LibreOffice is unavailable; install it or supply --pdf exported from Word")
    with tempfile.TemporaryDirectory(prefix="word-render-") as temporary:
        directory = Path(temporary)
        result = subprocess.run(
            [command, f"-env:UserInstallation={(directory / 'profile').as_uri()}",
             "--headless", "--convert-to", "pdf:writer_pdf_Export", "--outdir", str(directory), str(document.resolve())],
            capture_output=True, text=True, timeout=180, check=False,
        )
        rendered = directory / document.with_suffix(".pdf").name
        if result.returncode or not rendered.is_file():
            raise ValueError(f"Word rendering failed: {result.stdout}\n{result.stderr}")
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(rendered, output)
    return subprocess.run([command, "--version"], capture_output=True, text=True, check=True, timeout=10).stdout.strip()


def characters(text: str) -> Counter:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return Counter(char for char in normalized if char.isalnum())


def text_recall(expected: str, actual: str) -> float:
    reference = characters(expected)
    return sum((reference & characters(actual)).values()) / max(1, reference.total()) if reference else 1.0


def ink_scores(expected: Image.Image, actual: Image.Image, tolerance: int = 3) -> dict[str, float]:
    source = np.asarray(expected.convert("L")) < 220
    result = np.asarray(actual.convert("L")) < 220
    if not source.any() and not result.any():
        return {"ink_recall": 1.0, "ink_precision": 1.0, "ink_f1": 1.0}
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (tolerance * 2 + 1, tolerance * 2 + 1))
    near_source = cv2.dilate(source.astype(np.uint8), kernel).astype(bool)
    near_result = cv2.dilate(result.astype(np.uint8), kernel).astype(bool)
    recall = float((source & near_result).sum() / max(1, source.sum()))
    precision = float((result & near_source).sum() / max(1, result.sum()))
    return {"ink_recall": recall, "ink_precision": precision,
            "ink_f1": 2 * precision * recall / max(1e-12, precision + recall)}


def document_regions(document: Path) -> list[dict]:
    with ZipFile(document) as archive:
        root = etree.fromstring(archive.read("word/document.xml"), etree.XMLParser(resolve_entities=False, no_network=True))
    regions = []
    for anchor in root.xpath("//wp:anchor", namespaces=NS):
        label = anchor.find("wp:docPr", NS).get("name", "")
        match = re.match(r"Page (\d+),", label)
        if not match:
            continue
        left = int(anchor.find("wp:positionH/wp:posOffset", NS).text) / EMU_PER_POINT
        top = int(anchor.find("wp:positionV/wp:posOffset", NS).text) / EMU_PER_POINT
        extent = anchor.find("wp:extent", NS)
        width, height = (int(extent.get(key)) / EMU_PER_POINT for key in ("cx", "cy"))
        regions.append({"page": int(match[1]), "label": label, "box": [left, top, left + width, top + height],
                        "text": "".join(anchor.xpath(".//w:t/text() | .//m:t/text()", namespaces=NS)),
                        "equation": bool(anchor.xpath(".//m:oMath", namespaces=NS)),
                        "radicals": len(anchor.xpath(".//m:rad", namespaces=NS)),
                        "picture": bool(anchor.xpath(".//a:blip", namespaces=NS))})
    return regions


def compare_document(document: Path, pdf: Path, images: list[Path], output: Path,
                     *, renderer: str = "externally supplied PDF", min_ink_f1: float = 0.4) -> dict:
    if not images:
        raise ValueError("At least one source image is required")
    if not 0 <= min_ink_f1 <= 1:
        raise ValueError("min_ink_f1 must be between 0 and 1")
    output.mkdir(parents=True, exist_ok=True)
    protected = {path.resolve() for path in [document, pdf, *images]}
    if any((output / name).resolve() in protected for name in ("report.html", "metrics.json")):
        raise ValueError("Report output must not overwrite an input")
    regions = document_regions(document)
    report = {"document": str(document), "document_sha256": hashlib.sha256(document.read_bytes()).hexdigest(),
              "pdf": str(pdf), "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
              "renderer": renderer, "expected_pages": len(images), "pages": [], "warnings": [],
              "limits": "Character recall ignores order and extra content. Ink overlap is a diagnostic, not a fidelity score. Clipping/equation checks are heuristic. Microsoft Word desktop validation is required."}
    with pdfium.PdfDocument(pdf) as rendered:
        report["rendered_pages"] = len(rendered)
        if len(rendered) != len(images):
            report["warnings"].append(f"Page count mismatch: expected {len(images)}, rendered {len(rendered)}")
        for index, path in enumerate(images):
            entry = {"page": index + 1, "source": str(path), "warnings": [], "regions": []}
            report["pages"].append(entry)
            if index >= len(rendered):
                entry["warnings"].append("Missing rendered page")
                continue
            with closing(rendered[index]) as page:
                page_width, page_height = page.get_size()
                with closing(page.render(scale=150 / 72)) as bitmap:
                    actual = bitmap.to_pil().convert("RGB")
                with load_image(path) as source:
                    landscape = source.width > source.height
                    expected_points = (841.89, 595.28) if landscape else (595.28, 841.89)
                    if any(abs(value - target) > 2 for value, target in zip((page_width, page_height), expected_points)):
                        entry["warnings"].append("Rendered page size/orientation does not match expected A4 layout")
                    expected = Image.new("RGB", actual.size, "white")
                    scale = min(actual.width / source.width, actual.height / source.height)
                    fitted = source.resize((round(source.width * scale), round(source.height * scale)), Image.Resampling.LANCZOS)
                    expected.paste(fitted, ((actual.width - fitted.width) // 2, (actual.height - fitted.height) // 2))
                entry.update(ink_scores(expected, actual))
                if entry["ink_f1"] < min_ink_f1:
                    entry["warnings"].append("Low page ink overlap; inspect layout or page order")
                source_ink = np.asarray(expected.convert("L")) < 220
                if not source_ink.any():
                    entry["warnings"].append("Blank source rendering: ink overlap cannot establish successful content reconstruction")
                actual_ink = np.asarray(actual.convert("L")) < 220
                overlay = np.full((*source_ink.shape, 3), 255, dtype=np.uint8)
                overlay[source_ink] = [0, 165, 190]
                overlay[actual_ink] = [225, 50, 150]
                overlay[source_ink & actual_ink] = [40, 40, 40]
                for name, image in [("source", expected), ("rendered", actual), ("overlay", Image.fromarray(overlay))]:
                    filename = f"page-{index + 1:02d}-{name}.png"
                    if (output / filename).resolve() in protected:
                        raise ValueError("Report image would overwrite an input")
                    image.save(output / filename)
                    entry[name + "_image"] = filename
                with closing(page.get_textpage()) as textpage:
                    for region_index, region in enumerate(item for item in regions if item["page"] == index + 1):
                        left, top, right, bottom = region["box"]
                        observed = textpage.get_text_bounded(left - 1, page_height - bottom - 1, right + 1, page_height - top + 1)
                        row = {**region, "warnings": [], "text_recall": text_recall(region["text"], observed)}
                        if region["radicals"] > 1:
                            row["warnings"].append("Multiple radicals: verify root scope visually; matching glyphs and ink cannot establish mathematical equivalence")
                        if region["picture"] and (right - left) * (bottom - top) > page_width * page_height * 0.25:
                            row["warnings"].append("Large raster region: any text/form fields within it are not editable, regardless of pixel fidelity")
                        if row["text_recall"] < 0.98:
                            row["warnings"].append("Missing or clipped rendered text (or PDF font encoding mismatch)")
                        if region["equation"] and any(char in observed for char in ("¿", "\ufffd", "□")):
                            row["warnings"].append("Possible equation rendering failure: replacement glyph")
                        px = (max(0, round(left * 150 / 72)), max(0, round(top * 150 / 72)),
                              min(actual.width, round(right * 150 / 72)), min(actual.height, round(bottom * 150 / 72)))
                        if px[2] <= px[0] or px[3] <= px[1]:
                            row["warnings"].append("Region lies outside rendered page")
                        else:
                            a, b = expected.crop(px), actual.crop(px)
                            row.update(ink_scores(a, b))
                            if row["ink_f1"] < min_ink_f1 and np.any(np.asarray(a.convert("L")) < 220):
                                row["warnings"].append("Low region ink overlap; check positioning, clipping, and formatting")
                            comparison = Image.new("RGB", (a.width * 2, a.height), "white")
                            comparison.paste(a, (0, 0))
                            comparison.paste(b, (a.width, 0))
                            filename = f"page-{index + 1:02d}-region-{region_index + 1:03d}.png"
                            if (output / filename).resolve() in protected:
                                raise ValueError("Report crop would overwrite an input")
                            comparison.save(output / filename)
                            row["comparison"] = filename
                        entry["regions"].append(row)
                    # Characters crossing all known text-box bounds suggest overflow.
                    boxes = [item["box"] for item in entry["regions"] if item["text"]]
                    outside = 0
                    for char_index in range(textpage.count_chars()):
                        char = textpage.get_text_range(char_index, 1)
                        if not char.strip():
                            continue
                        x1, y1, x2, y2 = textpage.get_charbox(char_index)
                        y1, y2 = page_height - y2, page_height - y1
                        if not any(x1 >= a - 2 and y1 >= b - 2 and x2 <= c + 2 and y2 <= d + 2 for a, b, c, d in boxes):
                            outside += 1
                    if outside:
                        entry["warnings"].append(f"{outside} rendered characters extend outside expected text regions")
    report["warning_count"] = len(report["warnings"]) + sum(len(page["warnings"]) + sum(len(region["warnings"]) for region in page["regions"]) for page in report["pages"])
    (output / "metrics.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    write_report(report, output / "report.html")
    return report


def write_report(report: dict, output: Path) -> None:
    esc = html.escape
    parts = ['<!doctype html><meta charset="utf-8"><title>Word rendering checks</title><style>body{font:16px system-ui;max-width:1500px;margin:32px auto;padding:20px}figure{margin:0}img{max-width:100%}.pages{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}li{margin:8px 0}summary{cursor:pointer}.warning{color:#9c3515}table{border-collapse:collapse}td,th{padding:8px;border:1px solid #ddd}</style>',
             '<h1>Word rendering checks</h1>', f'<p>Renderer: {esc(report["renderer"])}. Pages: {report["rendered_pages"]}/{report["expected_pages"]}. Findings: {report["warning_count"]}.</p>',
             f'<p>{esc(report["limits"])}</p><p>Images: source, rendered output, overlay (cyan source / pink output). Region comparisons: source left, output right.</p>']
    parts.extend(f'<p class="warning">{esc(warning)}</p>' for warning in report["warnings"])
    for page in report["pages"]:
        parts.append(f'<h2>Page {page["page"]}</h2>')
        parts.extend(f'<p class="warning">{esc(warning)}</p>' for warning in page["warnings"])
        if "ink_f1" not in page:
            continue
        parts.append(f'<p>Tolerant ink F1: {page["ink_f1"]:.1%}</p><div class="pages">')
        for name in ("source", "rendered", "overlay"):
            filename = page[name + "_image"]
            parts.append(f'<figure><figcaption>{name}</figcaption><a href="{filename}"><img loading="lazy" src="{filename}" alt="{name}"></a></figure>')
        parts.append('</div>')
        for region in page["regions"]:
            warning = '; '.join(region['warnings']) or 'No automatic finding (visual review still required)'
            parts.append(f'<details><summary>{esc(region["label"])} — character recall {region["text_recall"]:.1%}: {esc(warning)}</summary>')
            if "comparison" in region:
                parts.append(f'<img loading="lazy" src="{region["comparison"]}" alt="Source and reconstructed region">')
            parts.append('</details>')
    output.write_text(''.join(parts), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument("--document", required=True, type=Path)
    parser.add_argument("--pdf", type=Path, help="PDF rendered from this DOCX; otherwise run local LibreOffice")
    parser.add_argument("--renderer", default="externally supplied PDF", help="Label identifying the renderer of --pdf")
    parser.add_argument("--output", required=True, type=Path, help="Directory for JSON, HTML and comparisons")
    parser.add_argument("--min-ink-f1", type=float, default=0.4)
    parser.add_argument("--fail-on-warnings", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.pdf:
            report = compare_document(args.document, args.pdf, args.images, args.output,
                                      renderer=args.renderer, min_ink_f1=args.min_ink_f1)
        else:
            with tempfile.TemporaryDirectory(prefix="word-check-") as temporary:
                pdf = Path(temporary) / "rendered.pdf"
                renderer = render_word(args.document, pdf)
                report = compare_document(args.document, pdf, args.images, args.output,
                                          renderer=renderer, min_ink_f1=args.min_ink_f1)
    except (OSError, ValueError, subprocess.SubprocessError, pdfium.PdfiumError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Saved {args.output / 'report.html'} ({report['warning_count']} findings)")
    return 2 if args.fail_on_warnings and report["warning_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
