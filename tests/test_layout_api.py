"""Layout API checks without Modal or GPU calls."""

import io
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

import layout_api
import modal_app
from layout_api import create_app
from scripts.batch_client import build_client


class LayoutApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.dict(os.environ, {"DOTS_MOCR_TOKEN": "test-token"}):
            self.app = create_app()
        # ASGITransport does not run the lifespan that creates the model client.
        self.app.state.http_client = None
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://ocr.modal.run",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_preflight_allows_any_origin_without_a_token(self):
        for origin in ["https://d-nyamkhuu.github.io", "https://another.example", "http://localhost:5175"]:
            response = await self.client.options("/v1/parse", headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["access-control-allow-origin"], "*")
            self.assertIn("authorization", response.headers["access-control-allow-headers"].lower())
            self.assertNotIn("access-control-allow-credentials", response.headers)

    async def test_missing_wrong_and_legacy_tokens_cannot_reach_inference(self):
        headers = [
            {}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic test-token"},
            {"Authorization": "Bearer"}, {"Modal-Key": "wk-test", "Modal-Secret": "ws-test"},
        ]
        with patch("layout_api.run_inference", new_callable=AsyncMock) as inference:
            for auth in headers:
                response = await self.client.post("/v1/parse", headers={"Origin": "https://any.example", **auth})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
                self.assertEqual(response.headers["access-control-allow-origin"], "*")
            inference.assert_not_awaited()

    async def test_all_http_routes_require_a_token(self):
        for path in ["/health", "/metrics", "/docs", "/openapi.json", "/unknown"]:
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 401)

    async def test_docs_and_schema_are_not_served(self):
        for path in ["/docs", "/redoc", "/openapi.json"]:
            response = await self.client.get(path, headers={"Authorization": "Bearer test-token"})
            self.assertEqual(response.status_code, 404)

    async def test_model_failure_maps_to_bad_gateway(self):
        image = io.BytesIO()
        Image.new("RGB", (72, 144), "white").save(image, format="PNG")
        transport = httpx.MockTransport(lambda request: httpx.Response(500))
        async with httpx.AsyncClient(transport=transport) as model_client:
            self.app.state.http_client = model_client
            response = await self.client.post("/v1/parse", headers={"Authorization": "Bearer test-token"},
                                              files={"file": ("page.png", image.getvalue(), "image/png")})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "The model request failed")

    async def test_private_image_url_is_rejected(self):
        response = await self.client.post("/v1/parse", headers={"Authorization": "Bearer test-token"},
                                          data={"image_url": "https://127.0.0.1/page.png"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("private or reserved", response.json()["detail"])

    async def test_valid_token_reaches_validation_and_errors_have_cors_headers(self):
        response = await self.client.post("/v1/parse", headers={
            "Authorization": "bearer test-token", "Origin": "https://any.example",
        })
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "Provide exactly one of file or image_url")
        self.assertEqual(response.headers["access-control-allow-origin"], "*")

    async def test_authenticated_image_upload_returns_ocr_with_cors(self):
        image = io.BytesIO()
        Image.new("RGB", (72, 144), "white").save(image, format="PNG")
        model_response = {
            "choices": [{"message": {"content": '[{"bbox":[0,0,28,28],"category":"Text","text":"Read text"}]'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        with patch("layout_api.run_inference", new_callable=AsyncMock, return_value=model_response) as inference:
            response = await self.client.post("/v1/parse", headers={
                "Authorization": "Bearer test-token", "Origin": "https://any.example",
            }, files={"file": ("page.png", image.getvalue(), "image/png")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["markdown"], "Read text")
        self.assertEqual(response.headers["access-control-allow-origin"], "*")
        inference.assert_awaited_once()

    async def test_invalid_images_return_client_errors(self):
        headers = {"Authorization": "Bearer test-token"}
        thin = io.BytesIO()
        Image.new("RGB", (1, 300), "white").save(thin, format="PNG")
        gif = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(gif, format="GIF")
        cases = [
            (b"not an image", 422),
            (thin.getvalue(), 422),
            (gif.getvalue(), 415),
            (b"0" * (layout_api.MAX_UPLOAD_BYTES + 1), 413),
        ]
        with patch("layout_api.run_inference", new_callable=AsyncMock) as inference:
            for content, status in cases:
                response = await self.client.post("/v1/parse", headers=headers, files={"file": ("page", content)})
                self.assertEqual(response.status_code, status)
            inference.assert_not_awaited()

    async def test_metrics_reports_unavailable_model_server(self):
        transport = httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("down")))
        async with httpx.AsyncClient(transport=transport) as model_client:
            self.app.state.http_client = model_client
            response = await self.client.get("/metrics", headers={"Authorization": "Bearer test-token"})
        self.assertEqual(response.status_code, 503)

    def test_modal_app_shares_model_settings(self):
        self.assertEqual(modal_app.MODEL_ID, layout_api.MODEL_ID)
        self.assertEqual(modal_app.MAX_MODEL_LEN, layout_api.MAX_MODEL_LEN)
        self.assertEqual(f"http://127.0.0.1:{modal_app.VLLM_PORT}", layout_api.VLLM_BASE_URL)

    def test_missing_configuration_fails_closed(self):
        with patch.dict(os.environ, {"DOTS_MOCR_TOKEN": ""}):
            with self.assertRaisesRegex(RuntimeError, "DOTS_MOCR_TOKEN"):
                create_app()

    async def test_batch_client_uses_bearer_authentication(self):
        async with build_client(base_url="https://ocr.modal.run", token="test-token", timeout=30) as client:
            self.assertEqual(client.headers["Authorization"], "Bearer test-token")
            self.assertNotIn("Modal-Key", client.headers)


if __name__ == "__main__":
    unittest.main()
