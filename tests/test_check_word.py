"""Rendering diagnostics against actual small PDF files, without an office install."""

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from scripts.check_word import compare_document, ink_scores, render_word
from word_export.export import export_word, load_pages


def write_pdf(path: Path, texts: list[str]) -> None:
    # A minimal valid PDF with an embedded content stream and built-in Helvetica.
    objects = [b'', b'', b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    kids = []
    for text in texts:
        page_id = len(objects) + 1
        kids.append(f'{page_id} 0 R')
        content = f'BT /F1 11 Tf 30 740 Td ({text}) Tj ET'.encode('ascii')
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.28 841.89] /Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>'.encode())
        objects.append(f'<< /Length {len(content)} >>\nstream\n'.encode() + content + b'\nendstream')
    objects[0] = b'<< /Type /Catalog /Pages 2 0 R >>'
    objects[1] = f'<< /Type /Pages /Kids [{" ".join(kids)}] /Count {len(kids)} >>'.encode()
    data = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f'{index} 0 obj\n'.encode() + obj + b'\nendobj\n')
    start = len(data)
    data.extend(f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        data.extend(f'{offset:010d} 00000 n \n'.encode())
    data.extend(f'trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode())
    path.write_bytes(data)


class WordCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.image = self.root / 'source.png'
        Image.new('RGB', (600, 800), 'white').save(self.image)
        self.document = self.root / 'document.docx'
        self.pdf = self.root / 'rendered.pdf'
        payload = {'status': 'ok', 'image': {'original_width': 600, 'original_height': 800},
                   'elements': [{'category': 'Text', 'text': 'Hello preserved words', 'bbox': [30, 40, 550, 150]}]}
        export_word(load_pages(payload, [self.image]), self.document)

    def test_pdf_content_retention_and_missing_text(self):
        write_pdf(self.pdf, ['Hello preserved words'])
        result = compare_document(self.document, self.pdf, [self.image], self.root / 'good')
        self.assertEqual(result['pages'][0]['regions'][0]['text_recall'], 1)
        write_pdf(self.pdf, ['Hello'])
        result = compare_document(self.document, self.pdf, [self.image], self.root / 'bad')
        self.assertLess(result['pages'][0]['regions'][0]['text_recall'], 0.5)
        self.assertTrue(any('clipped' in value for value in result['pages'][0]['regions'][0]['warnings']))
        self.assertTrue((self.root / 'bad/report.html').is_file())
        self.assertTrue((self.root / 'bad/page-01-region-001.png').is_file())

    def test_page_count_mismatch_and_missing_page(self):
        write_pdf(self.pdf, ['Hello'])
        result = compare_document(self.document, self.pdf, [self.image, self.image], self.root / 'report')
        self.assertIn('Page count mismatch', result['warnings'][0])
        self.assertEqual(result['pages'][1]['warnings'], ['Missing rendered page'])
        write_pdf(self.pdf, ['Hello', 'Extra page'])
        result = compare_document(self.document, self.pdf, [self.image], self.root / 'extra')
        self.assertIn('Page count mismatch', result['warnings'][0])

    def test_rendering_requires_available_engine(self):
        with self.assertRaisesRegex(ValueError, 'supply --pdf'):
            render_word(self.document, self.pdf, executable='definitely-no-office-binary')

    def test_review_warnings_do_not_depend_on_pixel_score(self):
        payload = {'status': 'ok', 'image': {'original_width': 600, 'original_height': 800},
                   'elements': [
                       {'category': 'Picture', 'text': None, 'bbox': [0, 0, 600, 600]},
                       {'category': 'Formula', 'text': r'\sqrt{4\sqrt[3]{x+1}}', 'bbox': [30, 620, 500, 750]},
                   ]}
        export_word(load_pages(payload, [self.image]), self.document)
        write_pdf(self.pdf, ['4 3 x 1'])
        result = compare_document(self.document, self.pdf, [self.image], self.root / 'review')
        page = result['pages'][0]
        self.assertTrue(any('Blank source' in warning for warning in page['warnings']))
        self.assertTrue(any('not editable' in warning for warning in page['regions'][0]['warnings']))
        self.assertTrue(any('root scope' in warning for warning in page['regions'][1]['warnings']))

    def test_ink_overlap_excludes_white_background(self):
        source = Image.new('RGB', (100, 100), 'white')
        actual = source.copy()
        source.paste('black', (0, 0, 10, 10))
        actual.paste('black', (90, 90, 100, 100))
        self.assertEqual(ink_scores(source, actual)['ink_f1'], 0)
        self.assertEqual(ink_scores(source, source)['ink_f1'], 1)


if __name__ == '__main__':
    unittest.main()
