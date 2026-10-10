"""Send independent images concurrently to the dots.mocr layout API."""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

import httpx


@dataclass
class BatchResult:
    index: int
    source: str
    response: dict[str, Any] | None
    elapsed_seconds: float
    error: str | None = None


def build_client(
    *,
    base_url: str,
    token: str,
    timeout: float,
) -> httpx.AsyncClient:
    if not token:
        raise ValueError("An OCR bearer token is required (--token or DOTS_MOCR_TOKEN)")
    return httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(timeout, connect=30),
        follow_redirects=True,
        trust_env=False,
    )


def request_parts(source: str) -> tuple[dict[str, str], dict[str, tuple[str, bytes, str]] | None]:
    """Build form fields for an HTTPS URL or a local PNG/JPEG file."""
    if source.startswith("https://"):
        return {"image_url": source}, None
    if source.startswith(("http://", "data:")):
        raise ValueError("Remote images must use HTTPS; data URLs are not supported")

    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Image not found: {path}")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if media_type not in {"image/jpeg", "image/png"}:
        raise ValueError(f"Unsupported image type for {path}: {media_type}")
    return {}, {"file": (path.name, path.read_bytes(), media_type)}


async def send_one(
    client: httpx.AsyncClient,
    *,
    index: int,
    source: str,
) -> BatchResult:
    started = time.perf_counter()
    try:
        data, files = request_parts(source)
        response = await client.post("/v1/parse", data=data, files=files)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("API response was not a JSON object")
        return BatchResult(
            index=index,
            source=source,
            response=payload,
            elapsed_seconds=time.perf_counter() - started,
        )
    except Exception as exc:  # Keep partial results for large concurrent jobs.
        detail = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            try:
                detail = json.dumps(exc.response.json(), ensure_ascii=False)
            except ValueError:
                detail = exc.response.text
        return BatchResult(
            index=index,
            source=source,
            response=None,
            elapsed_seconds=time.perf_counter() - started,
            error=f"{type(exc).__name__}: {detail}",
        )


async def send_many(
    client: httpx.AsyncClient,
    sources: Sequence[str],
    *,
    concurrency: int,
) -> list[BatchResult]:
    """Send requests concurrently while preserving source ordering."""
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded(index: int, source: str) -> BatchResult:
        async with semaphore:
            return await send_one(client, index=index, source=source)

    return await asyncio.gather(
        *(bounded(index, source) for index, source in enumerate(sources))
    )


def parser() -> argparse.ArgumentParser:
    arg_parser = argparse.ArgumentParser(description=__doc__)
    arg_parser.add_argument("images", nargs="+", help="Local PNG/JPEG paths or HTTPS URLs")
    arg_parser.add_argument("--base-url", required=True, help="Deployed Modal endpoint URL")
    arg_parser.add_argument("--concurrency", type=int, default=32)
    arg_parser.add_argument("--timeout", type=float, default=900.0)
    arg_parser.add_argument("--token", default=os.getenv("DOTS_MOCR_TOKEN"))
    return arg_parser


async def async_main(args: argparse.Namespace) -> int:
    async with build_client(
        base_url=args.base_url,
        token=args.token,
        timeout=args.timeout,
    ) as client:
        results = await send_many(client, args.images, concurrency=args.concurrency)

    print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
    return 1 if any(result.error for result in results) else 0


def main() -> int:
    return asyncio.run(async_main(parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
