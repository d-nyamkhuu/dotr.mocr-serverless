"""Check benchmark scheduling and summaries against a mocked endpoint."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from scripts.benchmark import run_level, summarize


def handler(request: httpx.Request) -> httpx.Response:
    degraded = b"degraded.png" in request.content
    return httpx.Response(200, json={
        "status": "degraded" if degraded else "ok",
        "generation": {"finish_reason": "stop", "usage": {"prompt_tokens": 100, "completion_tokens": 10}},
    })


class BenchmarkTests(unittest.TestCase):
    def test_level_records_and_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            sources = []
            for name in ("one.png", "two.png", "degraded.png"):
                path = Path(directory) / name
                path.write_bytes(b"\x89PNG")
                sources.append(str(path))
            sources.append(str(Path(directory) / "missing.png"))

            async def run():
                async with httpx.AsyncClient(base_url="https://example.test", transport=httpx.MockTransport(handler)) as client:
                    return await run_level(client, sources, 2)

            records = asyncio.run(run())
        self.assertEqual([item["status"] for item in records], ["ok", "ok", "degraded", "error"])
        self.assertTrue(all(item["started_seconds"] >= 0 for item in records))
        summary = summarize(2, records)
        self.assertEqual((summary["ok"], summary["degraded"], summary["errors"]), (2, 1, 1))
        self.assertGreater(summary["completion_tokens_per_second"], 0)
        self.assertIn("FileNotFoundError", summary["error_examples"][0])


if __name__ == "__main__":
    unittest.main()
