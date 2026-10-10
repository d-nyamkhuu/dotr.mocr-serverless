"""Skew correction, coordinate consistency and resampled pale table rules."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from word_export.export import Page, Region, prepared_page
from scripts.prepare_scans import prepare_scans, main
from word_export.layout import FontMetrics, table_geometry, text_lines
from word_export.scan import SkewEstimate, content_box, estimate_skew, rotate_scan, transform_box


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def text_page(self):
        image = Image.new('RGB', (900, 700), 'white')
        font = ImageFont.truetype(FontMetrics().paths[False, False], 22)
        draw = ImageDraw.Draw(image)
        for index in range(12):
            draw.text((70, 70 + index * 40), 'Scanned Latin text. Кириллица: строка документа.', font=font, fill='black')
        return image

    def test_text_only_angles_and_upright_control(self):
        source = self.text_page()
        for angle in [0, -0.5, 0.5, -1, 1, -3, 3]:
            with self.subTest(angle=angle):
                rotated = source.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor='white')
                estimate = estimate_skew(rotated)
                self.assertTrue(estimate.confident, estimate)
                self.assertAlmostEqual(estimate.angle, -angle, delta=0.15)

    def test_sparse_and_conflicting_evidence_is_not_rotated(self):
        blank = Image.new('RGB', (700, 900), 'white')
        self.assertFalse(estimate_skew(blank).confident)
        ImageDraw.Draw(blank).line((100, 100, 650, 125), width=3, fill='black')
        self.assertFalse(estimate_skew(blank).confident)
        large = self.text_page().rotate(7, resample=Image.Resampling.BICUBIC, fillcolor='white')
        self.assertFalse(estimate_skew(large).confident)
        strip = self.text_page().crop((0, 50, 900, 350))
        mixed = Image.new('RGB', (900, 700), 'white')
        mixed.paste(strip.rotate(3, fillcolor='white'), (0, 0))
        mixed.paste(strip.rotate(-3, fillcolor='white'), (0, 400))
        self.assertFalse(estimate_skew(mixed).confident)

    def test_rotation_preserves_edge_content_and_maps_boxes(self):
        image = Image.new('RGB', (500, 700), 'white')
        box = (0, 0, 40, 60)
        image.paste('red', box)
        for angle in [-3, 3]:
            rotated, matrix = rotate_scan(image, angle)
            self.assertGreater(rotated.width, image.width)
            self.assertGreater(rotated.height, image.height)
            pixels = np.asarray(rotated)
            ys, xs = np.nonzero((pixels[:, :, 0] > 200) & (pixels[:, :, 1] < 80))
            x1, y1, x2, y2 = transform_box(box, matrix, rotated.size)
            self.assertTrue(x1 <= xs.min() <= xs.max() < x2)
            self.assertTrue(y1 <= ys.min() <= ys.max() < y2)
            self.assertGreater(len(xs), 2300)

    def test_prepared_page_keeps_text_and_does_not_mutate_sources(self):
        original = self.text_page().rotate(3, resample=Image.Resampling.BICUBIC, fillcolor='white')
        path = self.root / 'scan.png'; original.save(path)
        region = Region((40, 30, 850, 600), 'Text', 'Editable Кириллица', 'Page 1, region 1')
        page = Page(path, *original.size, [region])
        before = path.read_bytes(); warnings = []
        with prepared_page(page, warnings) as (corrected, image):
            self.assertNotEqual(corrected.width, page.width)
            self.assertEqual(corrected.regions[0].text, region.text)
            self.assertLess(abs(estimate_skew(image).angle), 0.15)
            self.assertNotEqual(corrected.regions[0].bbox, region.bbox)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(page.regions[0], region)
        self.assertTrue(any('deskewed' in warning for warning in warnings))
        with prepared_page(page, [], deskew=False) as (unchanged, image):
            self.assertIs(unchanged, page)
            self.assertEqual(image.size, original.size)

    def test_picture_does_not_drive_page_correction(self):
        image = self.text_page().rotate(3, resample=Image.Resampling.BICUBIC, fillcolor='white')
        path = self.root / 'picture.png'; image.save(path)
        page = Page(path, *image.size, [Region((0, 0, *image.size), 'Picture', '', 'Picture')])
        with prepared_page(page, []) as (corrected, _):
            self.assertIs(corrected, page)

    def test_rotated_paragraph_boxes_exclude_neighboring_baselines(self):
        source = Image.new('RGB', (900, 600), 'white')
        font = ImageFont.truetype(FontMetrics().paths[False, False], 24)
        draw = ImageDraw.Draw(source)
        text = 'Long enough to overlap adjacent rotated boxes.'
        boxes = []
        for top in [100, 170, 240]:
            draw.text((70, top), text, font=font, fill='black')
            draw.text((70, top + 32), text[:18], font=font, fill='black')
            a = draw.textbbox((70, top), text, font=font)
            b = draw.textbbox((70, top + 32), text[:18], font=font)
            boxes.append((a[0], a[1], max(a[2], b[2]), b[3]))
        for angle in [-3, 3]:
            scan, forward = rotate_scan(source, angle)
            corrected, inverse = rotate_scan(scan, -angle)
            corrected_boxes = []
            for box in boxes:
                inflated = transform_box(box, forward, scan.size)
                recovered = content_box(corrected, inflated, inverse)
                self.assertEqual(len(text_lines(corrected.crop(recovered))), 2)
                corrected_boxes.append(recovered)
            self.assertTrue(all(a[3] < b[1] for a, b in zip(corrected_boxes, corrected_boxes[1:])))

    def test_uncertain_skew_warns_without_applying_transform(self):
        path = self.root / 'scan.png'; self.text_page().save(path)
        page = Page(path, 900, 700, [])
        warnings = []
        with patch('word_export.export.estimate_skew', return_value=SkewEstimate(5, False, 'search limit')):
            with prepared_page(page, warnings) as (unchanged, _):
                self.assertIs(unchanged, page)
        self.assertIn('uncertain scan skew', warnings[0])

    def test_preprocessing_order_exif_and_atomic_failure(self):
        first = self.root / 'first.jpg'
        image = Image.new('RGB', (300, 500), 'white')
        exif = image.getexif(); exif[274] = 6
        image.save(first, exif=exif)
        second = self.root / 'second.png'; self.text_page().save(second)
        missing = self.root / 'missing.png'; output = self.root / 'prepared'
        with self.assertRaises(ValueError):
            prepare_scans([first, missing], output)
        self.assertFalse(output.exists())
        records = prepare_scans([first, second], output)
        self.assertEqual([r['image'] for r in records], ['0001-first.png', '0002-second.png'])
        with Image.open(output / records[0]['image']) as oriented:
            self.assertEqual(oriented.size, (500, 300))
            self.assertNotIn(274, oriented.getexif())
        self.assertEqual(json.loads((output / 'manifest.json').read_text()), records)
        with self.assertRaises(ValueError):
            prepare_scans([second], output)
        self.assertEqual(main(['--output-dir', str(self.root / 'bad'), '--angle', 'nan', str(first)]), 1)

    def test_pale_merged_header_rule_survives_resampling(self):
        # The short header separator is lighter than the background. Blur
        # spreads its contrast across rows, as a deskew interpolation does.
        image = Image.new('RGB', (804, 414), 'white'); draw = ImageDraw.Draw(image)
        ys = [2, 82, 106, 140, 174, 208, 242, 276, 310, 344, 378, 412]
        draw.rectangle((2, 2, 802, 106), fill=(160, 160, 160))
        for y in ys[2:]:
            draw.line((2, y, 802, y), fill=(190, 190, 190), width=1)
        draw.line((502, 82, 702, 82), fill=(190, 190, 190), width=1)
        for x in range(2, 803, 100):
            draw.line((x, 2, x, 412), fill=(120, 120, 120), width=1)
        blurred = Image.fromarray(cv2.GaussianBlur(np.asarray(image), (3, 3), 0.8))
        measured = table_geometry(blurred, 8, 11)
        self.assertIsNotNone(measured.rows)
        self.assertAlmostEqual(measured.rows[1], 82, delta=3)
        self.assertEqual(len(measured.rows), 12)
        self.assertIsNone(table_geometry(blurred, 8, 17).rows)


if __name__ == '__main__':
    unittest.main()
