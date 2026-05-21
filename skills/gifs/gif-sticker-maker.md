---
name: gif-sticker-maker
description: |
  Convert photos (people, pets, objects, logos) into 4 animated GIF stickers with captions.
  Use when: user wants to create cartoon stickers, GIF expressions, emoji packs, animated avatars,
  or convert photos to Funko Pop / Pop Mart blind box style animations.
  Triggers: sticker, GIF, cartoon, emoji, expression pack, avatar animation.
license: MIT
metadata:
  version: "1.5"
  category: creative-tools
  style: Funko Pop / Pop Mart
  output_format: GIF
  enhanced: true
  output_count: 4
  sources:
    - MiniMax Image Generation API
    - MiniMax Video Generation API
---

# GIF Sticker Maker

Convert user photos into 4 animated GIF stickers (Funko Pop / Pop Mart style).

## Style Spec

- Funko Pop / Pop Mart blind box 3D figurine
- C4D / Octane rendering quality
- White background, soft studio lighting
- Caption: black text + white outline, bottom of image

## Prerequisites

Before starting any generation step, ensure:

1. **Python venv** is activated with dependencies from [requirements.txt](references/requirements.txt) installed
2. **`MINIMAX_API_KEY`** is exported (e.g. `export MINIMAX_API_KEY='your-key'`)
3. **`MINIMAX_API_BASE`** is set (e.g. `export MINIMAX_API_BASE='https://api.minimax.io/v1'` for overseas)
4. **`ffmpeg`** is available on PATH (for Step 3 GIF conversion)

If any prerequisite is missing, set it up first. Do NOT proceed to generation without all four.

## Workflow

### Step 0: Language Detection

Detect the user's language **from the first message in the conversation** using LLM inference — do NOT rely on Accept-Language headers, OS locale, or external lookups. Match the detected language to the captions table columns (English, Spanish, French, German, Chinese, Japanese, Korean). All captions and responses must use this single detected language. **Never mix languages.**

If the user later requests a different language explicitly, update and restart from Step 1.

### Step 1: Collect Captions

Ask user (in their detected language):
> "Would you like to customize the captions for your stickers, or use the defaults?"

- **Custom**: Collect 4 short captions (1–3 words). Actions auto-match caption meaning.
- **Default**: Look up [captions table](references/captions.md) by **detected user language**. **Never mix languages.**

### Step 2: Generate 4 Static Sticker Images

**Tool**: `scripts/minimax_image.py`

1. Analyze the user's photo — identify subject type (person / animal / object / logo).
2. For each of the 4 stickers, build a prompt from [image-prompt-template.txt](assets/image-prompt-template.txt) by filling `{action}` and `{caption}`.
3. **If subject is a person**: pass `--subject-ref <user_photo_path>` so the generated figurine preserves the person's actual facial likeness.
4. **Run all 4 generations in parallel** using `&` + `wait`. Enclose each command in a retry wrapper that attempts up to 3 times with exponential backoff (2s, 4s, 8s) on non-zero exit. If all 4 retries exhaust, abort the entire workflow.

Generate concurrently:

```bash
# Retry wrapper — 3 attempts with exponential backoff
retry_image() {
  local cmd="$1"; local output="$2"; local max_attempts=3
  local delay=2
  for attempt in $(seq 1 $max_attempts); do
    echo "[image attempt $attempt/3] $output"
    eval "$cmd" && { [[ -s "$output" ]] && echo "[OK] $output ($(stat -f%z "$output") bytes)" && return 0; } || true
    [[ $attempt -lt $max_attempts ]] && echo "[retry $attempt failed, waiting ${delay}s...]" && sleep $delay || true
    delay=$((delay * 2))
  done
  echo "[FATAL] image generation failed after $max_attempts attempts: $output" >&2
  return 1
}

# Launch all 4 in parallel, validate each output before accepting
mkdir -p output
retry_image \
  "python3 scripts/minimax_image.py \"<prompt_hi>\" -o output/sticker_hi.png --ratio 1:1 --subject-ref <photo>" \
  "output/sticker_hi.png" &
pid_hi=$!

retry_image \
  "python3 scripts/minimax_image.py \"<prompt_laugh>\" -o output/sticker_laugh.png --ratio 1:1 --subject-ref <photo>" \
  "output/sticker_laugh.png" &
pid_laugh=$!

retry_image \
  "python3 scripts/minimax_image.py \"<prompt_cry>\" -o output/sticker_cry.png --ratio 1:1 --subject-ref <photo>" \
  "output/sticker_cry.png" &
pid_cry=$!

retry_image \
  "python3 scripts/minimax_image.py \"<prompt_love>\" -o output/sticker_love.png --ratio 1:1 --subject-ref <photo>" \
  "output/sticker_love.png" &
pid_love=$!

# Wait and check all exits
failed=0
for pid in $pid_hi $pid_laugh $pid_cry $pid_love; do
  wait $pid || { echo "[ERROR] PID $pid exited non-zero" >&2; failed=$((failed+1)); }
done

# Validate all 4 files exist and are non-empty
for f in output/sticker_hi.png output/sticker_laugh.png output/sticker_cry.png output/sticker_love.png; do
  if [[ ! -s "$f" ]]; then
    echo "[FATAL] Missing or empty output: $f" >&2
    failed=$((failed+1))
  fi
done

if [[ $failed -gt 0 ]]; then
  echo "[FATAL] Step 2 failed: $failed error(s)" >&2
  exit 1
fi
echo "[Step 2 complete] 4 sticker images generated and validated"
```

> `--subject-ref` only works for person subjects (API limitation: type=character).
> For animals/objects/logos, omit the flag and rely on text description.

