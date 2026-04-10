---
name: kie-nano-banana
description: Generate images using Google Nano Banana Pro or Nano Banana 2 via the KIE.ai API. Use this skill whenever the user wants to create or generate an image with Nano Banana, KIE, nano-banana-pro, nano-banana-2, Google's Nano Banana models, or asks for text-to-image / image-to-image generation with reference images. Also trigger on Thai phrases like สร้างภาพ, สร้างรูป, วาดรูป, ทำรูป, gen รูป, or English phrases like generate an image, make a picture, create an image with references — even when the user doesn't explicitly name the model. Handles the full pipeline end-to-end — auto-uploading local reference images, submitting the task, polling until done, and downloading the result before the remote URL expires. Uses a resumable submit + wait subcommand split so long generations survive Claude Code's Bash timeout — a killed wait can be resumed with no credits lost.
---

# KIE Nano Banana image generation

Generate images with Google's Nano Banana Pro or Nano Banana 2 models hosted on KIE.ai.

## Choosing a model

Pick the model based on what the user is doing, not on "newer = better". Both models run the same generation pipeline — the differences are about limits.

**nano-banana-pro** — default for single-image generation
- Up to **8** reference images
- Prompt up to **10,000** characters
- Aspect ratios: `1:1`, `2:3`, `3:2`, `3:4`, `4:3`, `4:5`, `5:4`, `9:16`, `16:9`, `21:9`, `auto`
- Default output: **PNG**
- Choose this when the user wants fine-grained portrait ratios like `4:5` or `2:3` (useful for print, social posts, product photography).

**nano-banana-2** — default when many references or very long prompts are needed
- Up to **14** reference images
- Prompt up to **20,000** characters
- Aspect ratios: `1:1`, `16:9`, `9:16`, `4:3`, `3:4`, `21:9`, `auto`
- Default output: **JPG**
- Choose this for multi-reference compositing (collages, character-consistency work across many shots, style fusion from several images).

If the user says "Nano Banana" without specifying, ask which one they want only if the choice materially changes the result (e.g. they handed you 10 reference images — only nano-banana-2 accepts that). Otherwise default to `nano-banana-pro`.

## Quick start — use the resumable submit + wait flow

KIE generation is asynchronous: `createTask` returns a taskId in a couple of seconds, but the actual image takes anywhere from 30 s to several minutes to render. If you try to do the whole thing in one Bash call, **Claude Code's 2-minute Bash timeout will kill the process mid-poll** and you'll lose the image — even though KIE finished it server-side. The script is split into subcommands specifically to avoid that.

**Always use this two-step pattern, not the legacy single call:**

```bash
# Step 1 — submit. Finishes in under 5 seconds, gives you a taskId.
python3 scripts/generate.py submit \
  "a golden retriever puppy playing in snow, cinematic lighting, 35mm film" \
  --model nano-banana-pro \
  --aspect-ratio 16:9
```

The script defaults to **`--resolution 4K`** so the user gets the highest-quality output without having to remember the flag. Override to `2K` or `1K` only when the user explicitly asks for a smaller file, or when they're doing many draft iterations and want to save credits.

Parse the `taskId` out of the JSON stdout, then:

```bash
# Step 2 — wait. Polls until success, downloads the image, exits 0.
# IMPORTANT: pass a long Bash timeout (at least 600_000 ms) when you
# invoke this, because the default Bash tool timeout of 120_000 ms WILL
# kill it mid-poll for anything but the fastest tasks.
python3 scripts/generate.py wait <taskId>
```

When you invoke `wait` via the Bash tool, set `timeout: 600000` (10 min ceiling — the max Claude Code allows). That covers ~95 % of tasks in one call. If a task is very slow and `wait` exits with **code 3** ("still generating, resumable"), just call `wait <taskId>` again — no credits are spent on retry because KIE is still working on the original task.

### The four subcommands

| Command | What it does | Typical duration |
| --- | --- | --- |
| `submit <prompt> [opts]` | creates the task, writes `~/.kie/tasks/<id>.json`, returns the taskId | < 5 s |
| `wait <taskId>` | polls `recordInfo` until `success` / `fail`, downloads on success, updates state file. Exit codes: `0` done, `1` hard fail, `3` still generating (resumable) | up to 9 min per call |
| `status <taskId>` | one quick poll, prints current state, no download | < 2 s |
| `fetch <taskId>` | for tasks KIE has already finished: downloads `resultUrls` and saves them locally. Use this to recover a task whose `wait` was killed after success but before download | 5–15 s |

The state file at `~/.kie/tasks/<taskId>.json` is the single source of truth — it remembers the prompt, model, output directory, remote URLs, and local file paths. That's how `wait`, `status`, and `fetch` can all work with just a taskId, and how `wait` becomes idempotent (a second call on an already-downloaded task returns immediately).

### Legacy all-in-one mode

For shell users running the script directly from a terminal, the old form still works:

```bash
python3 scripts/generate.py "a serene mountain lake at sunrise" \
  --model nano-banana-pro --aspect-ratio 16:9
```

Do **not** use this form from inside Claude Code — it has the same Bash-timeout vulnerability the subcommand split was designed to fix. Use `submit` + `wait` instead.

**Use `python3`, not `python`.** Recent macOS ships only `python3` in `$PATH`; the bare `python` form will fail with `command not found`.

