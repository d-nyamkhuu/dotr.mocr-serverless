"""Run concurrency sweeps against the deployed dots.mocr endpoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from dataclasses import asdict

from scripts.batch_client import build_client, send_many


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


async def async_main(args: argparse.Namespace) -> int:
    summaries: list[dict[str, object]] = []
    failed = False
    async with build_client(
        base_url=args.base_url,
        token=args.token,
        timeout=args.timeout,
    ) as client:
        if not args.no_warmup:
            await send_many(
                client,
                [args.images[0]],
                concurrency=1,
            )

        for level in args.levels:
            request_count = args.requests_per_level or max(level * 2, len(args.images))
            sources = [args.images[index % len(args.images)] for index in range(request_count)]
            started = time.perf_counter()
            results = await send_many(
                client,
                sources,
                concurrency=level,
            )
            elapsed = time.perf_counter() - started
            errors = [result for result in results if result.error]
            failed = failed or bool(errors)
            latencies = [result.elapsed_seconds for result in results]
            completion_tokens = sum(
                int(
                    (((result.response or {}).get("generation") or {}).get("usage") or {}).get(
                        "completion_tokens", 0
                    )
                    or 0
                )
                for result in results
            )
            summaries.append(
                {
                    "concurrency": level,
                    "requests": request_count,
                    "elapsed_seconds": elapsed,
                    "requests_per_second": request_count / elapsed,
                    "completion_tokens_per_second": completion_tokens / elapsed,
                    "latency_p50_seconds": percentile(latencies, 0.50),
                    "latency_p95_seconds": percentile(latencies, 0.95),
                    "errors": len(errors),
                    "error_examples": [asdict(error) for error in errors[:3]],
                }
            )

    print(json.dumps(summaries, indent=2))
    return 1 if failed else 0


def parser() -> argparse.ArgumentParser:
    arg_parser = argparse.ArgumentParser(description=__doc__)
    arg_parser.add_argument("images", nargs="+", help="Representative image paths or URLs")
    arg_parser.add_argument("--base-url", required=True)
    arg_parser.add_argument("--levels", type=parse_levels, default=parse_levels("8,16,32,64"))
    arg_parser.add_argument("--requests-per-level", type=int)
    arg_parser.add_argument("--timeout", type=float, default=900.0)
    arg_parser.add_argument("--no-warmup", action="store_true")
    arg_parser.add_argument("--token", default=os.getenv("DOTS_MOCR_TOKEN"))
    return arg_parser


def main() -> int:
    return asyncio.run(async_main(parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
