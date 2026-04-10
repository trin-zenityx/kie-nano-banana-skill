# KIE Nano Banana — raw API reference

Read this if you need to call the API directly (e.g. to debug a specific field or to use a parameter the bundled script doesn't expose). For normal use, prefer `scripts/generate.py` — it handles the full async + upload + download workflow for you.

## Endpoints at a glance

| Purpose | Method | URL |
|---|---|---|
| Create generation task | `POST` | `https://api.kie.ai/api/v1/jobs/createTask` |
| Get task status/result | `GET`  | `https://api.kie.ai/api/v1/jobs/recordInfo?taskId=...` |
| Upload local file (stream) | `POST` | `https://kieai.redpandaai.co/api/file-stream-upload` |
| Upload by URL | `POST` | `https://kieai.redpandaai.co/api/file-url-upload` |
| Upload by base64 | `POST` | `https://kieai.redpandaai.co/api/file-base64-upload` |

All requests require `Authorization: Bearer <KIE_API_KEY>`.

## Create task — request body

```json
{
  "model": "nano-banana-pro",
  "input": {
    "prompt": "a watercolor painting of a mountain lake at dawn",
    "image_input": ["https://example.com/reference.jpg"],
    "aspect_ratio": "16:9",
    "resolution": "2K",
    "output_format": "png"
  },
  "callBackUrl": "https://optional-webhook.example.com/kie"
}
```

### Per-model constraints

| Field | `nano-banana-pro` | `nano-banana-2` |
|---|---|---|
| `model` literal | `nano-banana-pro` | `nano-banana-2` |
| `input.prompt` max | 10,000 chars | 20,000 chars |
| `input.image_input` count | up to 8 | up to 14 |
| Reference image size (each) | ≤ 30 MB | ≤ 30 MB |
| Reference image formats | JPEG, PNG, WebP | JPEG, PNG, WebP |
| `input.aspect_ratio` allowed | `1:1`, `2:3`, `3:2`, `3:4`, `4:3`, `4:5`, `5:4`, `9:16`, `16:9`, `21:9`, `auto` | `1:1`, `16:9`, `9:16`, `4:3`, `3:4`, `21:9`, `auto` |
| `input.resolution` allowed | `1K`, `2K`, `4K` (API default `1K`; script default `4K`) | `1K`, `2K`, `4K` (API default `1K`; script default `4K`) |
| `input.output_format` allowed | `png` (default), `jpg` | `jpg` (default), `png` |

`input.image_input` items must be publicly fetchable URLs — the generation service pulls them. Local files must be uploaded first (see the file-upload endpoints below).

`callBackUrl` is optional. If set, KIE will POST task-completion notifications there instead of requiring you to poll. The bundled `generate.py` does **not** use webhooks — it uses the subcommand split (`submit` → `wait`) backed by a local state file at `~/.kie/tasks/<taskId>.json`. Webhooks would need a publicly reachable endpoint (and therefore a hosted server or a tunnel like ngrok), which is unrealistic for a CLI tool running on an end-user's laptop. The resumable-poll pattern solves the same "don't lose work when the caller process dies" problem without that constraint.

## Create task — response

```json
{
  "code": 200,
  "msg": "success",
  "data": { "taskId": "task_nano-banana-pro_1734567890" }
}
```

Non-200 `code` or non-200 HTTP status = failure. See the error table at the bottom.

## Get task — response shape

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "taskId": "task_nano-banana-pro_1734567890",
    "model": "nano-banana-pro",
    "state": "success",
    "param": "{\"model\":\"nano-banana-pro\",\"input\":{...}}",
    "resultJson": "{\"resultUrls\":[\"https://tempfile.redpandaai.co/....png\"]}",
    "failCode": "",
    "failMsg": "",
    "costTime": 15000,
    "completeTime": 1734567910000,
    "createTime": 1734567900000,
    "updateTime": 1734567910000
  }
}
```

### State machine

`waiting` → `queuing` → `generating` → `success` **or** `fail`

You are done polling when `state` is either `success` or `fail`. Don't look at `progress` for Nano Banana — that field is only populated for sora2 models.

### Parsing the result

`resultJson` is a **string**, not an object. You must parse it:

```python
import json
result = json.loads(data["resultJson"])   # {"resultUrls": ["https://..."]}
urls = result["resultUrls"]
```

The URLs in `resultUrls` expire approximately 24 hours after generation. Always download them immediately if you want to keep the image. This is not a hint — it's the reason the bundled script has a download step.

### Failure branch

When `state == "fail"`, `failCode` and `failMsg` are populated. Treat `failCode` as the authoritative classifier for retry logic:

- `501` at task level usually means moderation-triggered generation failure. Don't auto-retry the same prompt.
- `500` at task level is usually a transient server issue. One retry is fine.

## File upload — stream

This is what the script uses to make local files available to the generation endpoint.

```
POST https://kieai.redpandaai.co/api/file-stream-upload
Authorization: Bearer <KIE_API_KEY>
Content-Type: multipart/form-data
```

Multipart fields:

| Field | Required | Notes |
|---|---|---|
| `file` | yes | binary file content |
| `uploadPath` | no | directory under the bucket, e.g. `images/nano-banana-refs` |
| `fileName` | no | if omitted, a random name is generated |

Response:

```json
{
  "success": true,
  "code": 200,
  "data": {
    "fileUrl": "https://kieai.redpandaai.co/files/images/nano-banana-refs/my-ref.jpg",
    "downloadUrl": "https://kieai.redpandaai.co/download/file_abc123",
    "fileId": "file_abc123",
    "fileName": "my-ref.jpg",
    "fileSize": 245760,
    "mimeType": "image/jpeg",
    "uploadTime": "2025-01-15T10:30:00Z",
    "expiresAt": "2025-01-18T10:30:00Z"
  }
}
```

Pass `fileUrl` (not `downloadUrl`) as the item inside `input.image_input` when creating the task — the generation backend needs a direct URL, not a redirect.

Uploaded files auto-delete after 3 days.

## File upload — by URL

Use this if you already have a publicly fetchable URL and want to rehost it through KIE (for example, to avoid CORS or access-control issues on the source).

```
POST https://kieai.redpandaai.co/api/file-url-upload
Authorization: Bearer <KIE_API_KEY>
Content-Type: application/json
```

Body:

```json
{ "fileUrl": "https://example.com/some.jpg", "uploadPath": "images/my-refs", "fileName": "optional.jpg" }
```

Response shape is the same as the stream upload. Source fetches time out after 30 seconds and files over ~100 MB are not recommended.

## File upload — by base64

For small files (recommended ≤ 10 MB; base64 inflates size by ~33%). Less useful than the stream endpoint for CLI work, so skip unless you have a reason.

## Error codes

| HTTP | Meaning | Practical response |
|---|---|---|
| 401 | Missing/invalid API key | check `KIE_API_KEY`, regenerate at kie.ai/api-key |
| 402 | Insufficient credits | tell the user to top up — do not retry |
| 404 | Task not found | check the `taskId`; it may have been purged |
| 422 | Validation error | re-read your request body; often bad aspect ratio or unreachable reference URL |
| 429 | Rate limited | exponential backoff |
| 455 | Maintenance | wait and retry later |
| 500 | Server error | one retry is fine |
| 501 | Generation failed | usually moderation — reframe the prompt |
| 505 | Feature disabled | the feature has been turned off on the account |

## Why the script splits into `submit` + `wait`

KIE's `createTask` returns in < 5 s, but `recordInfo` typically needs 30 s – several minutes to reach `state: success`. If a single process owns both halves and that process gets killed mid-poll (Claude Code's Bash tool has a 2-minute default timeout and a 10-minute hard ceiling), the task is still running on KIE's side — but there's nobody left to poll for the result or download the image before the 24 h URL expiry.

The script avoids this by persisting a state file at `~/.kie/tasks/<taskId>.json` at submit time and letting `wait` / `status` / `fetch` reload it:

```json
{
  "taskId": "task_nano-banana-pro_...",
  "model": "nano-banana-pro",
  "prompt": "...",
  "output_dir": "/abs/path/to/kie-output",
  "state": "submitted",   // or "generating", "success", "downloaded", "fail"
  "remote_urls": [],
  "local_files": [],
  "created_at": "2026-04-10T12:34:56Z",
  "updated_at": "2026-04-10T12:34:56Z"
}
```

Operational consequences:

- `wait <taskId>` is **idempotent**. Running it twice on an already-downloaded task is a no-op; running it after a killed previous `wait` resumes polling the same KIE task (no re-generation, no extra credits).
- `wait` exit code `3` means "still generating, resumable" — NOT a failure. Retry the same command.
- If `wait` was killed *after* KIE marked the task `success` but *before* the download completed, the remote URLs are still fetchable via `fetch <taskId>` for ~24 h. Don't re-run `submit`.
- If you're calling the script from something other than Claude Code and want a single process to own the whole lifecycle, the legacy `generate.py "<prompt>"` form still works — it's just not safe inside an agent.

## Raw `curl` examples

Create task:

```bash
curl -X POST https://api.kie.ai/api/v1/jobs/createTask \
  -H "Authorization: Bearer $KIE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nano-banana-pro",
    "input": {
      "prompt": "a golden retriever puppy in snow, cinematic",
      "aspect_ratio": "16:9",
      "resolution": "2K"
    }
  }'
```

Poll task:

```bash
curl -H "Authorization: Bearer $KIE_API_KEY" \
  "https://api.kie.ai/api/v1/jobs/recordInfo?taskId=task_nano-banana-pro_1734567890"
```

Upload a local reference:

```bash
curl -X POST https://kieai.redpandaai.co/api/file-stream-upload \
  -H "Authorization: Bearer $KIE_API_KEY" \
  -F "file=@/path/to/ref.jpg" \
  -F "uploadPath=images/nano-banana-refs"
```
