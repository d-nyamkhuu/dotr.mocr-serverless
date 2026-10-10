"""Render-ready layout parsing API backed by a private local vLLM server."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import math
import os
import secrets
import socket
import uuid
from contextlib import asynccontextmanager, closing
from io import BytesIO
from typing import Annotated, Any, Literal
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

MODEL_ID = "dots-studio/dots.mocr"
VLLM_PORT = 8000
VLLM_BASE_URL = f"http://127.0.0.1:{VLLM_PORT}"
IMAGE_TOKEN_PREFIX = "<|img|><|imgpad|><|endofimg|>"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MIN_MODEL_PIXELS = 3_136
MAX_MODEL_PIXELS = 11_289_600
IMAGE_FACTOR = 28
MAX_MODEL_LEN = 32_768
MAX_COMPLETION_TOKENS = 24_000
PROMPT_TOKEN_RESERVE = 1_024

logger = logging.getLogger(__name__)

LAYOUT_PROMPT = """Please output the layout information from the PDF image, including each layout element's bbox, its category, and the corresponding text content within the bbox.

1. Bbox format: [x1, y1, x2, y2]

2. Layout Categories: The possible categories are ['Caption', 'Footnote', 'Formula', 'List-item', 'Page-footer', 'Page-header', 'Picture', 'Section-header', 'Table', 'Text', 'Title'].

3. Text Extraction & Formatting Rules:
    - Picture: For the 'Picture' category, the text field should be omitted.
    - Formula: Format its text as LaTeX.
    - Table: Format its text as HTML.
    - All Others (Text, Title, etc.): Format their text as Markdown.

4. Constraints:
    - The output text must be the original text from the image, with no translation.
    - All layout elements must be sorted according to human reading order.

5. Final Output: The entire output must be a single JSON object.
"""


class ImageMetadata(BaseModel):
    original_width: int
    original_height: int
    model_input_width: int
    model_input_height: int


class LayoutElement(BaseModel):
    bbox: tuple[int, int, int, int]
    category: str
    text: str | None = None


class TokenUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class GenerationMetadata(BaseModel):
    finish_reason: str | None = None
    usage: TokenUsage | None = None


class ParseResponse(BaseModel):
    request_id: str
    status: Literal["ok", "degraded"]
    warnings: list[str]
    model: str
    image: ImageMetadata
    elements: list[LayoutElement] | None
    markdown: str
    raw_text: str
    generation: GenerationMetadata


class ImageError(ValueError):
    """An unusable image or image URL, reported to clients with the given HTTP status."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class ModelError(RuntimeError):
    """The private vLLM server failed to answer an inference request."""


def model_input_size(height: int, width: int) -> tuple[int, int]:
    if max(height, width) / min(height, width) > 200:
        raise ImageError(422, "Image aspect ratio must not exceed 200:1")

    resized_height = max(IMAGE_FACTOR, round(height / IMAGE_FACTOR) * IMAGE_FACTOR)
    resized_width = max(IMAGE_FACTOR, round(width / IMAGE_FACTOR) * IMAGE_FACTOR)
    if resized_height * resized_width > MAX_MODEL_PIXELS:
        scale = math.sqrt((height * width) / MAX_MODEL_PIXELS)
        resized_height = max(IMAGE_FACTOR, math.floor(height / scale / IMAGE_FACTOR) * IMAGE_FACTOR)
        resized_width = max(IMAGE_FACTOR, math.floor(width / scale / IMAGE_FACTOR) * IMAGE_FACTOR)
    elif resized_height * resized_width < MIN_MODEL_PIXELS:
        scale = math.sqrt(MIN_MODEL_PIXELS / (height * width))
        resized_height = math.ceil(height * scale / IMAGE_FACTOR) * IMAGE_FACTOR
        resized_width = math.ceil(width * scale / IMAGE_FACTOR) * IMAGE_FACTOR
    return resized_height, resized_width


