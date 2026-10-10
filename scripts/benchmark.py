"""Run concurrency sweeps against the deployed dots.mocr endpoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

import httpx

from scripts.batch_client import BatchResult, build_client, send_one


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(percentile_value * len(ordered)) - 1)
    return ordered[index]


def parse_levels(value: str) -> list[int]:
    levels = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not levels or any(level < 1 for level in levels):
        raise argparse.ArgumentTypeError("levels must be positive comma-separated integers")
    return levels


def record(level: int, started: float, level_started: float, result: BatchResult) -> dict[str, object]:
    """Flatten one result for plotting; offsets are seconds since the level started."""
    response = result.response or {}
    generation = response.get("generation") or {}
    usage = generation.get("usage") or {}
    return {
        "concurrency": level,
        "index": result.index,
        "source": result.source,
        "started_seconds": started - level_started,
        "elapsed_seconds": result.elapsed_seconds,
        "status": "error" if result.error else response.get("status"),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "finish_reason": generation.get("finish_reason"),
        "error": result.error,
    }


async def run_level(client: httpx.AsyncClient, sources: list[str], level: int) -> list[dict[str, object]]:
    semaphore = asyncio.Semaphore(level)
    level_started = time.perf_counter()

    async def bounded(index: int, source: str) -> dict[str, object]:
        async with semaphore:
            started = time.perf_counter()
            result = await send_one(client, index=index, source=source)
            return record(level, started, level_started, result)

    return await asyncio.gather(*(bounded(index, source) for index, source in enumerate(sources)))


def summarize(level: int, records: list[dict[str, object]]) -> dict[str, object]:
    elapsed = max(item["started_seconds"] + item["elapsed_seconds"] for item in records)
    latencies = [item["elapsed_seconds"] for item in records if item["status"] != "error"]
    errors = [item for item in records if item["status"] == "error"]
    return {
        "concurrency": level,
        "requests": len(records),
        "ok": sum(item["status"] == "ok" for item in records),
        "degraded": sum(item["status"] == "degraded" for item in records),
        "errors": len(errors),
        "elapsed_seconds": elapsed,
        "requests_per_second": len(records) / elapsed,
        "prompt_tokens_per_second": sum(item["prompt_tokens"] for item in records) / elapsed,
        "completion_tokens_per_second": sum(item["completion_tokens"] for item in records) / elapsed,
        "latency_mean_seconds": statistics.fmean(latencies) if latencies else 0.0,
        "latency_p50_seconds": percentile(latencies, 0.50),
        "latency_p90_seconds": percentile(latencies, 0.90),
        "latency_p95_seconds": percentile(latencies, 0.95),
        "latency_p99_seconds": percentile(latencies, 0.99),
        "latency_max_seconds": max(latencies, default=0.0),
        "error_examples": [item["error"] for item in errors[:3]],
    }


async def async_main(args: argparse.Namespace) -> int:
    # vLLM caches repeated prompts and images, so a repeated page skips most of
    # its prefill. Take images in order across the whole run to avoid repeats.
    position = 0

    def take(count: int) -> list[str]:
        nonlocal position
        sources = [args.images[(position + index) % len(args.images)] for index in range(count)]
        position += count
        return sources

    request_counts = [args.requests_per_level or max(8, level * 2) for level in args.levels]
    total = sum(request_counts) + (0 if args.no_warmup else 1)
    if total > len(args.images):
        print(f"Warning: {total} requests reuse {len(args.images)} images; cached repeats run faster",
              file=sys.stderr)

    summaries: list[dict[str, object]] = []
    records: list[dict[str, object]] = []
    async with build_client(base_url=args.base_url, token=args.token, timeout=args.timeout) as client:
        if not args.no_warmup:
            await run_level(client, take(1), 1)
        for level, count in zip(args.levels, request_counts):
            level_records = await run_level(client, take(count), level)
            records.extend(level_records)
            summaries.append(summarize(level, level_records))
            print(json.dumps(summaries[-1]), file=sys.stderr)

    if args.output:
        args.output.write_text(json.dumps({"summaries": summaries, "requests": records}, indent=2) + "\n")
    print(json.dumps(summaries, indent=2))
    return 1 if any(summary["errors"] for summary in summaries) else 0


def parser() -> argparse.ArgumentParser:
    arg_parser = argparse.ArgumentParser(description=__doc__)
    arg_parser.add_argument("images", nargs="+", help="Representative image paths or URLs, ideally all distinct")
    arg_parser.add_argument("--base-url", required=True)
    arg_parser.add_argument("--levels", type=parse_levels, default=parse_levels("8,16,32,64"))
    arg_parser.add_argument("--requests-per-level", type=int)
    arg_parser.add_argument("--timeout", type=float, default=900.0)
    arg_parser.add_argument("--no-warmup", action="store_true")
    arg_parser.add_argument("--output", type=Path, help="Also write summaries and per-request records as JSON")
    arg_parser.add_argument("--token", default=os.getenv("DOTS_MOCR_TOKEN"))
    return arg_parser


def main() -> int:
    return asyncio.run(async_main(parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
