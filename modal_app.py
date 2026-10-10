"""Modal deployment for the dots.mocr layout parsing API."""

import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import modal

# Modal imports this file in every container, including the slim download
# image, so it cannot import layout_api. tests/test_layout_api.py checks that
# these shared values match.
MODEL_ID = "dots-studio/dots.mocr"
MODEL_REVISION = "e539fbb52280393adc081b289ec597430a0f9031"
VLLM_VERSION = "0.27.1"
MODEL_VOLUME_PATH = Path("/models")
MODEL_PATH = MODEL_VOLUME_PATH / f"DotsMOCR-{MODEL_REVISION[:8]}"
VLLM_CACHE_PATH = Path("/root/.cache/vllm")
VLLM_PORT = 8000

MAX_INPUTS = 32
MAX_MODEL_LEN = 32_768
MINUTES = 60

VLLM_ENV = {
    "HF_HUB_OFFLINE": "1",
    # A Modal memory snapshot invalidates the TCPStore socket retained by
    # PyTorch's NCCL heartbeat thread. In PyTorch 2.10, disabling monitoring
    # only disables process termination; TORCH_NCCL_DUMP_ON_TIMEOUT separately
    # controls the once-per-second TCPStore poll. This deployment has one rank,
    # so cross-rank dump coordination provides no value. NCCL itself remains
    # enabled for vLLM.
    "TORCH_NCCL_DUMP_ON_TIMEOUT": "0",
    "TORCH_NCCL_ENABLE_MONITORING": "0",
    "TORCHINDUCTOR_COMPILE_THREADS": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "VLLM_CACHE_ROOT": str(VLLM_CACHE_PATH),
    "VLLM_SERVER_DEV_MODE": "1",
}

app = modal.App("dots-mocr-serverless")
model_volume = modal.Volume.from_name("dots-mocr-model-cache", create_if_missing=True)
vllm_cache_volume = modal.Volume.from_name(
    "dots-mocr-vllm-cache", create_if_missing=True
)

download_image = modal.Image.debian_slim(python_version="3.12").uv_pip_install(
    "huggingface_hub==0.36.0",
    "hf-xet>=1.1,<2",
)

vllm_image = (
    modal.Image.from_registry(f"vllm/vllm-openai:v{VLLM_VERSION}", add_python="3.12")
    .entrypoint([])
    # Modal's added Python is separate from the vLLM image environment, so the
    # custom API runtime needs its own explicit, reproducible dependency set.
    .uv_pip_install(
        # Match the FastAPI/Starlette generation bundled with vLLM 0.27.1 so
        # the added application runtime cannot shadow it incompatibly.
        "fastapi==0.136.3",
        "httpx==0.28.1",
        "pillow==12.3.0",
        "python-multipart==0.0.32",
    )
    .env(VLLM_ENV)
    .add_local_file(
        Path(__file__).parent / "assets/warmup.jpg",
        "/warmup.jpg",
        copy=True,
    )
    .add_local_python_source("layout_api")
)


def build_vllm_command() -> list[str]:
    """Build the pinned vLLM command used by every GPU replica."""
    return [
        "vllm",
        "serve",
        str(MODEL_PATH),
        "--host", "127.0.0.1",
        "--port", str(VLLM_PORT),
        "--served-model-name", MODEL_ID,
        "--tensor-parallel-size", "1",
        "--dtype", "bfloat16",
        "--gpu-memory-utilization", "0.9",
        "--max-model-len", str(MAX_MODEL_LEN),
        "--max-num-seqs", str(MAX_INPUTS),
        "--limit-mm-per-prompt", json.dumps({"image": 1, "video": 0}),
        "--chat-template-content-format", "string",
        "--trust-remote-code",
        "--async-scheduling",
        "--compilation-config", json.dumps({"compile_mm_encoder": True}),
        "--enable-sleep-mode",
    ]


