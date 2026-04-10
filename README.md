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

### The resumable `submit` → `wait` pattern (recommended)

`generate.py` is split into four subcommands so long generations survive short-lived caller processes:

```bash
# Step 1 — submit. Returns a taskId in < 5 seconds.
python3 scripts/generate.py submit \
  "a serene mountain lake at sunrise, mist rising off the water, 35mm film look" \
  --model nano-banana-pro \
  --aspect-ratio 16:9
```

The script defaults to **`--resolution 4K`**. Override to `--resolution 2K` or `1K` only when you want smaller files or are running many draft iterations.

Parse `taskId` from the JSON on stdout, then:

```bash
# Step 2 — wait. Polls until the image is ready, then downloads it.
python3 scripts/generate.py wait <taskId>
```

`wait` is **idempotent and resumable**. If the caller process gets killed mid-poll (Claude Code's Bash tool kills commands after 2 minutes by default), just run `wait <taskId>` again — no credits are spent, because KIE is still working on the original task.

The four subcommands:

| Command | Purpose |
| --- | --- |
| `submit <prompt> [opts]` | create the task, write `~/.kie/tasks/<id>.json`, return the taskId |
| `wait <taskId>` | poll until success, download the image, update state. Exit 0 done, 1 hard fail, 3 still generating |
| `status <taskId>` | one quick poll, print current state — no download |
| `fetch <taskId>` | for tasks KIE has already finished: pull `resultUrls` and save locally |

### Image-to-image with a local reference (auto-uploaded)

```bash
python3 scripts/generate.py submit \
  "transform this into a watercolor painting" \
  --model nano-banana-pro \
  --reference ./my-photo.jpg \
  --aspect-ratio 1:1
# ... then wait <taskId>
```

### Mix local files and remote URLs, up to the model's reference limit

```bash
python3 scripts/generate.py submit \
  "combine the character from the first image with the style of the second, cinematic lighting" \
  --model nano-banana-2 \
  --reference ./character.png \
  --reference https://example.com/style-ref.jpg \
  --aspect-ratio 16:9
# ... then wait <taskId>
```

What the script does for you:
1. Uploads any local reference files via KIE's file-stream endpoint (returns a public URL).
2. Submits the generation task and writes `~/.kie/tasks/<taskId>.json`.
3. `wait` polls `recordInfo` every 5 seconds until the task finishes, honouring its own timeout (default 540 s) and a resumable exit code for longer tasks.
4. Downloads every result image to `./kie-output/` (configurable with `--output-dir`).
5. Prints a JSON summary on stdout with `taskId`, `state`, `remote_urls`, and `local_files`.

### Legacy all-in-one mode

For shell users running the script directly, the old form still works:

```bash
python3 scripts/generate.py "a serene mountain lake at sunrise" \
  --model nano-banana-pro --aspect-ratio 16:9
```

Avoid this form inside Claude Code — it has the exact Bash-timeout vulnerability the subcommand split was designed to fix.

### All CLI flags

```
generate.py submit <prompt>
  --model {nano-banana-pro, nano-banana-2}   default: nano-banana-pro
  --reference URL_OR_PATH                    repeatable (8 max for Pro, 14 for 2)
  --aspect-ratio RATIO                       e.g. 1:1, 16:9, 9:16, 4:5 — see per-model list
  --resolution {1K, 2K, 4K}                  default: 4K
  --output-format {png, jpg}                 default: PNG for Pro, JPG for 2
  --output-dir DIR                           default: ./kie-output

generate.py wait <taskId>
  --timeout SECONDS                          polling timeout, default 540 (stays under Bash ceiling)
  --poll-interval SECONDS                    default 5

generate.py status <taskId>                  one quick poll, exits fast
generate.py fetch <taskId>                   download a task KIE already finished
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

---

## คู่มือภาษาไทย

สกิลนี้ทำให้ Claude (ทั้ง Claude Code, Claude Desktop และ Agent SDK) สามารถสร้างภาพด้วยโมเดล **Google Nano Banana Pro** หรือ **Nano Banana 2** ผ่าน API ของ [KIE.ai](https://kie.ai) ได้จากการพิมพ์ข้อความธรรมชาติ ไม่ต้องเขียนโค้ด ไม่ต้องอัปโหลดรูปอ้างอิงเอง ไม่ต้องมานั่ง poll สถานะงาน และไม่ต้องกลัวว่า URL ผลลัพธ์จะหมดอายุ — สคริปต์จัดการให้หมด

### สิ่งที่ต้องเตรียมครั้งเดียว

1. **Python 3.9 ขึ้นไป** (macOS ใหม่ ๆ มีติดมาแล้วในชื่อ `python3`)
2. ติดตั้งไลบรารี `requests`
   ```bash
   pip3 install requests
   ```
3. **API key จาก KIE** — ขอได้ที่ <https://kie.ai/api-key>
4. **ติดตั้งสกิล** — ดาวน์โหลดไฟล์ `.skill` จากหน้า Releases แล้ว unzip เข้าโฟลเดอร์สกิลของ Claude
   ```bash
   gh release download -R trin-zenityx/kie-nano-banana-skill --pattern 'kie-nano-banana.skill'
   unzip kie-nano-banana.skill -d ~/.claude/skills/
   ```

### ตั้งค่า API key (วิธีง่ายสุดสำหรับมือใหม่บน Mac)

เปิด Terminal แล้วพิมพ์ทีละบรรทัด:

```bash
mkdir -p ~/.kie
nano ~/.kie/.env
```

ในหน้าต่าง nano ที่เปิดขึ้นมา พิมพ์บรรทัดเดียวว่า:

```
KIE_API_KEY=sk-คีย์ของคุณตรงนี้
```

กด `Ctrl + O` แล้ว `Enter` เพื่อบันทึก จากนั้น `Ctrl + X` เพื่อออก สุดท้ายรัน:

```bash
chmod 600 ~/.kie/.env
```

เพื่อให้ไฟล์อ่านได้เฉพาะตัวเอง สคริปต์จะหาคีย์จากไฟล์นี้อัตโนมัติทุกครั้งที่เรียกใช้

### วิธีใช้ — พิมพ์คุยกับ Claude ได้เลย

พอสกิลถูกติดตั้งแล้ว **คุณไม่ต้องเรียกสกิลด้วยคำสั่งพิเศษ** แค่เปิดห้องแชทกับ Claude (Claude Code หรือ Claude Desktop) แล้วพิมพ์สิ่งที่อยากได้เป็นภาษาไทยหรืออังกฤษตามปกติ Claude จะดูคำในข้อความของคุณและเรียกสกิลให้เอง

ตัวอย่างประโยคที่ใช้ได้จริง:

```
สร้างภาพทะเลยามเย็นมีเรือใบลำเล็ก ใช้ nano banana pro แบบ 16:9
```

```
ช่วย gen รูปแมวส้มนอนบนโซฟาในบ้านสไตล์ญี่ปุ่น ใช้ nano-banana-2 แนวตั้ง 9:16
```

```
วาดรูปร้านกาแฟในซอยเงียบ ๆ ตอนเช้า แสงนุ่ม ๆ สไตล์ฟิล์ม 35mm
```

```
เอารูป ./photo.jpg ไปทำเป็นภาพสีน้ำให้หน่อย ใช้ nano banana pro
```

```
สร้างภาพตัวละครจากรูปแรก ในสไตล์ของรูปที่สอง ใช้ nano-banana-2
— reference: ./character.png
— reference: https://example.com/style.jpg
```

คำที่ Claude จะจับแล้วเรียกสกิลให้อัตโนมัติ: **สร้างภาพ, สร้างรูป, วาดรูป, ทำรูป, gen รูป, generate an image, make a picture, nano banana, KIE** และคำใกล้เคียง

### บอกรายละเอียดอะไรได้บ้าง

- **โมเดล** — `nano-banana-pro` (ดีฟอลต์ เหมาะกับงานรูปเดี่ยว) หรือ `nano-banana-2` (เหมาะเมื่อต้องใส่รูปอ้างอิงหลายรูป หรือมี prompt ยาว ๆ)
- **อัตราส่วนภาพ** — Pro รองรับ `1:1, 2:3, 3:2, 3:4, 4:3, 4:5, 5:4, 9:16, 16:9, 21:9, auto` / 2 รองรับ `1:1, 16:9, 9:16, 4:3, 3:4, 21:9, auto`
- **ความละเอียด** — `1K`, `2K`, `4K` (ดีฟอลต์ `4K` — ถ้าอยากได้ไฟล์เล็กกว่านี้ให้บอก Claude ว่า "ใช้ 2K" หรือ "1K")
- **รูปอ้างอิง** — ใส่เป็น URL หรือไฟล์ในเครื่องก็ได้ สคริปต์จะอัปโหลดให้เอง (Pro สูงสุด 8 รูป / 2 สูงสุด 14 รูป)
- **โฟลเดอร์ปลายทาง** — ดีฟอลต์คือ `./kie-output/` ภาพจะถูกดาวน์โหลดมาไว้ในเครื่องทันทีหลังเจนเสร็จ

### เลือกโมเดลยังไงดี

| ใช้ `nano-banana-pro` เมื่อ | ใช้ `nano-banana-2` เมื่อ |
| --- | --- |
| งานรูปเดี่ยว รูปโปสเตอร์ รูปโปรดักต์ | ต้องใส่รูปอ้างอิงหลายรูปพร้อมกัน (> 8) |
| ต้องการอัตราส่วน portrait ละเอียด เช่น `4:5` หรือ `2:3` | ต้องการความสม่ำเสมอของตัวละคร (character consistency) |
| Prompt สั้นถึงปานกลาง (ไม่เกิน 10,000 ตัวอักษร) | Prompt ยาวมาก ๆ (ไม่เกิน 20,000 ตัวอักษร) |

ถ้าไม่ระบุ Claude จะใช้ `nano-banana-pro` เป็นค่าดีฟอลต์

### ปัญหาที่เจอบ่อย

| ข้อความ error | สาเหตุ | วิธีแก้ |
| --- | --- | --- |
| `401 Unauthorized` | คีย์ผิดหรือไม่เจอคีย์ | ตรวจไฟล์ `~/.kie/.env` ว่ามี `KIE_API_KEY=...` |
| `402 Insufficient credits` | เครดิตหมด | เติมเครดิตที่ <https://kie.ai> |
| `422 Validation error` | พารามิเตอร์ผิด (มักเป็น aspect ratio ที่โมเดลนั้นไม่รองรับ หรือ URL รูปอ้างอิงเปิดไม่ได้) | ลองเปลี่ยน ratio ให้ตรงกับรายการที่โมเดลรองรับ |
| `429 Rate limited` | ยิงเร็วไป | รอ 30–60 วินาทีแล้วลองใหม่ |
| `501 Generation failed` | มักโดน content filter | ปรับคำใน prompt ให้เบาลงก่อนลองใหม่ |
| `command not found: python` | macOS ไม่มี `python` แบบไม่มีเลข | ใช้ `python3` แทน (สคริปต์ของสกิลใช้ `python3` อยู่แล้ว) |

### เบื้องหลัง — ทำไมต้องแบ่งเป็น submit + wait

KIE เป็น async API: `createTask` คืน `taskId` ภายใน 5 วินาที แต่กว่าภาพจริงจะเสร็จอาจใช้เวลา 30 วิ – หลายนาที ปัญหาคือ **Bash tool ของ Claude Code มี default timeout 2 นาที** — ถ้าเราเจน + รอ + ดาวน์โหลดในคำสั่งเดียว พอเกิน 2 นาที Claude Code จะ kill process ทิ้ง ทั้งที่ KIE เจนเสร็จแล้ว รูปเลยหาย (v1.0 เป็นแบบนี้)

v1.1 แก้โดยแยกสคริปต์เป็น subcommand:

```bash
# Step 1 — submit (เร็ว < 5 วินาที)
python3 ~/.claude/skills/kie-nano-banana/scripts/generate.py submit \
  "ภาพภูเขายามเช้าตรู่ หมอกลอยเหนือทะเลสาบ" \
  --model nano-banana-pro --aspect-ratio 16:9

# Step 2 — wait (poll จนเสร็จ + ดาวน์โหลด)
python3 ~/.claude/skills/kie-nano-banana/scripts/generate.py wait <taskId>
```

- **state file** ถูกเก็บที่ `~/.kie/tasks/<taskId>.json` — จดจำ prompt, model, output dir, สถานะปัจจุบัน
- ถ้า `wait` ถูก kill กลางทาง → รัน `wait <taskId>` ซ้ำได้เลย **ไม่เสียเครดิต** เพราะ task เดิมยังทำงานอยู่บน KIE
- ถ้า KIE เจนเสร็จแล้วแต่ `wait` ถูก kill ก่อนดาวน์โหลด → ใช้ `fetch <taskId>` ดึงรูปกลับมา (ยัง valid ภายใน 24 ชม.)

ตอนคุยกับ Claude ไม่ต้องคิดเรื่องนี้ Claude จะเรียก `submit` แล้ว `wait` ให้อัตโนมัติ — รู้ไว้เผื่อเวลา debug พอ

### ถ้าอยากรันสคริปต์ตรง ๆ โดยไม่ผ่าน Claude

มี legacy mode (ทำทุกอย่างในคำสั่งเดียว) เหลือไว้สำหรับใช้จาก terminal:

```bash
python3 ~/.claude/skills/kie-nano-banana/scripts/generate.py \
  "ภาพภูเขายามเช้าตรู่ หมอกลอยเหนือทะเลสาบ" \
  --model nano-banana-pro \
  --aspect-ratio 16:9
```

ภาพจะถูกเซฟไว้ที่ `./kie-output/` และสคริปต์จะพิมพ์ JSON สรุป (`taskId`, URL บน KIE, path ในเครื่อง) ออกมาที่หน้าจอ — ใช้ได้เฉพาะจาก terminal ปกตินะครับ ไม่ควรให้ Claude Code เรียก legacy mode เพราะจะเจอปัญหา Bash timeout แบบเดิม
