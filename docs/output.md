# Output Structure

All conversation artifacts are organized into per-session directories under `result/`.

## Directory Layout

```
result/
└── 20260324_112523_a1b2c3d4/        ← one directory per conversation
    ├── images/                       ← generated images (PNG)
    │   ├── 20260324_112530_painter.png
    │   ├── 20260324_112545_painter.png
    │   └── 20260324_112600_painter.png
    ├── videos/                       ← generated videos (MP4)
    │   └── 20260324_112610_animator.mp4
    ├── music/                        ← generated audio (WAV)
    │   └── 20260324_112620_composer.wav
    ├── web/                          ← HTML pages
    │   ├── 20260324_112630_webshowcase.html
    │   ├── composer.wav              ← copied media sibling files
    │   └── animator.mp4
    ├── breakout/                     ← breakout session transcripts (MD)
    │   └── 20260324_112525_scene_development.md
    ├── manuscript.txt                ← accumulated accepted outputs
    └── memory.json                   ← session memory dump
```

## Session Directory Naming

Format: `{timestamp}_{conversation_id_slug}`

- **Timestamp**: `YYYYMMDD_HHMMSS` (session start time)
- **Conversation ID slug**: First 8 characters of the UUID from `conv:{uuid}`

Example: `20260324_112523_a1b2c3d4`

## Artifact Types

### `manuscript.txt`

Accumulated text from `[ACCEPT]`ed worker outputs in SHOWRUNNER_DRIVEN mode. Contains the final polished content (scripts, articles, reports, etc.).

Only created in SHOWRUNNER_DRIVEN sessions with at least one accepted output.

### `memory.json`

JSON dump of the session memory store. Structure:

```json
{
  "goals": [
    {"id": "...", "key": "mission", "content": "Create a sitcom scene...", "author": "system"}
  ],
  "tasks": [
    {"id": "...", "key": "breakout_1", "content": "Writers Room | 23 rounds | ...", "author": "system"}
  ],
  "decisions": [
    {"id": "...", "key": "art_style", "content": "Flat animation, bold outlines", "author": "Director"}
  ],
  "lessons": [
    {"id": "...", "key": "api_failures", "content": "HF FLUX 503 during peak hours", "author": "Director"}
  ]
}
```

Only created when the memory store is non-empty.

### `images/*.png`

Generated images from:
- `ImageAgent` (HF FLUX)
- `OpenAIImageAgent` (OpenAI)
- `GeminiImageAgent` (Google Gemini)

Filename: `{timestamp}_{agent_name}.png`

### `videos/*.mp4`

Generated videos from `VideoAgent` (HF).

Filename: `{timestamp}_{agent_name}.mp4`

### `music/*.wav`

Generated audio from `GeminiMusicAgent` (Google Lyria).

Format: WAV, 48kHz, 16-bit stereo PCM.

Filename: `{timestamp}_{agent_name}.wav`

### `web/*.html`

Self-contained HTML pages from `WebPageAgent`. Images are embedded as base64 data URIs. Audio and video files are copied as sibling files for relative references.

Filename: `{timestamp}_{agent_name}.html`

### `breakout/*.md`

Full transcripts of breakout sessions. Markdown format with:
- Topic header
- Agent list
- Round-by-round utterances
- Completion status

Filename: `{timestamp}_{topic_slug}.md`

## Implementation

The `SessionOutputManager` (`src/ofp_playground/config/output.py`) creates and manages the session directory:

```python
from ofp_playground.config.output import SessionOutputManager

output = SessionOutputManager("conv:a1b2c3d4-...")
output.root      # Path("result/20260324_112523_a1b2c3d4")
output.images    # Path("result/20260324_112523_a1b2c3d4/images")
output.videos    # Path("result/20260324_112523_a1b2c3d4/videos")
output.music     # Path("result/20260324_112523_a1b2c3d4/music")
output.web       # Path("result/20260324_112523_a1b2c3d4/web")
output.breakout  # Path("result/20260324_112523_a1b2c3d4/breakout")
```

Each subdirectory is created lazily on first access.

## Git Ignore

The `result/` directory is in `.gitignore` along with the legacy output directories:

```
result/
ofp-images/
ofp-videos/
ofp-music/
ofp-breakout/
ofp-showcase/
ofp-web/
```

## Migration from Legacy Directories

Previously, outputs were scattered across project-root directories:
- `ofp-images/` → now `result/<session>/images/`
- `ofp-videos/` → now `result/<session>/videos/`
- `ofp-music/` → now `result/<session>/music/`
- `ofp-web/` (and `ofp-showcase/`) → now `result/<session>/web/`
- `ofp-breakout/` → now `result/<session>/breakout/`
- `manuscript_*.txt` → now `result/<session>/manuscript.txt`
- `memory_*.json` → now `result/<session>/memory.json`

Agents fall back to the legacy directories if no `SessionOutputManager` is wired (e.g., in tests or standalone usage).
