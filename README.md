# KIE Nano Banana — Claude Skill

A Claude skill that lets any Claude agent (Claude Code, Claude Desktop, Agent SDK) generate images with **Google Nano Banana Pro** or **Nano Banana 2** through the [KIE.ai](https://kie.ai) API.

The skill turns a multi-step async API (`createTask` → poll `recordInfo` → parse `resultJson` → download before the 24-hour expiry) into a single command. Local reference images are auto-uploaded. Errors are surfaced cleanly. API keys are loaded from any of four standard locations. You don't have to think about any of it.

## Why use this skill instead of letting the agent figure it out?

I ran a head-to-head benchmark: the same three prompts executed by Claude Opus with and without the skill, hitting the real KIE API. Results:

| Metric                  | With skill | Without skill | Delta              |
| ----------------------- | ---------: | ------------: | ------------------ |
| Task correctness        |     100 %  |         100 % | —                  |
| Wall clock (avg/run)    |    71.2 s  |       154.5 s | **−54 % faster**   |
| Agent tool calls        |       2–6  |          9–15 | **3–5× fewer**     |
| Agent tokens (avg/run)  |    55,174  |        59,224 | −7 %               |

The biggest margin was on image-to-image with a local reference file — baseline had to hand-roll a multipart upload, debug a Cloudflare 1010 user-agent block, and discover that KIE's upload response uses `data.downloadUrl` instead of the documented `data.fileUrl`. The skill already handles all of that.

**Task correctness is the same either way.** The skill doesn't make the images better — it makes the agent reach the same result with half the friction.

## Installation

### Option A: download the packaged `.skill` file (recommended)

```bash
gh release download -R trin-zenityx/kie-nano-banana-skill --pattern 'kie-nano-banana.skill'
```

A `.skill` file is just a zip — drop it wherever your Claude environment expects skills, or unzip it manually:

```bash
unzip kie-nano-banana.skill -d ~/.claude/skills/
```

### Option B: clone the repo

```bash
git clone https://github.com/trin-zenityx/kie-nano-banana-skill.git
cp -r kie-nano-banana-skill/kie-nano-banana ~/.claude/skills/
```

The skill directory is self-contained; you only need its three real files:

```
kie-nano-banana/
├── SKILL.md            ← the skill's instructions
├── scripts/generate.py ← the full pipeline
└── references/api.md   ← raw API reference for deep dives
```

## Prerequisites

- **Python 3.9+** (uses only stdlib + `requests`)
- `requests` library: `pip install requests`
- A **KIE API key** from <https://kie.ai/api-key>

On macOS: use `python3`, not `python`. Recent macOS ships only `python3` in `$PATH`.

## Authentication — four ways to provide the API key

The script looks for the key in this order and uses the first one it finds. Pick whichever fits your workflow — **never paste the key into chat, commits, or shell history**.

| Priority | Location            | Format                    | Best for                              |
| -------: | ------------------- | ------------------------- | ------------------------------------- |
|        1 | `KIE_API_KEY` env var | `export KIE_API_KEY=...`  | CI, shell rc                          |
|        2 | `./kie.env`         | `KIE_API_KEY=...`         | Per-project secrets                   |
|        3 | `~/.kie/.env`       | `KIE_API_KEY=...`         | User-global config                    |
|        4 | `~/.kie_api_key`    | just the key, one line    | Simplest for a single-user laptop     |

Example: store the key globally on macOS/Linux in under ten seconds:

```bash
mkdir -p ~/.kie
echo 'KIE_API_KEY=sk-your-key-here' > ~/.kie/.env
chmod 600 ~/.kie/.env
```

## Usage

### Text-to-image

```bash
python3 scripts/generate.py \
  "a serene mountain lake at sunrise, mist rising off the water, 35mm film look" \
  --model nano-banana-pro \
  --aspect-ratio 16:9 \
  --resolution 2K
```

### Image-to-image with a local reference (auto-uploaded)

```bash
python3 scripts/generate.py \
  "transform this into a watercolor painting" \
  --model nano-banana-pro \
  --reference ./my-photo.jpg \
  --aspect-ratio 1:1
```

### Mix local files and remote URLs, up to the model's reference limit

```bash
python3 scripts/generate.py \
  "combine the character from the first image with the style of the second, cinematic lighting" \
  --model nano-banana-2 \
  --reference ./character.png \
  --reference https://example.com/style-ref.jpg \
  --aspect-ratio 16:9
```

The script:
1. Uploads any local files via KIE's file-stream endpoint (returns a public URL).
2. Submits the generation task and receives a `taskId`.
3. Polls `recordInfo` every 5 seconds until the task finishes.
4. Downloads every result image to `./kie-output/` (configurable with `--output-dir`).
5. Prints a JSON summary to stdout with `taskId`, `remote_urls`, and `local_files`.

### All CLI flags

```
generate.py <prompt>
  --model {nano-banana-pro, nano-banana-2}   default: nano-banana-pro
  --reference URL_OR_PATH                    repeatable (8 max for Pro, 14 for 2)
  --aspect-ratio RATIO                       e.g. 1:1, 16:9, 9:16, 4:5 — see per-model list
  --resolution {1K, 2K, 4K}                  default: 1K
  --output-format {png, jpg}                 default: PNG for Pro, JPG for 2
  --output-dir DIR                           default: ./kie-output
  --timeout SECONDS                          polling timeout, default 600
  --poll-interval SECONDS                    default 5
```

## Model comparison

|                              | `nano-banana-pro`            | `nano-banana-2`      |
| ---------------------------- | ---------------------------- | -------------------- |
| Prompt max length            | 10,000 chars                 | 20,000 chars         |
| Reference images             | up to 8                      | up to 14             |
| Aspect ratios                | 1:1, 2:3, 3:2, 3:4, 4:3, 4:5, 5:4, 9:16, 16:9, 21:9, auto | 1:1, 16:9, 9:16, 4:3, 3:4, 21:9, auto |
| Default output format        | PNG                          | JPG                  |
| Best at                      | fine-grained portrait ratios (4:5, 2:3) | many-reference compositing, very long prompts |

Pick Pro for single-image work with unusual aspect ratios; pick 2 when you need to feed the model many reference images or a very long prompt.

## How the agent uses this skill

When you ask Claude to "generate an image", "สร้างภาพ", "create an image with nano banana", or anything similar, the agent reads `SKILL.md`, learns which CLI flags to use, and calls `scripts/generate.py` directly. It does **not** need to read the API docs or write any HTTP code — `generate.py` encapsulates the full workflow end-to-end.

If the agent ever needs to make a raw HTTP call (for debugging or an unusual edge case), `references/api.md` contains the full endpoint, parameter, and response documentation.

## Error handling

The script exits with a clear stderr message on each known failure mode:

| HTTP | Meaning              | What to do                                              |
| ---: | -------------------- | ------------------------------------------------------- |
|  401 | bad API key          | check `KIE_API_KEY`, regenerate at kie.ai/api-key       |
|  402 | insufficient credits | top up at kie.ai                                        |
|  422 | validation error     | re-check parameters; often a bad aspect ratio or unreachable reference URL |
|  429 | rate limited         | wait and retry                                          |
|  500 | KIE server error     | one retry is fine                                       |
|  501 | generation failed    | usually moderation — reframe the prompt before retrying |

## Development

The skill was built using [skill-creator](https://github.com/anthropics/skills) and benchmarked with real KIE API calls. The `evals/` directory contains the three test prompts used during development.

Contributions welcome. If you find a bug or want to add a feature:

1. Open an issue describing the problem or idea.
2. Fork the repo, make your change, and open a pull request.
3. If you touch `generate.py`, please add or update the corresponding `evals/` entry and describe how you verified the behaviour.

## License

MIT — see [LICENSE](LICENSE).

## Credits

- API by [KIE.ai](https://kie.ai) — see their [docs](https://docs.kie.ai) for model details and pricing.
- Built with [Claude](https://claude.com) using [skill-creator](https://github.com/anthropics/skills/tree/main/skill-creator).