def wait_for_vllm(process: subprocess.Popen[bytes], timeout_seconds: float) -> None:
    """Wait until private vLLM is ready before exposing the public API."""
    deadline = time.monotonic() + timeout_seconds
    health_url = f"http://127.0.0.1:{VLLM_PORT}/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"vLLM exited during startup with code {process.returncode}")
        try:
            with urllib.request.urlopen(health_url, timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(1)
    process.terminate()
    raise TimeoutError(f"vLLM did not become ready within {timeout_seconds:g} seconds")


def post_vllm(path: str, *, payload: dict[str, object] | None = None) -> None:
    """Call a private vLLM lifecycle or warm-up endpoint."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{VLLM_PORT}{path}",
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10 * MINUTES) as response:
        if response.status != 200:
            raise RuntimeError(f"vLLM {path} returned HTTP {response.status}")


@app.function(
    image=download_image,
    volumes={MODEL_VOLUME_PATH: model_volume},
    cpu=4.0,
    memory=8 * 1024,
    timeout=60 * MINUTES,
)
def download_model() -> dict[str, str | bool]:
    """Idempotently download the exact Hugging Face revision to the model Volume."""
    from huggingface_hub import snapshot_download

    marker = MODEL_PATH / ".revision.json"
    config = MODEL_PATH / "config.json"
    expected = {"model_id": MODEL_ID, "revision": MODEL_REVISION}

    if marker.exists() and config.exists():
        try:
            if json.loads(marker.read_text(encoding="utf-8")) == expected:
                return {"downloaded": False, **expected, "path": str(MODEL_PATH)}
        except (json.JSONDecodeError, OSError):
            pass

    MODEL_PATH.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=MODEL_ID,
        revision=MODEL_REVISION,
        local_dir=MODEL_PATH,
    )
    marker.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    model_volume.commit()
    return {"downloaded": True, **expected, "path": str(MODEL_PATH)}


@app.cls(
    image=vllm_image,
    secrets=[modal.Secret.from_name("dots-mocr-api")],
    gpu="L40S",
    cpu=8.0,
    memory=24 * 1024,
    min_containers=0,
    max_containers=2,
    buffer_containers=0,
    scaledown_window=30,
    timeout=15 * MINUTES,
    startup_timeout=30 * MINUTES,
    enable_memory_snapshot=True,
    experimental_options={"enable_gpu_snapshot": True},
    volumes={
        MODEL_VOLUME_PATH: model_volume,
        VLLM_CACHE_PATH: vllm_cache_volume,
    },
)
@modal.concurrent(max_inputs=MAX_INPUTS, target_inputs=32)
class LayoutApiServer:
    """Snapshot-aware private vLLM with a public ASGI layout API."""

    @modal.enter(snap=True)
    def prepare_snapshot(self) -> None:
        from layout_api import prepare_image

        if not (MODEL_PATH / "config.json").exists():
            raise RuntimeError(
                f"Model weights are missing at {MODEL_PATH}. "
                "Run `uv run modal run modal_app.py::download_model` before deploying."
            )

        self.vllm_process = subprocess.Popen(build_vllm_command())
        wait_for_vllm(self.vllm_process, timeout_seconds=25 * MINUTES)
        _, payload = prepare_image(Path("/warmup.jpg").read_bytes())
        payload["max_completion_tokens"] = 16
        for _ in range(3):
            post_vllm("/v1/chat/completions", payload=payload)
        post_vllm("/sleep?level=1")

    @modal.enter(snap=False)
    def restore_snapshot(self) -> None:
        post_vllm("/wake_up")
        wait_for_vllm(self.vllm_process, timeout_seconds=5 * MINUTES)

    @modal.asgi_app(label="api", requires_proxy_auth=False)
    def serve(self):
        from layout_api import create_app

        return create_app()

    @modal.exit()
    def stop(self) -> None:
        process = getattr(self, "vllm_process", None)
        if process is not None and process.poll() is None:
            process.terminate()
