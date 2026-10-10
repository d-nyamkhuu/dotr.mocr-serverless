"""Conservative source-image measurements, using Pillow and OpenCV."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageFont


class FontMetrics:
    def __init__(self, family: str = "Arial") -> None:
        self.family = family
        self.paths: dict[tuple[bool, bool], str] = {}
        self.resolved_family = family
        self.fonts: dict[tuple[float, bool, bool], ImageFont.FreeTypeFont] = {}
        for bold, italic in [(False, False), (True, False), (False, True), (True, True)]:
            style = ("Bold" if bold else "Regular") + (" Italic" if italic else "")
            path = None
            if shutil.which("fc-match"):
                result = subprocess.run(
                    ["fc-match", "-f", "%{file}\n%{family}", f"{family}:style={style}"],
                    capture_output=True, text=True, check=True, timeout=10,
                ).stdout.splitlines()
                if result and Path(result[0]).is_file():
                    path = result[0]
                    if not bold and not italic:
                        self.resolved_family = result[1] if len(result) > 1 else family
            if path is None:
                suffix = "bi" if bold and italic else "bd" if bold else "i" if italic else ""
                candidates = [
                    Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / f"arial{suffix}.ttf",
                    Path("/Library/Fonts") / f"Arial{' Bold' if bold else ''}{' Italic' if italic else ''}.ttf",
                ] if family.casefold() == "arial" else []
                path = next((str(item) for item in candidates if item.is_file()), None)
            if path is None:
                try:
                    path = ImageFont.truetype(family, 12).path
                except OSError:
                    try:
                        path = ImageFont.truetype("DejaVuSans.ttf", 12).path
                        self.resolved_family = "DejaVu Sans"
                    except OSError as exc:
                        raise ValueError("No measurable TrueType font found; install Arial or Liberation Sans") from exc
            self.paths[bold, italic] = str(path)

    def font(self, size: float, bold: bool = False, italic: bool = False) -> ImageFont.FreeTypeFont:
        # Measure at 4x resolution to reduce hinting errors at small point sizes.
        key = size, bold, italic
        if key not in self.fonts:
            self.fonts[key] = ImageFont.truetype(self.paths[bold, italic], size=round(size * 4))
        return self.fonts[key]

    def width(self, text: str, size: float, bold: bool = False, italic: bool = False) -> float:
        return self.font(size, bold, italic).getlength(text) / 4

    def line_height(self, size: float) -> float:
        return sum(self.font(size).getmetrics()) / 4


def bands(values: np.ndarray) -> list[tuple[int, int]]:
    """Half-open ranges for contiguous nonzero projection values."""
    edges = np.diff(np.pad(np.asarray(values, dtype=np.int8), (1, 1)))
    return list(zip(np.flatnonzero(edges == 1).tolist(), np.flatnonzero(edges == -1).tolist()))


def ink_mask(image: Image.Image) -> np.ndarray:
    return (np.asarray(image.convert("L")) < 210).astype(np.uint8)


def text_lines(image: Image.Image) -> list[tuple[int, int]]:
    ink = ink_mask(image)
    if ink.mean() > 0.35:
        # Photographed paper and security textures can sit below the absolute
        # ink threshold. Measure local contrast instead of the paper itself.
        gray = np.asarray(image.convert("L"))
        ink = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY_INV, 31, 12) // 255
    # Remove long rules and very large solid components before measuring glyph rows.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    clean = np.zeros_like(ink)
    for index in range(1, count):
        x, y, width, height, area = stats[index]
        if (2 <= height <= image.height and area >= 2 and width < image.width * 0.85
                and width / max(height, 1) < 25 and height / max(width, 1) < 12):
            patch = clean[y:y + height, x:x + width]
            patch[labels[y:y + height, x:x + width] == index] = 1
    projection = clean.sum(axis=1) >= max(2, image.width * 0.003)
    # Join detached dots/accents without bridging typical interline gaps.
    projection = cv2.morphologyEx(projection.astype(np.uint8)[:, None], cv2.MORPH_CLOSE,
                                 np.ones((3, 1), np.uint8))[:, 0]
    return [(start, end) for start, end in bands(projection) if end - start >= 3]


def source_font_size(image: Image.Image, points_per_pixel: float, metrics: FontMetrics,
                     text: str | None = None) -> tuple[float | None, float | None]:
    lines = text_lines(image)
    if not lines:
        return None, None
    glyph_height = median(end - start for start, end in lines) * points_per_pixel
    sample = re.sub(r"[*_`#]", "", text).strip() if text and len(lines) == 1 else "AgӨү"
    reference = metrics.font(12).getbbox(sample or "AgӨү")
    ratio = (reference[3] - reference[1]) / 4 / 12
    size = round(min(36, max(4, glyph_height / max(ratio, 0.5))) * 2) / 2
    if text and len(lines) == 1 and len(sample) < 100 and '\n' not in text.strip():
        # A logo or tall capital can inflate the height measurement. A source
        # line still has to fit horizontally without inventing another line.
        advance = metrics.width(sample, size)
        if advance > image.width * points_per_pixel:
            size = max(4, int(size * image.width * points_per_pixel / advance * 2) / 2)
    pitch = median(lines[index + 1][0] - lines[index][0] for index in range(len(lines) - 1)) * points_per_pixel if len(lines) > 1 else None
    # A huge inter-paragraph gap is not a line spacing measurement.
    if pitch is not None and not size <= pitch <= size * 2:
        pitch = None
    return size, pitch


@dataclass
class TableGeometry:
    columns: list[int] | None
    rows: list[int] | None
    horizontal: np.ndarray
    vertical: np.ndarray
    pixels: np.ndarray


def boundaries(projection: np.ndarray, count: int, extent: int) -> list[int] | None:
    # Antialiasing can split one rule into several adjacent bands. OCR boxes
    # also include a few pixels outside the outer rules.
    joined = cv2.morphologyEx(projection.astype(np.uint8)[None, :], cv2.MORPH_CLOSE,
                              np.ones((1, 5), np.uint8))[0]
    positions = [round((a + b - 1) / 2) for a, b in bands(joined)]
    if not positions:
        return None
    tolerance = max(8, extent * 0.012)
    if positions[0] > tolerance:
        positions.insert(0, 0)
    else:
        positions[0] = 0
    if positions[-1] < extent - tolerance:
        positions.append(extent)
    else:
        positions[-1] = extent
    return positions if len(positions) == count + 1 else None


def table_geometry(image: Image.Image, columns: int, rows: int) -> TableGeometry:
    gray = np.asarray(image.convert("L"))
    ink = (cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY_INV, 31, 12) > 0).astype(np.uint8)
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, max(12, image.width // max(columns * 2, 2))), np.uint8))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((max(20, image.height // max(rows, 1)), 1), np.uint8))
    # Light rules on shaded headers are invisible to a dark-ink threshold.
    edges = (np.abs(np.diff(gray.astype(np.int16), axis=0, prepend=gray[:1])) > 12).astype(np.uint8)
    edges = cv2.morphologyEx(edges, cv2.MORPH_OPEN,
                           np.ones((1, max(12, image.width // max(columns * 2, 2))), np.uint8))
    horizontal |= edges
    glyphs = ink & (1 - horizontal) & (1 - vertical)
    xs = boundaries(vertical.sum(axis=0) > image.height * 0.18, columns, image.width)
    ys = boundaries(horizontal.sum(axis=1) > image.width * 0.18, rows, image.height)
    if ys is None:
        # Resampling spreads pale header rules over several rows. Retry with
        # lower contrast, still requiring long rules and the semantic row count.
        faint = (np.abs(np.diff(gray.astype(np.int16), axis=0, prepend=gray[:1])) > 5).astype(np.uint8)
        faint = cv2.morphologyEx(faint, cv2.MORPH_OPEN,
                                np.ones((1, max(12, image.width // max(columns * 2, 2))), np.uint8))
        candidate = horizontal | faint
        ys = boundaries(candidate.sum(axis=1) > image.width * 0.18, rows, image.height)
        if ys is not None:
            horizontal = candidate
            glyphs = ink & (1 - horizontal) & (1 - vertical)
    if ys is None:
        # Borderless tables: only trust row bands when their count agrees with the semantic table.
        lines = bands(glyphs.sum(axis=1) > max(2, image.width * 0.01))
        if len(lines) == rows:
            inset = lines[0][0]
            ys = [0, *[start - inset for start, _ in lines[1:]], image.height]
    if xs is None:
        gaps = [(a, b) for a, b in bands(glyphs.sum(axis=0) <= max(1, image.height * 0.005))
                if a > 0 and b < image.width and b - a >= max(6, image.width * 0.012)]
        rule_count = len(bands(vertical.sum(axis=0) > image.height * 0.18))
        if len(gaps) == columns - 1 and rule_count <= columns:
            xs = [0, *[round((a + b) / 2) for a, b in gaps], image.width]
        elif ys is not None and rule_count <= columns:
            # Header text often supplies column starts when a region combines
            # differently ruled tables. Require exactly the expected count,
            # and never override evidence of more rules than semantic columns.
            projection = glyphs[ys[0]:ys[1]].sum(axis=0) > 0
            joined = cv2.morphologyEx(projection.astype(np.uint8)[None, :], cv2.MORPH_CLOSE,
                                      np.ones((1, max(8, image.width // 50)), np.uint8))[0]
            groups = bands(joined)
            if len(groups) == columns:
                inset = groups[0][0]
                xs = [0, *[start - inset for start, _ in groups[1:]], image.width]
    if xs is None and ys is not None:
        xs = header_columns(glyphs, ys[1], columns)
    return TableGeometry(xs, ys, horizontal, vertical, np.asarray(image.convert("RGB")))


def header_columns(glyphs: np.ndarray, header_end: int, columns: int) -> list[int] | None:
    """Locate borderless columns using header groups and body whitespace.

    Centered headings do not mark column starts. Only accept a complete set of
    headings with a low-ink separator between each pair in the table body.
    """
    height, width = glyphs.shape
    header = glyphs[:header_end]
    lines = bands(header.sum(axis=1) > max(2, width * 0.01))
    if not lines:
        return None
    glyph_height = median(b - a for a, b in lines)
    projection = (header.sum(axis=0) >= max(2, glyph_height * 0.25)).astype(np.uint8)
    joined = cv2.morphologyEx(projection[None, :], cv2.MORPH_CLOSE,
                              np.ones((1, max(3, round(glyph_height / 2))), np.uint8))[0]
    groups = [(a, b) for a, b in bands(joined) if b - a >= max(3, glyph_height * 0.2)]
    if len(groups) != columns:
        return None
    body = glyphs[header_end:]
    if not body.size:
        return None
    gaps = bands(body.sum(axis=0) <= max(1, (height - header_end) * 0.005))
    positions = [0]
    for (_, end), (start, _) in pairwise(groups):
        candidates = [(max(a, end), min(b, start)) for a, b in gaps if a < start and b > end]
        if not candidates:
            return None
        a, b = max(candidates, key=lambda gap: gap[1] - gap[0])
        if b - a < 3:
            return None
        positions.append(round((a + b) / 2))
    return [*positions, width]


def cell_appearance(layout: TableGeometry, box: tuple[int, int, int, int]) -> tuple[str | None, dict[str, tuple[str, int]]]:
    x1, y1, x2, y2 = box
    pixels = layout.pixels[y1 + 2:max(y1 + 3, y2 - 2), x1 + 2:max(x1 + 3, x2 - 2)]
    fill = None
    if pixels.size:
        quantized = (pixels.reshape(-1, 3).astype(np.int32) // 8) * 8
        colors, counts = np.unique(quantized, axis=0, return_counts=True)
        color = colors[counts.argmax()]
        if counts.max() / len(quantized) > 0.55 and 60 < color.mean() < 244:
            fill = "".join(f"{int(value):02X}" for value in np.median(pixels.reshape(-1, 3), axis=0))
    rules = {}
    height, width = layout.horizontal.shape
    for side, position, start, end, mask in [
        ("top", y1, x1, x2, layout.horizontal), ("bottom", y2 - 1, x1, x2, layout.horizontal),
        ("left", x1, y1, y2, layout.vertical.T), ("right", x2 - 1, y1, y2, layout.vertical.T),
    ]:
        strip = mask[max(0, position - 2):position + 3, start:end]
        if strip.size and np.any(strip, axis=0).mean() > 0.65:
            thickness = max(1, round(strip.sum() / max(1, end - start)))
            if side in {"top", "bottom"}:
                colors = layout.pixels[max(0, position - 2):min(height, position + 3), start:end]
            else:
                colors = layout.pixels[start:end, max(0, position - 2):min(width, position + 3)]
            # Measure along the rule, including pale gray lines. Selecting only
            # dark pixels turns light rules black (or samples nearby letters).
            darkest = colors.min(axis=0 if side in {"top", "bottom"} else 1)
            color = np.median(darkest, axis=0)
            rules[side] = ("".join(f"{int(value):02X}" for value in color), thickness)
    return fill, rules


def missing_graphics(image: Image.Image, regions: list[Any]) -> tuple[list[tuple[int, int, int, int]], int]:
    """Find substantial components, excluding known pictures/tables and glyph-sized marks.

    Only colored solid components may be recovered inside text regions. All
    other unexplained ink is counted for review, not silently rasterized.
    """
    pixels = np.asarray(image.convert("RGB"))
    ink = ink_mask(image)
    covered = np.zeros_like(ink)
    excluded = np.zeros_like(ink)
    for region in regions:
        x1, y1, x2, y2 = region.bbox
        covered[max(0, y1 - 3):y2 + 3, max(0, x1 - 3):x2 + 3] = 1
        if region.category in {"Picture", "Table", "Formula"}:
            excluded[max(0, y1 - 3):y2 + 3, max(0, x1 - 3):x2 + 3] = 1
    color = ((pixels.max(axis=2).astype(int) - pixels.min(axis=2)) > 70) & (pixels.min(axis=2) < 180)
    candidates = ((ink & (1 - covered)) | color.astype(np.uint8)) & (1 - excluded)
    count, _, stats, _ = cv2.connectedComponentsWithStats(candidates, 8)
    boxes = []
    min_side = max(12, min(image.size) * 0.024)
    for index in range(1, count):
        x, y, width, height, area = stats[index]
        density = area / max(1, width * height)
        compact_dot = (min(width, height) >= min_side * 0.55 and 0.8 < width / height < 1.25 and density > 0.72)
        if (min(width, height) < min_side and not compact_dot) or area < min_side ** 2 * 0.25 or density < 0.2:
            continue
        if max(width, height) > max(image.size) * 0.45:
            continue
        patch = pixels[y:y + height, x:x + width].mean(axis=2)
        surround = pixels[max(0, y - 4):min(image.height, y + height + 4),
                          max(0, x - 4):min(image.width, x + width + 4)].mean(axis=2)
        edge = np.concatenate((surround[0], surround[-1], surround[:, 0], surround[:, -1]))
        if float(np.median(edge) - np.percentile(patch, 10)) < 40:
            # Paper shadows and security textures are not isolated graphics.
            continue
        overlap = covered[y:y + height, x:x + width].mean()
        if overlap > 0.1 and height < min_side * 2:
            # Colored letters tend to have neighboring components on the same
            # baseline. Isolated dots and larger illustrations do not.
            others = stats[1:]
            same_line = (np.abs(others[:, 1] + others[:, 3] / 2 - (y + height / 2)) < height * 0.4)
            similar_height = (others[:, 3] > height * 0.5) & (others[:, 3] < height * 1.8)
            nearby = (others[:, 0] < x + width + height) & (others[:, 0] + others[:, 2] > x - height)
            if np.count_nonzero(same_line & similar_height & nearby) > 1:
                continue
        # Reject solid highlighting/colored rectangles over recognized text.
        solid_rectangle = ink[y:y + height, x:x + width].mean() > 0.93
        if overlap > 0.1 and (solid_rectangle or color[y:y + height, x:x + width].mean() < 0.2):
            continue
        boxes.append((max(0, int(x) - 1), max(0, int(y) - 1), min(image.width, int(x + width) + 1), min(image.height, int(y + height) + 1)))
    residual = ink & (1 - covered)
    for x1, y1, x2, y2 in boxes:
        residual[y1:y2, x1:x2] = 0
    return boxes, int(residual.sum())


def word_tokens(text: str) -> list[str]:
    return re.findall(r"\S+\s*|\s+", text)


def text_rotation(image: Image.Image, text: str, metrics: FontMetrics) -> int | None:
    """Infer a quarter turn only when recognized words match rotated glyphs.

    Aspect ratio alone cannot distinguish a rotated phrase from stacked text.
    Both directions must be compared with a rendered template; ambiguous cases
    remain unchanged and should be reviewed by the caller.
    """
    if image.height < image.width * 3 or not 4 <= len(text.strip()) <= 160 or '\n' in text.strip():
        return None
    from PIL import ImageDraw

    font = metrics.font(12)
    bbox = font.getbbox(text.strip())
    template = Image.new('L', (max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])), 255)
    ImageDraw.Draw(template).text((-bbox[0], -bbox[1]), text.strip(), font=font, fill=0)

    def normalized(candidate: Image.Image) -> np.ndarray | None:
        mask = ink_mask(candidate)
        ys, xs = np.nonzero(mask)
        if not len(xs):
            return None
        mask = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        return cv2.resize(mask.astype(np.float32), (240, 32), interpolation=cv2.INTER_AREA)

    target = normalized(template)
    if target is None:
        return None
    scores = []
    for rotation in (90, 270):
        candidate = normalized(image.rotate(rotation, expand=True))
        if candidate is None:
            return None
        scores.append(float(2 * np.minimum(candidate, target).sum() / max(1, candidate.sum() + target.sum())))
    best = int(np.argmax(scores))
    return (90, 270)[best] if scores[best] >= 0.55 and abs(scores[0] - scores[1]) >= 0.08 else None


def flat_background(image: Image.Image) -> tuple[int, int, int] | None:
    """Only erase a recognized overlay where its background is clearly flat."""
    pixels = np.asarray(image.convert('RGB')).astype(np.int16)
    color = np.median(pixels.reshape(-1, 3), axis=0)
    close = np.max(np.abs(pixels - color), axis=2) < 18
    edge = np.concatenate((close[0], close[-1], close[:, 0], close[:, -1]))
    if close.mean() >= 0.70 and edge.mean() >= 0.95:
        return tuple(int(value) for value in color)
    return None


def text_color(image: Image.Image) -> tuple[int, int, int] | None:
    pixels = np.asarray(image.convert('RGB')).astype(np.int16)
    background = np.median(pixels.reshape(-1, 3), axis=0)
    foreground = pixels[np.max(np.abs(pixels - background), axis=2) > 90]
    if len(foreground) < 5:
        return None
    colors, counts = np.unique(foreground // 16, axis=0, return_counts=True)
    dominant = colors[counts.argmax()]
    solid = foreground[np.all(foreground // 16 == dominant, axis=1)]
    color = np.median(solid, axis=0)
    # Anti-aliased edges blend the text color into the background; count them as text.
    direction = color - background
    blend = np.clip((foreground - background) @ direction / max(direction @ direction, 1), 0, 1)
    residual = np.max(np.abs(foreground - background - blend[:, None] * direction), axis=1)
    if (residual < 48).mean() < 0.6:
        return None  # Mixed colors cannot be assigned reliably without span boxes.
    if color.mean() > 220:
        return (0, 0, 0)  # White source lettering needs a background we cannot infer.
    return tuple(int(value) for value in color)


def cell_alignment(layout: TableGeometry, box: tuple[int, int, int, int]) -> str | None:
    x1, y1, x2, y2 = box
    pixels = layout.pixels[y1 + 3:y2 - 3, x1 + 3:x2 - 3].astype(np.int16)
    if not pixels.size:
        return None
    background = np.median(pixels.reshape(-1, 3), axis=0)
    ink = (pixels.mean(axis=2) < background.mean() - 70).astype(np.uint8)
    # Remove rules without confusing them for centered text.
    ink &= 1 - layout.horizontal[y1 + 3:y2 - 3, x1 + 3:x2 - 3]
    ink &= 1 - layout.vertical[y1 + 3:y2 - 3, x1 + 3:x2 - 3]
    _, xs = np.nonzero(ink)
    width = ink.shape[1]
    if len(xs) < 3 or xs.max() - xs.min() > width * 0.8:
        return None
    if abs((xs.min() + xs.max()) / 2 - width / 2) < width * 0.08:
        return 'center'
    if width - xs.max() < max(5, width * 0.05) and xs.min() > width * 0.2:
        return 'right'
    return None
