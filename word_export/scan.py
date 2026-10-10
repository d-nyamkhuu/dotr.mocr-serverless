"""Conservative small-angle scan correction using OpenCV pixel measurements."""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

from word_export.measure import text_lines


@dataclass(frozen=True)
class SkewEstimate:
    # Counterclockwise correction, matching OpenCV and Pillow conventions.
    angle: float
    confident: bool
    reason: str


def estimate_skew(image: Image.Image) -> SkewEstimate:
    """Find a shared baseline within ±5°; leave sparse/ambiguous pages alone."""
    gray = np.asarray(image.convert("L"))
    scale = min(1, 1200 / max(gray.shape))
    if scale < 1:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    height, width = gray.shape
    if min(height, width) < 40:
        return SkewEstimate(0, False, "too little image evidence")
    ink = (cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY_INV, 31, 12) > 0).astype(np.uint8)
    # Long rules provide stronger evidence than text, especially on sparse tables.
    lines = cv2.HoughLinesP(ink, 1, np.pi / 1800, threshold=max(30, width // 8),
                           minLineLength=max(40, width // 5), maxLineGap=10)
    angles, lengths = [], []
    if lines is not None:
        for x1, y1, x2, y2 in lines[:, 0]:
            angle = math.degrees(math.atan2(float(y2 - y1), float(x2 - x1)))
            angle = (angle + 45) % 90 - 45
            if abs(angle) <= 7:
                angles.append(angle)
                lengths.append(math.hypot(x2 - x1, y2 - y1))
    if len(angles) >= 3:
        order = np.argsort(angles)
        weights = np.asarray(lengths)[order]
        middle = np.searchsorted(np.cumsum(weights), weights.sum() / 2)
        angle = float(np.asarray(angles)[order][middle])
        agreeing = np.abs(np.asarray(angles) - angle) < 0.3
        if np.asarray(lengths)[agreeing].sum() >= max(width, height) * 2 and agreeing.mean() >= 0.7:
            if abs(angle) > 5:
                return SkewEstimate(angle, False, "tilt exceeds the supported ±5° range")
            # Refine below Hough's endpoint quantization: a tenth of a degree
            # can still smear a faint rule across several pixel rows.
            candidates = np.arange(angle - 0.2, angle + 0.201, 0.025)
            scores = []
            for candidate in candidates:
                matrix = cv2.getRotationMatrix2D((width / 2, height / 2), float(candidate), 1)
                rotated = cv2.warpAffine(ink, matrix, (width, height), flags=cv2.INTER_NEAREST)
                scores.append(float(np.square(rotated.sum(axis=1).astype(float)).sum()
                                    + np.square(rotated.sum(axis=0).astype(float)).sum()))
            angle = float(candidates[int(np.argmax(scores))])
            if abs(angle) > 5:
                return SkewEstimate(angle, False, "tilt exceeds the supported ±5° range")
            return SkewEstimate(angle if abs(angle) >= 0.15 else 0, True, "consistent page rules")
    # Keep glyph-sized components; photos, rules and solid fills must not set the tilt.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    keep = np.zeros(count, dtype=np.uint8)
    for index in range(1, count):
        _, _, w, h, area = stats[index]
        if 2 <= h <= height * 0.06 and 1 <= w <= width * 0.08 and 3 <= area <= width * height * 0.001:
            if 0.08 <= w / h <= 8 and area / (w * h) >= 0.08:
                keep[index] = 1
    if keep.sum() < 25:
        return SkewEstimate(0, False, "too few text components or page rules")
    mask = keep[labels]
    center = (width / 2, height / 2)

    def score(angle: float) -> float:
        matrix = cv2.getRotationMatrix2D(center, float(angle), 1)
        rotated = cv2.warpAffine(mask, matrix, (width, height), flags=cv2.INTER_NEAREST)
        projection = rotated.sum(axis=1).astype(float)
        return float(np.square(projection).sum())

    candidates = np.arange(-5, 5.01, 0.25)
    scores = [score(angle) for angle in candidates]
    angle = float(candidates[int(np.argmax(scores))])
    if abs(angle) >= 5:
        return SkewEstimate(angle, False, "tilt is at the ±5° search limit")
    fine = np.arange(angle - 0.2, angle + 0.201, 0.05)
    angle = float(fine[int(np.argmax([score(value) for value in fine]))])
    peak = score(angle)
    shoulders = max(score(angle - 0.5), score(angle + 0.5))
    alternatives = [value for candidate, value in zip(candidates, scores) if abs(candidate - angle) > 0.75]
    if alternatives and max(alternatives) >= peak * 0.94:
        return SkewEstimate(angle, False, "multiple competing text angles")
    if peak < shoulders * 1.04:
        return SkewEstimate(angle, False, "text baselines do not agree on a clear angle")
    if abs(angle) < 0.15 or peak < score(0) * 1.06:
        return SkewEstimate(0, True, "text is already approximately upright")
    return SkewEstimate(round(angle, 2), True, "consistent text baselines")


def rotate_scan(image: Image.Image, angle: float) -> tuple[Image.Image, np.ndarray]:
    """Rotate once onto an expanded white canvas, retaining edge content."""
    width, height = image.size
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1)
    corners = np.array([[0, 0, 1], [width, 0, 1], [width, height, 1], [0, height, 1]]) @ matrix.T
    low = np.floor(corners.min(axis=0))
    high = np.ceil(corners.max(axis=0))
    matrix[:, 2] -= low
    size = tuple((high - low).astype(int))
    pixels = cv2.warpAffine(np.asarray(image.convert("RGB")), matrix, size,
                            flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT,
                            borderValue=(255, 255, 255))
    return Image.fromarray(pixels), matrix


def transform_box(box: tuple[int, int, int, int], matrix: np.ndarray,
                  size: tuple[int, int]) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    corners = np.array([[x1, y1, 1], [x2, y1, 1], [x2, y2, 1], [x1, y2, 1]]) @ matrix.T
    low = np.maximum(0, np.floor(corners.min(axis=0))).astype(int)
    high = np.minimum(size, np.ceil(corners.max(axis=0))).astype(int)
    return int(low[0]), int(low[1]), int(high[0]), int(high[1])


def content_box(image: Image.Image, original: tuple[int, int, int, int], matrix: np.ndarray,
                *, table: bool = False) -> tuple[int, int, int, int]:
    """Remove rotation-induced blank corners, using only the original region's ink."""
    box = transform_box(original, matrix, image.size)
    x1, y1, x2, y2 = original
    corners = np.array([[x1, y1, 1], [x2, y1, 1], [x2, y2, 1], [x1, y2, 1]]) @ matrix.T
    corners -= box[:2]
    with image.crop(box) as crop:
        ink = (np.asarray(crop.convert("L")) < (245 if table else 210)).astype(np.uint8)
    mask = np.zeros_like(ink)
    cv2.fillConvexPoly(mask, np.round(corners).astype(np.int32), 1)
    ink &= mask
    if not table:
        # A rotated axis-aligned OCR rectangle includes triangles from adjacent
        # lines. Undo its geometric inflation to select whole baselines, then
        # fit the box to those glyphs instead of retaining neighboring fragments.
        cosine, sine = abs(matrix[0, 0]), abs(matrix[0, 1])
        upright_height = ((y2 - y1) * cosine - (x2 - x1) * sine) / (cosine ** 2 - sine ** 2)
        center_y = float(np.array([(x1 + x2) / 2, (y1 + y2) / 2, 1]) @ matrix[1]) - box[1]
        lines = text_lines(Image.fromarray((1 - ink) * 255))
        top, bottom = center_y - max(3, upright_height / 2), center_y + max(3, upright_height / 2)
        # Ragged right edges (especially a short final heading line) make the
        # rectangle inversion underestimate height. Retain a whole glyph band
        # when a meaningful part intersects the estimate.
        selected = [(a, b) for a, b in lines
                    if min(b, bottom) - max(a, top) >= max(2, (b - a) * 0.2)]
        if selected:
            ink[:selected[0][0]] = 0
            ink[selected[-1][1]:] = 0
    rows = np.flatnonzero(ink.sum(axis=1) >= max(2, ink.shape[1] * 0.002))
    columns = np.flatnonzero(ink.sum(axis=0) >= 2)
    if not len(rows) or not len(columns):
        return box
    # Keep a little antialiasing/outline allowance; never discard recognized text.
    return (box[0] + max(0, int(columns[0]) - 2), box[1] + max(0, int(rows[0]) - 2),
            min(box[2], box[0] + int(columns[-1]) + 3),
            min(box[3], box[1] + int(rows[-1]) + 3))