### Step 3: Animate Each Image → Video

**Tool**: `scripts/minimax_video.py` with `--image` flag (image-to-video mode)

For each sticker image, build a prompt from [video-prompt-template.txt](assets/video-prompt-template.txt), then run all 4 in parallel with retry logic:

```bash
# Retry wrapper — 3 attempts with exponential backoff
retry_video() {
  local cmd="$1"; local output="$2"; local max_attempts=3
  local delay=2
  for attempt in $(seq 1 $max_attempts); do
    echo "[video attempt $attempt/3] $output"
    eval "$cmd" && { [[ -s "$output" ]] && echo "[OK] $output ($(stat -f%z "$output") bytes)" && return 0; } || true
    [[ $attempt -lt $max_attempts ]] && echo "[retry $attempt failed, waiting ${delay}s...]" && sleep $delay || true
    delay=$((delay * 2))
  done
  echo "[FATAL] video generation failed after $max_attempts attempts: $output" >&2
  return 1
}

retry_video \
  "python3 scripts/minimax_video.py \"<prompt_hi>\" --image output/sticker_hi.png -o output/sticker_hi.mp4" \
  "output/sticker_hi.mp4" &
pid_hi=$!

retry_video \
  "python3 scripts/minimax_video.py \"<prompt_laugh>\" --image output/sticker_laugh.png -o output/sticker_laugh.mp4" \
  "output/sticker_laugh.mp4" &
pid_laugh=$!

retry_video \
  "python3 scripts/minimax_video.py \"<prompt_cry>\" --image output/sticker_cry.png -o output/sticker_cry.mp4" \
  "output/sticker_cry.mp4" &
pid_cry=$!

retry_video \
  "python3 scripts/minimax_video.py \"<prompt_love>\" --image output/sticker_love.png -o output/sticker_love.mp4" \
  "output/sticker_love.mp4" &
pid_love=$!

failed=0
for pid in $pid_hi $pid_laugh $pid_cry $pid_love; do
  wait $pid || { echo "[ERROR] PID $pid exited non-zero" >&2; failed=$((failed+1)); }
done

# Validate all 4 files exist and are non-empty (MP4 header minimum ~1KB)
for f in output/sticker_hi.mp4 output/sticker_laugh.mp4 output/sticker_cry.mp4 output/sticker_love.mp4; do
  if [[ ! -s "$f" ]]; then
    echo "[FATAL] Missing or empty output: $f" >&2
    failed=$((failed+1))
  elif [[ $(stat -f%z "$f") -lt 1024 ]]; then
    echo "[FATAL] Suspiciously small output (possible ffmpeg failure): $f ($(stat -f%z "$f") bytes)" >&2
    failed=$((failed+1))
  fi
done

if [[ $failed -gt 0 ]]; then
  echo "[FATAL] Step 3 failed: $failed error(s)" >&2
  exit 1
fi
echo "[Step 3 complete] 4 videos generated and validated"
```

All 4 calls are independent — **run concurrently**.

### Step 4: Convert Videos → GIF

**Tool**: `scripts/convert_mp4_to_gif.py`

Validate all MP4s exist and are non-empty **before** calling ffmpeg:

```bash
for f in output/sticker_hi.mp4 output/sticker_laugh.mp4 output/sticker_cry.mp4 output/sticker_love.mp4; do
  if [[ ! -s "$f" ]]; then
    echo "[FATAL] Missing or empty MP4 before GIF conversion: $f" >&2
    exit 1
  fi
done

python3 scripts/convert_mp4_to_gif.py output/sticker_hi.mp4 output/sticker_laugh.mp4 output/sticker_cry.mp4 output/sticker_love.mp4
```

Validate all GIFs exist and are non-empty **after** conversion:

```bash
failed=0
for f in output/sticker_hi.gif output/sticker_laugh.gif output/sticker_cry.gif output/sticker_love.gif; do
  if [[ ! -s "$f" ]]; then
    echo "[FATAL] GIF missing or empty after conversion: $f" >&2
    failed=$((failed+1))
  else
    echo "[OK] $f ($(stat -f%z "$f") bytes)"
  fi
done

if [[ $failed -gt 0 ]]; then
  echo "[FATAL] Step 4 failed: $failed GIF(s) missing/empty" >&2
  exit 1
fi
echo "[Step 4 complete] 4 GIFs generated and validated"
```

Outputs GIF files alongside each MP4 (e.g. `sticker_hi.gif`).

### Step 5: Deliver

Output format (strict order):
1. Brief status line (e.g. "4 stickers created:")
2. `<deliver_assets>` block with all GIF files
3. **NO text after deliver_assets**

```xml
<deliver_assets>
<item><path>output/sticker_hi.gif</path></item>
<item><path>output/sticker_laugh.gif</path></item>
<item><path>output/sticker_cry.gif</path></item>
<item><path>output/sticker_love.gif</path></item>
</deliver_assets>
```

## Default Actions

| # | Action | Filename ID | Animation |
|---|--------|-------------|-----------|
| 1 | Happy waving | hi | Wave hand, slight head tilt |
| 2 | Laughing hard | laugh | Shake with laughter, eyes squint |
| 3 | Crying tears | cry | Tears stream, body trembles |
| 4 | Heart gesture | love | Heart hands, eyes sparkle |

See [references/captions.md](references/captions.md) for multilingual caption defaults.

## Rules

- Language detection: infer from user's **first message** in the conversation using LLM inference only
- All outputs follow the detected language — never mix languages
- Captions MUST come from [captions.md](references/captions.md) matching user's language column — never mix languages
- All image prompts must be in **English** regardless of user language (only caption text is localized)
- `<deliver_assets>` must be LAST in response, no text after