def image_data_url(image: Image.Image) -> str:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def load_image(data: bytes) -> Image.Image:
    # word_export.pages.load_image is the file-based twin; the server cannot
    # depend on the optional word extra, so the two stay separate.
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageError(413, "Image exceeds the 20 MiB limit")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG"}:
                raise ImageError(415, "Only PNG and JPEG are supported")
            return ImageOps.exif_transpose(image).convert("RGB")
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
        raise ImageError(422, "The supplied content is not a valid PNG or JPEG") from exc


def preprocess_image(image: Image.Image) -> Image.Image:
    """Upscale by 200/96 (96-DPI screen to 200-DPI print) within the model's pixel limits."""
    scale = min(200 / 96, math.sqrt(MAX_MODEL_PIXELS / (image.width * image.height)))
    height, width = model_input_size(math.ceil(image.height * scale), math.ceil(image.width * scale))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def build_vllm_request(model_image: Image.Image) -> dict[str, Any]:
    image_tokens = math.ceil(model_image.width / IMAGE_FACTOR) * math.ceil(
        model_image.height / IMAGE_FACTOR
    )
    completion_tokens = min(MAX_COMPLETION_TOKENS, MAX_MODEL_LEN - image_tokens - PROMPT_TOKEN_RESERVE)
    return {
        "model": MODEL_ID,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": image_data_url(model_image)},
                    },
                    {"type": "text", "text": IMAGE_TOKEN_PREFIX + LAYOUT_PROMPT},
                ],
            }
        ],
        "max_completion_tokens": completion_tokens,
        "temperature": 0.1,
        "top_p": 1.0,
        "stream": False,
    }


def build_inference_request(data: bytes) -> tuple[ImageMetadata, dict[str, Any]]:
    with closing(load_image(data)) as original, closing(preprocess_image(original)) as model_image:
        image = ImageMetadata(
            original_width=original.width,
            original_height=original.height,
            model_input_width=model_image.width,
            model_input_height=model_image.height,
        )
        return image, build_vllm_request(model_image)


