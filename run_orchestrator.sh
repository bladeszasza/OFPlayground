#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
# Showrunner-driven romantic comedy novella for 4-year-olds
# Policy: showrunner_driven — Director assigns chunks, accepts/rejects,
# builds a shared manuscript, and signals TASK_COMPLETE.
# ─────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Configuration ────────────────────────────────────────────────

DIRECTOR_MODEL="zai-org/GLM-5"
WRITER_MODEL="zai-org/GLM-4.7"
COMEDY_MODEL="zai-org/GLM-4.5"
ART_MODEL="Tongyi-MAI/Z-Image"

MAX_TURNS=60

# ── Timeout / retry settings ─────────────────────────────────────
# Director (orchestrator) gets a generous timeout — its responses drive the session.
DIRECTOR_TIMEOUT=180
DIRECTOR_RETRIES=2
# Text workers: 90 s per call, 2 retries on rate-limit / 5xx.
WORKER_TIMEOUT=90
WORKER_RETRIES=2
# Image generation can be slow; give it more time.
IMAGE_TIMEOUT=300
IMAGE_RETRIES=1

MISSION="Create a short romantic comedy novella, featuring 80's junkie skate culture. \
The story should involve twists, and have distinct deep characters. Put emphasis on great jokes, and sophisticated humor. \
Warm, silly skate cultre tone. \
Have a real intriqui a skate contest which they outperform each other and themsevlese as well, in order to choose between the love and glory. \
Create cover art for each chapter and the book and 3 images for every character from different angles. "

# MISSION="Paint a picture of a skater performing a treflip at a skatepark, with a romantic comedy vibe. The skater should have a mischievous grin and be surrounded by adoring fans. The background should feature a sunset and palm trees, giving it a warm, nostalgic feel. The art style should be reminiscent of 80's comic book illustrations, with bold lines and vibrant colors"

# ── Anti-reasoning suffix (appended to every worker prompt) ──────
# GLM models sometimes dump chain-of-thought into output.
# This instruction tells them to skip it.
NO_REASONING="IMPORTANT: Output ONLY the requested creative text. \
Do NOT include any planning, thinking, reasoning, word-counting, or meta-commentary. \
Do NOT start with 'Okay, I need to...' or 'Let me think...'. \
Jump straight into the story text."

# ── Launch ───────────────────────────────────────────────────────

ofp-playground start \
  --no-human \
  --topic "$MISSION" \
  --max-turns "$MAX_TURNS" \
  --policy showrunner_driven \
  \
  --agent "-provider orchestrator \
           -name Director \
           -timeout $DIRECTOR_TIMEOUT \
           -max-retries $DIRECTOR_RETRIES \
           -system $MISSION \
           -model $DIRECTOR_MODEL" \
  \
  --agent "-provider hf \
           -name StoryWriter \
           -max-tokens 1200 \
           -timeout $WORKER_TIMEOUT \
           -max-retries $WORKER_RETRIES \
           -model $WRITER_MODEL \
           -system You write like Seth Rogen or Seth MacFarlane. Write EXACTLY what the Director assigned you — nothing more. $NO_REASONING" \
  \
  --agent "-provider hf \
           -name DialogWriter \
           -max-tokens 800 \
           -timeout $WORKER_TIMEOUT \
           -max-retries $WORKER_RETRIES \
           -model $WRITER_MODEL \
           -system You write funny simple dialogue for sophisticate kinky adult humor. Write EXACTLY what the Director assigned you. $NO_REASONING" \
  \
  --agent "-provider hf \
           -name ComedyBeats \
           -max-tokens 800 \
           -timeout $WORKER_TIMEOUT \
           -max-retries $WORKER_RETRIES \
           -model $COMEDY_MODEL \
           -system You write one silly physical comedy moment. Your writing style resembles Jim Carrey. Write EXACTLY what the Director assigned you. Setup plus punchline. $NO_REASONING" \
  \
  --agent "-provider hf \
           -name CliffWriter \
           -max-tokens 800 \
           -timeout $WORKER_TIMEOUT \
           -max-retries $WORKER_RETRIES \
           -model $COMEDY_MODEL \
           -system You write the best cliffhangers and scene transitions, your style resembles George R R Martin. Write EXACTLY what the Director assigned you. End with oh-no or I-wonder. Oh Fuck! I didnt see it comming! Holly Macaronni! $NO_REASONING" \
  \
  --agent "-provider hf \
           -name Davinci \
           -max-tokens 8000 \
           -timeout $IMAGE_TIMEOUT \
           -max-retries $IMAGE_RETRIES \
           -type Text-to-Image \
           -model $ART_MODEL \
           -system Your art style resembles Stan Lee. Paint EXACTLY what the Director assigned you. Use texture shaded style with bold colors and dynamic lighting. $NO_REASONING"