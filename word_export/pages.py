"""Validated OCR pages: saved layout results paired with their source images."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError


@dataclass
class Region:
    bbox: tuple[int, int, int, int]
    category: str
    text: str
    label: str
    rotation: int = 0


@dataclass
class Page:
    path: Path
    width: int
    height: int
    regions: list[Region]


def load_image(path: Path) -> Image.Image:
    # layout_api.load_image is the byte-based twin used by the server, which
    # cannot depend on this optional package.
    try:
        with Image.open(path) as image:
            if image.format not in {"PNG", "JPEG"}:
                raise ValueError(f"{path}: only PNG and JPEG images are supported")
            oriented = ImageOps.exif_transpose(image)
            try:
                return oriented.convert("RGB")
            finally:
                oriented.close()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise ValueError(f"Cannot read image {path}: {exc}") from exc


def load_pages(payload: Any, paths: Sequence[Path]) -> list[Page]:
    """Pair OCR results with page images.

    ``payload`` is one ``/v1/parse`` response, a list of them, or the list that
    ``scripts.batch_client`` writes (``asdict(BatchResult)`` entries with
    ``index``, ``response`` and ``error``).
    """
    results = [payload] if isinstance(payload, dict) else payload
    if not paths or not isinstance(results, list) or len(results) != len(paths):
        raise ValueError("Provide exactly one OCR result per image, in the same order")
    pages = []
    for page_index, (entry, path) in enumerate(zip(results, paths), start=1):
        label = f"Page {page_index}"
        if not isinstance(entry, dict):
            raise TypeError(f"{label}: OCR result must be an object")
        if "response" in entry:
            if entry.get("error") or not isinstance(entry["response"], dict):
                raise ValueError(f"{label}: batch OCR request failed")
            if "index" in entry and (type(entry["index"]) is not int or entry["index"] != page_index - 1):
                raise ValueError(f"{label}: batch result indices are out of order")
            entry = entry["response"]
        if entry.get("status") != "ok" or not isinstance(entry.get("elements"), list):
            raise ValueError(f"{label}: successful OCR with layout elements is required")
        metadata = entry.get("image")
        if not isinstance(metadata, dict):
            raise TypeError(f"{label}: missing image dimensions")
        with load_image(path) as image:
            width, height = image.size
        dimensions = (metadata.get("original_width"), metadata.get("original_height"))
        if any(type(value) is not int for value in dimensions) or dimensions != (width, height):
            raise ValueError(f"{label}: OCR dimensions do not match the EXIF-oriented image {width}×{height}")
        regions = []
        for region_index, element in enumerate(entry["elements"], start=1):
            location = f"{label}, region {region_index}"
            if not isinstance(element, dict):
                raise TypeError(f"{location}: layout element must be an object")
            bbox = element.get("bbox")
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or any(
                type(value) is not int for value in bbox
            ):
                raise ValueError(f"{location}: bounding box must contain four integer coordinates")
            x1, y1, x2, y2 = bbox
            if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
                raise ValueError(f"{location}: bounding box is outside the image or has no area")
            category = element.get("category")
            text = element.get("text")
            if not isinstance(category, str) or not category.strip():
                raise ValueError(f"{location}: missing region category")
            if text is None and category == "Picture":
                text = ""
            if not isinstance(text, str):
                raise TypeError(f"{location}: text must be a string")
            # XML 1.0 disallows these even though JSON permits them.
            if any(not (char in "\t\n\r" or 0x20 <= ord(char) <= 0xD7FF
                        or 0xE000 <= ord(char) <= 0xFFFD or 0x10000 <= ord(char) <= 0x10FFFF)
                   for char in text):
                raise ValueError(f"{location}: text contains characters invalid in Word XML")
            rotation = element.get("rotation", 0)
            if type(rotation) is not int or rotation not in {0, 90, 180, 270}:
                raise ValueError(f"{location}: rotation must be 0, 90, 180 or 270 clockwise degrees")
            regions.append(Region(tuple(bbox), category, text, location, rotation))
        pages.append(Page(path, width, height, regions))
    return pages
