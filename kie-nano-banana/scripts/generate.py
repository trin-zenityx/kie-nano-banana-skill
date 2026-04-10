#!/usr/bin/env python3
"""Generate images with Google Nano Banana (Pro or 2) via KIE.ai.

The KIE generation endpoint is async — createTask returns a taskId, and
the actual image is produced later. This script offers two ways to drive
that lifecycle, pick whichever fits the caller:

1. Legacy all-in-one mode (shell-friendly)
       generate.py "prompt..." --model nano-banana-pro --aspect-ratio 16:9
   Creates, polls, and downloads in one process. Simple, but the caller
   has to keep the process alive for the full generation window, which
   can be several minutes. Claude Code's Bash tool kills long-running
   processes at its own timeout, so this mode is fragile under an agent.

2. Subcommand mode (agent-friendly, resumable)
       generate.py submit "prompt..." --model nano-banana-pro
           → creates task, writes a state file at ~/.kie/tasks/<id>.json,
             returns immediately (< 5 s) with the taskId.
       generate.py wait <taskId>
           → polls until success (or Bash timeout), downloads on success,
             updates the state file. Exits with code 3 if the task is
             still running when the caller-side timeout fires, so the
             caller can simply re-run `wait` to resume without losing
             credits.
       generate.py status <taskId>
           → one quick poll, prints current state, exits < 2 s.
       generate.py fetch <taskId>
           → for tasks KIE already finished: pulls resultUrls and
             downloads them. Idempotent.

The state file at ~/.kie/tasks/<id>.json stores enough metadata to
resume a task after the caller process dies, restarts, or gets killed
mid-poll. Nothing important is kept in memory.

Stderr is human progress; stdout is a single JSON summary for machines.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

KIE_API_BASE = "https://api.kie.ai/api/v1"
KIE_UPLOAD_BASE = "https://kieai.redpandaai.co/api"

# Where we persist per-task state so `wait` / `status` / `fetch` can
# resume without the caller having to pass the original prompt / model
# back in every time.
STATE_DIR = Path.home() / ".kie" / "tasks"

# Exit codes with meaning. 0 = done; 1 = hard failure; 2 = usage /
# validation problem; 3 = "still generating, resume me" (not an error).
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_IN_PROGRESS = 3

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

SUBCOMMANDS = {"submit", "wait", "status", "fetch"}


# ----------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------


def log(msg: str) -> None:
    """Write a human-readable progress message to stderr."""
    print(msg, file=sys.stderr, flush=True)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_url(s: str) -> bool:
    try:
        parsed = urlparse(s)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def _parse_env_file(path: Path) -> dict:
    """Minimal dotenv parser — no external dependency.

    Handles ``KEY=value``, ignores blank lines and ``#`` comments, strips
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
    sys.exit(EXIT_USAGE)


# Backwards-compatible alias — earlier versions of this script called it
# get_api_key(). Anything still importing the old name keeps working.
get_api_key = load_api_key


# ----------------------------------------------------------------------
# state file — ~/.kie/tasks/<taskId>.json
# ----------------------------------------------------------------------


def state_path(task_id: str) -> Path:
    return STATE_DIR / f"{task_id}.json"


def load_state(task_id: str) -> dict:
    """Load the state file for a task. Returns an empty dict if missing.

    A missing state file is NOT an error — it just means the task was
    created by a different process or an older version of this script.
    In that case callers should still be able to `wait` / `fetch` using
    just the taskId; state file is a convenience, not a requirement.
    """
    p = state_path(task_id)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log(f"  (warning: could not parse state file {p}: {exc})")
        return {}