Images are saved to `./kie-output/` by default. The JSON summary on stdout carries `taskId`, `state`, `remote_urls`, and `local_files` — the local file is what the user keeps, because the remote URL expires in ~24 h.

## Authentication

The script looks for the API key in this order and uses the first one it finds:

1. **`KIE_API_KEY` environment variable** — the standard 12-factor approach. Best for CI and for users who already keep it in their shell rc.
2. **`./kie.env` in the current working directory** — a project-local dotenv file with one line: `KIE_API_KEY=...`. Use this when you want the key scoped to one repo and don't want it leaking into unrelated processes. (The script deliberately ignores a generic `./.env` to avoid reading files that belong to other projects.)
3. **`~/.kie/.env`** — a user-global dotenv file with the same format. Survives across sessions and projects.
4. **`~/.kie_api_key`** — a bare file containing only the key, no `KEY=` prefix. The simplest option if you don't need multiple values.

Recommend one to the user based on their situation; don't rewrite their whole shell config. If nothing is set, the script exits with a helpful message listing all four options. Keys are managed at https://kie.ai/api-key.

Security housekeeping the user should do once:
- `chmod 600 ~/.kie_api_key` (or `~/.kie/.env`) so other local users can't read it.
- If `kie.env` lives inside a git repo, add `kie.env` to `.gitignore` before committing anything.

Never paste the key into the chat or into a file we're about to commit.

## Reference images (image-to-image, style transfer, subject consistency)

Pass `--reference` once per image, up to the model's limit. You can mix local files and remote URLs freely:

```bash
python3 scripts/generate.py submit \
  "reimagine this portrait in the style of the second image, oil painting, dramatic lighting" \
  --model nano-banana-pro \
  --reference ./portrait.jpg \
  --reference https://example.com/van-gogh-reference.jpg
```

(Then `wait <taskId>` as usual.)

Local files are auto-uploaded to KIE's temporary file storage first (it returns a URL, which is the only thing the generation endpoint accepts). Uploaded files auto-delete after 3 days, which is fine for one-shot generations — don't reuse the same upload across days.

## All options

```
generate.py submit <prompt>
  --model {nano-banana-pro, nano-banana-2}       default: nano-banana-pro
  --reference <url or path>                      repeatable; up to model's limit
  --aspect-ratio <ratio>                          see per-model list above
  --resolution {1K, 2K, 4K}                       default: 4K
  --output-format {png, jpg}                      default: model-specific
  --output-dir <path>                             default: ./kie-output

generate.py wait <taskId>
  --timeout <seconds>                             default 540 (under Bash ceiling)
  --poll-interval <seconds>                       default 5

generate.py status <taskId>
generate.py fetch <taskId>
```

For parameter details or when you need to make the raw HTTP call manually, read `references/api.md`.

## Common errors

Match the error code to the right fix — don't blindly retry.

| HTTP | Meaning | Action |
|---|---|---|
| 401 | Missing / invalid API key | Check `KIE_API_KEY`. Get a new key from kie.ai if needed. |
| 402 | Insufficient credits | Tell the user to top up at kie.ai. Do not retry automatically. |
| 422 | Validation error | Re-read the parameters. Usually an aspect-ratio not allowed by the chosen model, or an image URL the service can't fetch. |
| 429 | Rate limited | Wait 30–60 seconds and retry once. If it persists, back off further. |
| 500 | Server error | Retry once. If it persists, it's on KIE — tell the user. |
| 501 | Generation failed | Often moderation-triggered. Try reframing the prompt before retrying; don't spam retries with the same prompt. |

## Why the skill is shaped this way

Four facts about the KIE API + the Claude Code runtime drove every design decision — read these so you understand what the script is protecting the user from.

1. **The generation endpoint is asynchronous.** `createTask` returns a `taskId` immediately; the actual image is produced by polling `recordInfo` until `state` becomes `success` or `fail`, then parsing `resultUrls` out of the `resultJson` string. Every invocation needs the same create → poll → parse loop, and writing that inline each time is error-prone. The script does it once, correctly.

2. **Result URLs expire in ~24 hours.** A user who comes back the next day loses the image unless it was downloaded at generation time. The script downloads immediately — not as a convenience, as correctness.

3. **The generation endpoint does not accept local files.** Only URLs. Making the user upload separately turns one logical request ("make me this image with that reference") into two. The script uploads any local file path through `/api/file-stream-upload` first and substitutes the returned URL transparently.

4. **Claude Code's Bash tool has a default 2-minute timeout and a 10-minute hard ceiling.** A single blocking call that does `createTask + poll + download` will routinely get killed mid-poll on any task that takes more than 2 minutes — and KIE Pro at 2K/4K often does. That's why the script is split into `submit` (fast, stateless from the caller's POV) and `wait` (resumable from a state file at `~/.kie/tasks/<id>.json`). If `wait` ever gets killed or times out, calling it again resumes from the existing KIE task — no credits lost, no re-generation. Webhooks would solve the same problem in theory, but require a publicly reachable endpoint, which is unrealistic for a CLI running on a user's laptop.

If you ever find yourself wanting to skip the script and call the API directly — you probably shouldn't. If you must (e.g. debugging a specific field), `references/api.md` has the full request/response shapes.
