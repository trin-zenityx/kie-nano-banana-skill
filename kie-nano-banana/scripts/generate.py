#!/usr/bin/env python3
"""Generate images with Google Nano Banana (Pro or 2) via KIE.ai.

Pipeline:
  1. Auto-upload any local reference images to KIE's temporary storage
     (the generation endpoint only accepts URLs).
  2. POST /api/v1/jobs/createTask to kick off the generation.
  3. Poll GET /api/v1/jobs/recordInfo until state == "success" or "fail".
  4. Download the resulting images locally before the remote URLs expire
     (~24h after creation).

Stderr carries human progress messages. Stdout carries a single JSON summary
on success, so callers can parse the task id, remote URLs, and local paths.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

KIE_API_BASE = "https://api.kie.ai/api/v1"
KIE_UPLOAD_BASE = "https://kieai.redpandaai.co/api"

MODEL_CONSTRAINTS = {
    "nano-banana-pro": {
        "max_refs": 8,
        "aspect_ratios": {
            "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4",
            "9:16", "16:9", "21:9", "auto",
        },
        "default_output_format": "png",
        "prompt_max": 10_000,
    },
    "nano-banana-2": {
        "max_refs": 14,
        "aspect_ratios": {
            "1:1", "16:9", "9:16", "4:3", "3:4", "21:9", "auto",
        },
        "default_output_format": "jpg",
        "prompt_max": 20_000,
    },
}


def log(msg: str) -> None:
    """Write a human-readable progress message to stderr."""
    print(msg, file=sys.stderr, flush=True)


def _parse_env_file(path: Path) -> dict:
    """Minimal dotenv parser — no external dependency.

    Handles `KEY=value`, ignores blank lines and `#` comments, strips
    surrounding single or double quotes from values. Anything more exotic
    (multi-line values, interpolation, export prefixes) is out of scope.
    """
    out: dict = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (
            v.startswith("'") and v.endswith("'")
        ):
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def load_api_key() -> str:
    """Find KIE_API_KEY from several places, in priority order.

    1. ``KIE_API_KEY`` environment variable — standard 12-factor.
    2. ``./kie.env`` in the current working directory — project-local.
       (We deliberately do NOT read a generic ``./.env`` because that file
       usually belongs to someone else's project and we don't want to
       clash with or accidentally expose its contents.)
    3. ``~/.kie/.env`` — user-global config directory.
    4. ``~/.kie_api_key`` — a bare one-line file containing only the key.

    The env var always wins so CI/automation can override file-based config.
    """
    key = os.environ.get("KIE_API_KEY")
    if key and key.strip():
        return key.strip()

    env_files = [
        Path.cwd() / "kie.env",
        Path.home() / ".kie" / ".env",
    ]
    for path in env_files:
        if path.is_file():
            parsed = _parse_env_file(path)
            val = parsed.get("KIE_API_KEY", "").strip()
            if val:
                log(f"  (loaded KIE_API_KEY from {path})")
                return val

    bare = Path.home() / ".kie_api_key"
    if bare.is_file():
        val = bare.read_text().strip()
        if val:
            log(f"  (loaded KIE_API_KEY from {bare})")
            return val

    log("ERROR: KIE_API_KEY not found.")
    log("Set it one of these ways (whichever is convenient):")
    log('  1. export KIE_API_KEY="..."                              # env var')
    log('  2. echo "KIE_API_KEY=..." > ./kie.env                     # project-local')
    log('  3. mkdir -p ~/.kie && echo "KIE_API_KEY=..." > ~/.kie/.env  # user-global')
    log('  4. echo "..." > ~/.kie_api_key && chmod 600 ~/.kie_api_key  # bare key file')
    log("Get a key at https://kie.ai/api-key")
    sys.exit(2)


# Backwards-compatible alias — earlier versions of this script called it
# get_api_key().  Anything still importing the old name keeps working.
get_api_key = load_api_key


def is_url(s: str) -> bool:
    try:
        parsed = urlparse(s)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def upload_local_file(path: Path, api_key: str) -> str:
    """Upload one local file via the stream endpoint, return its public URL."""
    log(f"  uploading {path.name} to KIE temp storage...")
    url = f"{KIE_UPLOAD_BASE}/file-stream-upload"
    headers = {"Authorization": f"Bearer {api_key}"}
    with open(path, "rb") as f:
        files = {
            "file": (path.name, f),
            "uploadPath": (None, "images/nano-banana-refs"),
        }
        r = requests.post(url, headers=headers, files=files, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(
            f"Upload failed: HTTP {r.status_code} — {r.text[:500]}"
        )
    data = r.json()
    payload = data.get("data") or {}
    file_url = payload.get("fileUrl") or payload.get("downloadUrl")
    if not file_url:
        raise RuntimeError(f"Upload response missing fileUrl: {data}")
    log(f"  uploaded -> {file_url}")
    return file_url


def resolve_references(
    refs: list[str], api_key: str, max_refs: int
) -> list[str]:
    if not refs:
        return []
    if len(refs) > max_refs:
        log(
            f"ERROR: this model accepts at most {max_refs} reference images "
            f"(got {len(refs)})."
        )
        sys.exit(2)
    resolved: list[str] = []
    for ref in refs:
        if is_url(ref):
            resolved.append(ref)
            continue
        p = Path(ref).expanduser()
        if not p.is_file():
            log(f"ERROR: reference image not found: {ref}")
            sys.exit(2)
        resolved.append(upload_local_file(p, api_key))
    return resolved


def create_task(
    api_key: str,
    model: str,
    prompt: str,
    image_urls: list[str],
    aspect_ratio: str | None,
    resolution: str | None,
    output_format: str | None,
) -> str:
    url = f"{KIE_API_BASE}/jobs/createTask"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    input_obj: dict = {"prompt": prompt}
    if image_urls:
        input_obj["image_input"] = image_urls
    if aspect_ratio:
        input_obj["aspect_ratio"] = aspect_ratio
    if resolution:
        input_obj["resolution"] = resolution
    if output_format:
        input_obj["output_format"] = output_format

    body = {"model": model, "input": input_obj}
    r = requests.post(url, headers=headers, json=body, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(
            f"createTask failed: HTTP {r.status_code} — {r.text[:500]}"
        )
    data = r.json()
    if data.get("code") != 200:
        raise RuntimeError(f"createTask error: {data}")
    task_id = data["data"]["taskId"]
    log(f"task created: {task_id}")
    return task_id


def poll_task(
    api_key: str, task_id: str, timeout: int, poll_interval: int
) -> list[str]:
    url = f"{KIE_API_BASE}/jobs/recordInfo"
    headers = {"Authorization": f"Bearer {api_key}"}
    deadline = time.time() + timeout
    last_state: str | None = None

    while time.time() < deadline:
        try:
            r = requests.get(
                url, headers=headers, params={"taskId": task_id}, timeout=30
            )
        except requests.RequestException as exc:
            log(f"  (poll network error: {exc}, retrying)")
            time.sleep(poll_interval)
            continue

        if r.status_code != 200:
            log(f"  (poll got HTTP {r.status_code}, retrying)")
            time.sleep(poll_interval)
            continue

        data = r.json().get("data", {}) or {}
        state = data.get("state")
        if state != last_state:
            log(f"  state: {state}")
            last_state = state

        if state == "success":
            result_json = data.get("resultJson") or "{}"
            try:
                result = json.loads(result_json)
            except json.JSONDecodeError:
                result = {}
            urls = result.get("resultUrls") or []
            if not urls:
                raise RuntimeError(
                    f"task succeeded but no result URLs found. "
                    f"resultJson={result_json[:500]}"
                )
            return urls

        if state == "fail":
            raise RuntimeError(
                f"task failed: {data.get('failCode') or ''} "
                f"{data.get('failMsg') or ''}".strip()
            )

        time.sleep(poll_interval)

    raise TimeoutError(
        f"task {task_id} did not finish within {timeout}s; "
        f"last state was {last_state!r}"
    )


def download_images(
    urls: list[str], output_dir: str, model: str, task_id: str
) -> list[Path]:
    out = Path(output_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    short_task = task_id.split("_")[-1]
    for i, url in enumerate(urls):
        parsed = urlparse(url)
        ext = Path(parsed.path).suffix or ".png"
        filename = f"{model}_{short_task}_{i+1}{ext}"
        dest = out / filename
        log(f"  downloading {url} -> {dest}")
        r = requests.get(url, timeout=180, stream=True)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
        saved.append(dest)
    return saved


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Generate images with Google Nano Banana (Pro or 2) via KIE.ai."
        ),
    )
    parser.add_argument("prompt", help="text prompt describing the image")
    parser.add_argument(
        "--model",
        choices=list(MODEL_CONSTRAINTS),
        default="nano-banana-pro",
        help="which Nano Banana model to use (default: nano-banana-pro)",
    )
    parser.add_argument(
        "--reference",
        action="append",
        default=[],
        metavar="URL_OR_PATH",
        help=(
            "reference image, URL or local file path; "
            "repeat the flag to pass multiple (up to model limit)"
        ),
    )
    parser.add_argument(
        "--aspect-ratio",
        default=None,
        help="e.g. 1:1, 16:9, 9:16 — see references/api.md for per-model list",
    )
    parser.add_argument(
        "--resolution",
        choices=["1K", "2K", "4K"],
        default=None,
    )
    parser.add_argument(
        "--output-format",
        choices=["png", "jpg"],
        default=None,
    )
    parser.add_argument(
        "--output-dir",
        default="./kie-output",
        help="where to save generated images (default: ./kie-output)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="polling timeout in seconds (default: 600)",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=5,
        help="seconds between polls (default: 5)",
    )
    args = parser.parse_args()

    constraints = MODEL_CONSTRAINTS[args.model]

    if len(args.prompt) > constraints["prompt_max"]:
        log(
            f"ERROR: prompt length {len(args.prompt)} exceeds "
            f"{constraints['prompt_max']}-char limit for {args.model}"
        )
        sys.exit(2)

    if args.aspect_ratio and args.aspect_ratio not in constraints["aspect_ratios"]:
        log(
            f"ERROR: aspect_ratio {args.aspect_ratio!r} not supported by "
            f"{args.model}."
        )
        log(f"  allowed: {sorted(constraints['aspect_ratios'])}")
        sys.exit(2)

    api_key = get_api_key()

    image_urls = resolve_references(
        args.reference, api_key, constraints["max_refs"]
    )

    task_id = create_task(
        api_key,
        args.model,
        args.prompt,
        image_urls,
        args.aspect_ratio,
        args.resolution,
        args.output_format,
    )
    log(f"polling every {args.poll_interval}s (timeout {args.timeout}s)...")
    urls = poll_task(api_key, task_id, args.timeout, args.poll_interval)
    log(f"{len(urls)} image(s) generated.")

    saved = download_images(urls, args.output_dir, args.model, task_id)
    log(f"saved {len(saved)} file(s) to {args.output_dir}")

    summary = {
        "taskId": task_id,
        "model": args.model,
        "prompt": args.prompt,
        "remote_urls": urls,
        "local_files": [str(s) for s in saved],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, TimeoutError) as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