def save_state(task_id: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = state_path(task_id)
    state["updated_at"] = now_iso()
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ----------------------------------------------------------------------
# KIE API calls
# ----------------------------------------------------------------------


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
        sys.exit(EXIT_USAGE)
    resolved: list[str] = []
    for ref in refs:
        if is_url(ref):
            resolved.append(ref)
            continue
        p = Path(ref).expanduser()
        if not p.is_file():
            log(f"ERROR: reference image not found: {ref}")
            sys.exit(EXIT_USAGE)
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


def fetch_task_info(api_key: str, task_id: str) -> dict:
    """Return the raw ``data`` dict from one /recordInfo call.

    Raises ``RuntimeError`` if the HTTP layer itself failed. A running
    task is a normal return (state in waiting/queuing/generating).
    """
    url = f"{KIE_API_BASE}/jobs/recordInfo"
    headers = {"Authorization": f"Bearer {api_key}"}
    r = requests.get(
        url, headers=headers, params={"taskId": task_id}, timeout=30
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"recordInfo failed: HTTP {r.status_code} — {r.text[:500]}"
        )
    return r.json().get("data", {}) or {}


def extract_result_urls(task_info: dict) -> list[str]:
    """Pull ``resultUrls`` out of ``resultJson`` (which is a string)."""
    raw = task_info.get("resultJson") or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {}
    return parsed.get("resultUrls") or []


def poll_until_terminal(
    api_key: str,
    task_id: str,
    timeout: int,
    poll_interval: int,
) -> tuple[str, dict]:
    """Poll recordInfo until state is ``success``/``fail`` or timeout.

    Returns a tuple ``(state, task_info)``. If polling hits the timeout
    before the task finishes, state is the last observed non-terminal
    value (e.g. ``"generating"``) — the caller decides whether to treat
    that as a resumable "in progress" or a hard failure.
    """
    deadline = time.time() + timeout
    last_state: str | None = None
    last_info: dict = {}

    while time.time() < deadline:
        try:
            info = fetch_task_info(api_key, task_id)
        except requests.RequestException as exc:
            log(f"  (poll network error: {exc}, retrying)")
            time.sleep(poll_interval)
            continue
        except RuntimeError as exc:
            log(f"  (poll {exc}, retrying)")
            time.sleep(poll_interval)
            continue

        last_info = info
        state = info.get("state")
        if state != last_state:
            log(f"  state: {state}")
            last_state = state

        if state in ("success", "fail"):
            return state, info

        time.sleep(poll_interval)

    return last_state or "unknown", last_info


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


# ----------------------------------------------------------------------
# subcommand implementations
# ----------------------------------------------------------------------


def validate_prompt_and_ratio(
    prompt: str, model: str, aspect_ratio: str | None
) -> None:
    constraints = MODEL_CONSTRAINTS[model]
    if len(prompt) > constraints["prompt_max"]:
        log(
            f"ERROR: prompt length {len(prompt)} exceeds "
            f"{constraints['prompt_max']}-char limit for {model}"
        )
        sys.exit(EXIT_USAGE)
    if aspect_ratio and aspect_ratio not in constraints["aspect_ratios"]:
        log(
            f"ERROR: aspect_ratio {aspect_ratio!r} not supported by {model}."
        )
        log(f"  allowed: {sorted(constraints['aspect_ratios'])}")
        sys.exit(EXIT_USAGE)


def cmd_submit(args: argparse.Namespace) -> int:
    """Create the task, persist state, exit fast. No polling."""
    validate_prompt_and_ratio(args.prompt, args.model, args.aspect_ratio)

    api_key = load_api_key()
    constraints = MODEL_CONSTRAINTS[args.model]

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

    state = {
        "taskId": task_id,
        "model": args.model,
        "prompt": args.prompt,
        "aspect_ratio": args.aspect_ratio,
        "resolution": args.resolution,
        "output_format": args.output_format,
        "image_urls": image_urls,
        "output_dir": str(Path(args.output_dir).expanduser().resolve()),
        "state": "submitted",
        "remote_urls": [],
        "local_files": [],
        "created_at": now_iso(),
    }
    save_state(task_id, state)
    log(f"state file: {state_path(task_id)}")

    summary = {
        "taskId": task_id,
        "model": args.model,
        "state": "submitted",
        "state_file": str(state_path(task_id)),
        "next": (
            f"python3 {Path(__file__).name} wait {task_id}"
        ),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return EXIT_OK


def _download_and_finalize(
    task_id: str,
    remote_urls: list[str],
    state: dict,
) -> dict:
    """Shared path: download remote URLs, update state, return summary dict."""
    model = state.get("model") or "nano-banana-pro"
    output_dir = state.get("output_dir") or str(
        (Path.cwd() / "kie-output").resolve()
    )
    saved = download_images(remote_urls, output_dir, model, task_id)
    log(f"saved {len(saved)} file(s) to {output_dir}")

    state.update({
        "state": "downloaded",
        "remote_urls": remote_urls,
        "local_files": [str(s) for s in saved],
    })
    save_state(task_id, state)

    return {
        "taskId": task_id,
        "model": model,
        "prompt": state.get("prompt"),
        "state": "downloaded",
        "remote_urls": remote_urls,
        "local_files": [str(s) for s in saved],
        "state_file": str(state_path(task_id)),
    }


def cmd_wait(args: argparse.Namespace) -> int:
    """Block-poll a task, then download. Resumable."""
    task_id = args.task_id
    api_key = load_api_key()
    state = load_state(task_id)

    # Already downloaded? Nothing to do — print the summary and succeed.
    if state.get("state") == "downloaded" and state.get("local_files"):
        log(f"task {task_id} is already downloaded; nothing to do.")
        print(json.dumps({
            "taskId": task_id,
            "model": state.get("model"),
            "prompt": state.get("prompt"),
            "state": "downloaded",
            "remote_urls": state.get("remote_urls", []),
            "local_files": state.get("local_files", []),
            "state_file": str(state_path(task_id)),
        }, indent=2, ensure_ascii=False))
        return EXIT_OK

    log(
        f"polling {task_id} every {args.poll_interval}s "
        f"(timeout {args.timeout}s)..."
    )
    final_state, info = poll_until_terminal(
        api_key, task_id, args.timeout, args.poll_interval
    )

    if final_state == "success":
        urls = extract_result_urls(info)
        if not urls:
            raise RuntimeError(
                f"task {task_id} succeeded but no resultUrls — "
                f"resultJson={info.get('resultJson', '')[:500]}"
            )
        state.setdefault("taskId", task_id)
        state.setdefault("model", info.get("model") or "nano-banana-pro")
        summary = _download_and_finalize(task_id, urls, state)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return EXIT_OK

    if final_state == "fail":
        msg = (
            f"{info.get('failCode') or ''} {info.get('failMsg') or ''}"
        ).strip()
        state.update({"state": "fail", "error": msg})
        save_state(task_id, state)
        log(f"ERROR: task {task_id} failed: {msg}")
        print(json.dumps({
            "taskId": task_id,
            "state": "fail",
            "error": msg,
        }, indent=2, ensure_ascii=False))
        return EXIT_FAIL

    # Timed out mid-generation. This is NOT a failure — KIE is still
    # working on it. The caller can re-run `wait` to resume.
    state.update({"state": final_state or "generating"})
    save_state(task_id, state)
    log(
        f"wait timed out after {args.timeout}s; task is still {final_state!r}. "
        f"Re-run `generate.py wait {task_id}` to resume — no credits lost."
    )
    print(json.dumps({
        "taskId": task_id,
        "state": final_state,
        "resumable": True,
        "hint": f"python3 {Path(__file__).name} wait {task_id}",
    }, indent=2, ensure_ascii=False))
    return EXIT_IN_PROGRESS


def cmd_status(args: argparse.Namespace) -> int:
    """Single /recordInfo poll + print. No download, no block."""
    task_id = args.task_id
    api_key = load_api_key()
    state = load_state(task_id)
    info = fetch_task_info(api_key, task_id)
    kie_state = info.get("state") or "unknown"
    urls = extract_result_urls(info) if kie_state == "success" else []

    if state:
        state.update({"state": kie_state})
        if urls:
            state["remote_urls"] = urls
        save_state(task_id, state)

    print(json.dumps({
        "taskId": task_id,
        "state": kie_state,
        "remote_urls": urls,
        "local_files": state.get("local_files", []),
        "failCode": info.get("failCode") or "",
        "failMsg": info.get("failMsg") or "",
    }, indent=2, ensure_ascii=False))
    return EXIT_OK


def cmd_fetch(args: argparse.Namespace) -> int:
    """Download a task that KIE has already finished."""
    task_id = args.task_id
    api_key = load_api_key()
    state = load_state(task_id)
    info = fetch_task_info(api_key, task_id)
    kie_state = info.get("state")

    if kie_state != "success":
        log(
            f"task {task_id} is not in success state (state={kie_state!r}); "
            f"use `wait` if it's still generating, or check `status`."
        )
        return EXIT_FAIL

    urls = extract_result_urls(info)
    if not urls:
        raise RuntimeError(
            f"task {task_id} is success but has no resultUrls — "
            f"resultJson={info.get('resultJson', '')[:500]}"
        )

    # If we have no state file, seed one with what we can learn from
    # /recordInfo so the download step has an output dir + model.
    if not state:
        state = {
            "taskId": task_id,
            "model": info.get("model") or "nano-banana-pro",
            "prompt": "",
            "output_dir": str((Path.cwd() / "kie-output").resolve()),
            "created_at": now_iso(),
        }

    summary = _download_and_finalize(task_id, urls, state)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return EXIT_OK


def cmd_legacy(args: argparse.Namespace) -> int:
    """All-in-one: submit + wait + download. Shell-friendly, not agent-safe.

    Kept for backwards compatibility and for direct CLI use. Agents
    should prefer the submit + wait split so a Bash timeout at the
    caller level doesn't lose work.
    """
    validate_prompt_and_ratio(args.prompt, args.model, args.aspect_ratio)

    api_key = load_api_key()
    constraints = MODEL_CONSTRAINTS[args.model]

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

    state = {
        "taskId": task_id,
        "model": args.model,
        "prompt": args.prompt,
        "aspect_ratio": args.aspect_ratio,
        "resolution": args.resolution,
        "output_format": args.output_format,
        "image_urls": image_urls,
        "output_dir": str(Path(args.output_dir).expanduser().resolve()),
        "state": "submitted",
        "remote_urls": [],
        "local_files": [],
        "created_at": now_iso(),
    }
    save_state(task_id, state)

    log(f"polling every {args.poll_interval}s (timeout {args.timeout}s)...")
    final_state, info = poll_until_terminal(
        api_key, task_id, args.timeout, args.poll_interval
    )

    if final_state != "success":
        if final_state == "fail":
            msg = (
                f"{info.get('failCode') or ''} {info.get('failMsg') or ''}"
            ).strip()
            raise RuntimeError(f"task {task_id} failed: {msg}")
        raise TimeoutError(
            f"task {task_id} did not finish within {args.timeout}s; "
            f"last state was {final_state!r}. "
            f"Resume with: python3 {Path(__file__).name} wait {task_id}"
        )

    urls = extract_result_urls(info)
    if not urls:
        raise RuntimeError(
            f"task {task_id} succeeded but no resultUrls — "
            f"resultJson={info.get('resultJson', '')[:500]}"
        )

    summary = _download_and_finalize(task_id, urls, state)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return EXIT_OK


# ----------------------------------------------------------------------
# argparse plumbing
# ----------------------------------------------------------------------


def _add_generation_flags(p: argparse.ArgumentParser) -> None:
    """Flags shared between ``submit`` and the legacy all-in-one mode."""
    p.add_argument("prompt", help="text prompt describing the image")
    p.add_argument(
        "--model",
        choices=list(MODEL_CONSTRAINTS),
        default="nano-banana-pro",
        help="which Nano Banana model to use (default: nano-banana-pro)",
    )
    p.add_argument(
        "--reference",
        action="append",
        default=[],
        metavar="URL_OR_PATH",
        help=(
            "reference image, URL or local file path; "
            "repeat the flag to pass multiple (up to model limit)"
        ),
    )
    p.add_argument(
        "--aspect-ratio",
        default=None,
        help="e.g. 1:1, 16:9, 9:16 — see references/api.md for per-model list",
    )
    p.add_argument(
        "--resolution",
        choices=["1K", "2K", "4K"],
        default="4K",
        help="output resolution (default: 4K)",
    )
    p.add_argument(
        "--output-format",
        choices=["png", "jpg"],
        default=None,
    )
    p.add_argument(
        "--output-dir",
        default="./kie-output",
        help="where to save generated images (default: ./kie-output)",
    )


def _add_poll_flags(p: argparse.ArgumentParser, default_timeout: int) -> None:
    p.add_argument(
        "--timeout",
        type=int,
        default=default_timeout,
        help=f"polling timeout in seconds (default: {default_timeout})",
    )
    p.add_argument(
        "--poll-interval",
        type=int,
        default=5,
        help="seconds between polls (default: 5)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generate.py",
        description=(
            "Generate images with Google Nano Banana (Pro or 2) via KIE.ai. "
            "Use subcommands (submit / wait / status / fetch) for the "
            "agent-friendly, resumable workflow — or pass a prompt as the "
            "first positional argument for the legacy all-in-one mode."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    p_submit = sub.add_parser(
        "submit",
        help="create a task and return immediately (no polling)",
        description=(
            "Create a KIE generation task and write a state file to "
            "~/.kie/tasks/<taskId>.json. Returns the taskId. Pair with "
            "`wait <taskId>` to actually get the image."
        ),
    )
    _add_generation_flags(p_submit)
    p_submit.set_defaults(func=cmd_submit)

    p_wait = sub.add_parser(
        "wait",
        help="poll a submitted task until success, then download",
        description=(
            "Poll an existing task until it reaches success (and download "
            "the result) or fail. Safe to call repeatedly — no credits are "
            "spent on retry. Exits with code 3 if polling times out "
            "before the task finishes."
        ),
    )
    p_wait.add_argument("task_id", help="taskId returned by `submit`")
    _add_poll_flags(p_wait, default_timeout=540)  # ~9 min, under Bash ceiling
    p_wait.set_defaults(func=cmd_wait)

    p_status = sub.add_parser(
        "status",
        help="print current state of a task (one quick poll, no download)",
        description=(
            "Hit /recordInfo once and print the current state. Useful for "
            "checking on a task without blocking the caller."
        ),
    )
    p_status.add_argument("task_id")
    p_status.set_defaults(func=cmd_status)

    p_fetch = sub.add_parser(
        "fetch",
        help="download a task that KIE has already finished",
        description=(
            "For tasks already in state=success on KIE: pull the "
            "resultUrls and download them locally. Idempotent. Useful if "
            "a previous `wait` was killed after success but before the "
            "download completed."
        ),
    )
    p_fetch.add_argument("task_id")
    p_fetch.set_defaults(func=cmd_fetch)

    return parser


def build_legacy_parser() -> argparse.ArgumentParser:
    """All-in-one mode — invoked when no subcommand is given."""
    parser = argparse.ArgumentParser(
        prog="generate.py",
        description=(
            "Legacy all-in-one mode: create a task, poll to completion, "
            "and download in a single process. Shell-friendly. Agents "
            "should prefer `submit` + `wait` because Bash timeouts can "
            "kill this mode mid-poll."
        ),
    )
    _add_generation_flags(parser)
    _add_poll_flags(parser, default_timeout=600)
    parser.set_defaults(func=cmd_legacy)
    return parser


def main() -> None:
    argv = sys.argv[1:]
    # Subcommand mode iff the first positional matches a known subcommand.
    # This keeps the legacy `generate.py "prompt..."` form working — a
    # prompt is never going to be literally `submit` / `wait` / etc.
    if argv and argv[0] in SUBCOMMANDS:
        parser = build_parser()
        args = parser.parse_args(argv)
    else:
        parser = build_legacy_parser()
        args = parser.parse_args(argv)

    try:
        rc = args.func(args)
    except (RuntimeError, TimeoutError) as exc:
        log(f"ERROR: {exc}")
        sys.exit(EXIT_FAIL)
    sys.exit(rc or EXIT_OK)


if __name__ == "__main__":
    main()
