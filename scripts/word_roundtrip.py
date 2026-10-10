"""Replay saved multi-document OCR fixtures and compare baseline/improved renderings.

A sample directory contains selection.json (name/image entries), ocr/NAME.json,
originals/NAME.pdf, and optionally reconstructed/NAME.docx plus NAME.pdf from the
previous exporter. No inference requests or downloads are made.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import pypdfium2 as pdfium

from scripts.check_word import compare_document, render_word
from word_export.export import export_word
from word_export.pages import load_pages


def pdf_words(path: Path) -> Counter:
    words: Counter = Counter()
    with pdfium.PdfDocument(path) as document:
        for index in range(len(document)):
            with closing(document[index]) as page, closing(page.get_textpage()) as text:
                value = unicodedata.normalize("NFKC", text.get_text_bounded()).casefold().replace('\u00ad', '')
                words.update(re.findall(r'\w+', value))
    return words


def score(report: dict, original: Path, rendered: Path) -> dict:
    source, actual = pdf_words(original), pdf_words(rendered)
    return {"mean_ink_f1": sum(page.get("ink_f1", 0) for page in report["pages"]) / len(report["pages"]),
            "source_word_recall": sum((source & actual).values()) / max(1, source.total()),
            "pages": report["rendered_pages"], "findings": report["warning_count"]}


def write_report(summary: dict, output: Path, renderer: str) -> None:
    parts = ['<!doctype html><meta charset="utf-8"><title>Word exporter regression comparison</title><style>body{font:16px system-ui;max-width:1100px;margin:40px auto}td,th{padding:12px;border:1px solid #ddd}table{border-collapse:collapse}p{line-height:1.6}</style><h1>Word exporter regression comparison</h1>',
             f'<p>Renderer: {html.escape(renderer)}. Both versions scored with the same PDFium rasterizer and 3-pixel ink tolerance at 150 DPI. Original page images are fitted to A4.</p>',
             '<p>Ink F1 is a layout diagnostic, not a percentage of fidelity. Source-word recall ignores word order, added words, and equation meaning. Findings are heuristic and require visual review. Microsoft Word desktop validation remains pending.</p>',
             '<table><tr><th>Sample</th><th>Pages</th><th>Ink F1 before → after</th><th>Word recall before → after</th><th>Reports</th></tr>']
    for name, result in summary['documents'].items():
        current = result['improved']
        before = result.get('baseline', {})
        ink_before = f'{before["mean_ink_f1"]:.1%}' if before else '—'
        words_before = f'{before["source_word_recall"]:.1%}' if before else '—'
        parts.append(f'<tr><td>{name}</td><td>{current["pages"]}</td><td>{ink_before} → {current["mean_ink_f1"]:.1%}</td><td>{words_before} → {current["source_word_recall"]:.1%}</td><td><a href="checks/improved/{name}/report.html">Improved</a> · <a href="checks/baseline/{name}/report.html">Baseline</a> · <a href="reconstructed/{name}.docx">Word file</a></td></tr>')
    parts.append('</table><h2>Regression checks</h2>')
    parts.append('<p>' + html.escape('; '.join(summary['regressions']) or 'No page-count, text-retention, or mean ink-overlap regression against the saved baseline.') + '</p>')
    output.write_text(''.join(parts), encoding='utf-8')


def replay(samples: Path, output: Path, *, phase: str = "all", pdf_directory: Path | None = None,
           renderer: str = "externally supplied PDF") -> dict:
    if samples.resolve() == output.resolve() or samples.resolve() in output.resolve().parents:
        raise ValueError("Use a separate output directory to preserve the saved baseline")
    selection = json.loads((samples / 'selection.json').read_text(encoding='utf-8'))
    names = list(dict.fromkeys(page['name'] for page in selection))
    if not names or any(not re.fullmatch(r'[A-Za-z0-9_-]+', name) for name in names):
        raise ValueError("Sample names must contain only letters, digits, underscore or hyphen")
    (output / 'reconstructed').mkdir(parents=True, exist_ok=True)
    summary = {"samples": str(samples), "documents": {}, "regressions": []}
    for name in names:
        images = [samples / page['image'] for page in selection if page['name'] == name]
        document = output / 'reconstructed' / f'{name}.docx'
        if phase in {'export', 'all'}:
            results = json.loads((samples / 'ocr' / f'{name}.json').read_text(encoding='utf-8'))
            warnings = export_word(load_pages(results, images), document)
            (output / 'reconstructed' / f'{name}.warnings.json').write_text(json.dumps(warnings, indent=2, ensure_ascii=False), encoding='utf-8')
        if phase == 'export':
            continue
        pdf = (pdf_directory or (output / 'reconstructed')) / f'{name}.pdf'
        if pdf_directory is None and phase == 'all':
            renderer = render_word(document, pdf)
        report = compare_document(document, pdf, images, output / 'checks' / 'improved' / name, renderer=renderer)
        current = score(report, samples / 'originals' / f'{name}.pdf', pdf)
        summary['documents'][name] = {'improved': current}
        if current['pages'] != len(images):
            summary['regressions'].append(f'{name}: page count mismatch')
        baseline_doc = samples / 'reconstructed' / f'{name}.docx'
        baseline_pdf = baseline_doc.with_suffix('.pdf')
        if baseline_doc.is_file() and baseline_pdf.is_file():
            previous = compare_document(baseline_doc, baseline_pdf, images, output / 'checks' / 'baseline' / name,
                                        renderer='saved baseline PDF; see sample provenance')
            baseline = score(previous, samples / 'originals' / f'{name}.pdf', baseline_pdf)
            summary['documents'][name]['baseline'] = baseline
            if current['mean_ink_f1'] < baseline['mean_ink_f1'] - 0.03:
                summary['regressions'].append(f'{name}: mean ink F1 decreased by more than 3 percentage points')
            if current['source_word_recall'] < baseline['source_word_recall'] - 0.01:
                summary['regressions'].append(f'{name}: source-word recall decreased by more than 1 percentage point')
        print(f'{name}: {current["pages"]} pages, ink F1 {current["mean_ink_f1"]:.1%}, words {current["source_word_recall"]:.1%}, {current["findings"]} findings', flush=True)
    if phase != 'export':
        (output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        write_report(summary, output / 'report.html', renderer)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--phase', choices=['export', 'check', 'all'], default='all')
    parser.add_argument('--pdf-directory', type=Path, help='Use already rendered PDFs instead of running local LibreOffice')
    parser.add_argument('--renderer', default='externally supplied PDF')
    args = parser.parse_args(argv)
    try:
        result = replay(args.samples, args.output, phase=args.phase, pdf_directory=args.pdf_directory,
                        renderer=args.renderer)
    except (OSError, ValueError, KeyError, pdfium.PdfiumError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    return 2 if result['regressions'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