async def run_inference(client: httpx.AsyncClient, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = await client.post(
            f"{VLLM_BASE_URL}/v1/chat/completions",
            json=payload,
            timeout=httpx.Timeout(14 * 60, connect=10),
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ModelError("The model request failed") from exc
    return response.json()


def parse_layout_elements(raw_text: str, image: ImageMetadata) -> list[LayoutElement]:
    scale_x = image.original_width / image.model_input_width
    scale_y = image.original_height / image.model_input_height
    payload = json.loads(raw_text)
    if not isinstance(payload, list):
        raise ValueError("Layout output must be a JSON array")
    elements = []
    for item in payload:
        x1, y1, x2, y2 = item["bbox"]
        x1, x2 = sorted(max(0, min(image.original_width, round(x * scale_x))) for x in (x1, x2))
        y1, y2 = sorted(max(0, min(image.original_height, round(y * scale_y))) for y in (y1, y2))
        if x2 <= x1 or y2 <= y1:
            raise ValueError("Layout bounding box has no area")
        elements.append(
            LayoutElement(bbox=(x1, y1, x2, y2), category=item["category"], text=item.get("text"))
        )
    return elements


def formula_markdown(text: str) -> str:
    text = text.strip()
    if not text or (text.startswith("$") and text.endswith("$")):
        return text
    if text.startswith("\\[") and text.endswith("\\]"):
        text = text[2:-2].strip()
    return f"$$\n{text}\n$$"


def elements_to_markdown(elements: list[LayoutElement]) -> str:
    blocks: list[str] = []
    for element in elements:
        if element.category == "Picture":
            continue
        elif element.category == "Formula":
            blocks.append(formula_markdown(element.text or ""))
        else:
            blocks.append((element.text or "").strip())
    return "\n\n".join(block for block in blocks if block)


def build_parse_response(image: ImageMetadata, inference: dict[str, Any]) -> ParseResponse:
    choice = inference["choices"][0]
    raw_text = choice["message"]["content"]
    try:
        elements = parse_layout_elements(raw_text, image)
    except (ValueError, TypeError, KeyError):
        elements = None

    return ParseResponse(
        request_id=uuid.uuid4().hex,
        status="degraded" if elements is None else "ok",
        warnings=["Invalid layout JSON; returning raw text."] if elements is None else [],
        model=MODEL_ID,
        image=image,
        elements=elements,
        markdown=raw_text.strip() if elements is None else elements_to_markdown(elements),
        raw_text=raw_text,
        generation=GenerationMetadata(
            finish_reason=choice.get("finish_reason"), usage=inference.get("usage")
        ),
    )


async def validate_public_https_url(url: str) -> None:
    # httpx resolves the hostname again when connecting, so this check does not
    # defend against DNS rebinding. Use network egress rules for strict isolation.
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ImageError(422, "image_url must be a public HTTPS URL")
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
        )
    except (ValueError, socket.gaierror) as exc:
        raise ImageError(422, "image_url hostname or port is invalid") from exc
    if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
        raise ImageError(422, "image_url must not resolve to a private or reserved address")


async def fetch_image_url(client: httpx.AsyncClient, url: str) -> bytes:
    try:
        for _ in range(4):
            await validate_public_https_url(url)
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes(1024 * 1024):
                    data.extend(chunk)
                    if len(data) > MAX_UPLOAD_BYTES:
                        raise ImageError(413, "Image exceeds the 20 MiB limit")
                return bytes(data)
    except httpx.HTTPError as exc:
        raise ImageError(422, "image_url could not be fetched") from exc
    raise ImageError(422, "image_url redirected too many times")


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


HttpClient = Annotated[httpx.AsyncClient, Depends(get_http_client)]


def create_app() -> FastAPI:
    auth_token = os.environ.get("DOTS_MOCR_TOKEN", "").strip().encode()
    if not auth_token:
        raise RuntimeError("Set DOTS_MOCR_TOKEN before starting the OCR API")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), trust_env=False) as client:
            app.state.http_client = client
            yield

    # Every route requires the bearer token, so the interactive docs and
    # OpenAPI schema are not served.
    app = FastAPI(
        title="dots.mocr Layout API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(ImageError)
    async def image_error(request: Request, exc: ImageError) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(ModelError)
    async def model_error(request: Request, exc: ModelError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    # Authentication is middleware rather than a dependency because FastAPI
    # parses form bodies before resolving dependencies; middleware rejects
    # unauthenticated uploads before any multipart parsing.
    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(token.encode(), auth_token):
            return JSONResponse(
                {"detail": "Invalid or missing bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    # The outer middleware answers preflight before authentication runs and
    # adds CORS headers to authentication failures as well as OCR responses.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
    )

    @app.post("/v1/parse")
    async def parse_layout(
        client: HttpClient,
        file: Annotated[UploadFile | None, File()] = None,
        image_url: Annotated[str | None, Form()] = None,
    ) -> ParseResponse:
        if (file is None) == (image_url is None):
            raise HTTPException(422, "Provide exactly one of file or image_url")
        if file is not None:
            data = await file.read(MAX_UPLOAD_BYTES + 1)
        else:
            data = await fetch_image_url(client, image_url)

        image, payload = await run_in_threadpool(build_inference_request, data)
        del data
        inference = await run_inference(client, payload)
        response = build_parse_response(image, inference)
        if response.status == "degraded":
            logger.warning(
                "Request %s returned invalid layout JSON (finish_reason=%s)",
                response.request_id,
                response.generation.finish_reason,
            )
        return response

    @app.get("/health")
    async def health(client: HttpClient) -> dict[str, str]:
        try:
            response = await client.get(f"{VLLM_BASE_URL}/health")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(503, "The model server is unavailable") from exc
        return {"status": "ok", "model": MODEL_ID}

    @app.get("/metrics")
    async def metrics(client: HttpClient) -> Response:
        try:
            response = await client.get(f"{VLLM_BASE_URL}/metrics")
        except httpx.HTTPError as exc:
            raise HTTPException(503, "The model server is unavailable") from exc
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers={"content-type": response.headers.get("content-type", "text/plain")},
        )

    return app
