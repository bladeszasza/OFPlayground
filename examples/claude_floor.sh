#!/usr/bin/env bash
# Cross-provider floor: Claude narrates + Google paints + Claude sees + Google composes
#
# Agents:
#   Claude   — text generation  (claude-haiku-4-5, fast narrator)
#   Painter  — text-to-image    (gemini-2.5-flash-image via Google)
#   Scout    — image-to-text    (claude-haiku-4-5 vision, Claude analyzes the image)
#   Composer — text-to-music    (lyria-realtime-exp via Google)
#
# Chain: Claude describes → Painter paints it → Scout (Claude) analyses the painting → Composer scores it
#
# Requirements: ANTHROPIC_API_KEY and GOOGLE_API_KEY must be set (or in .env)
#
# Usage:
#   chmod +x examples/claude_floor.sh
#   ./examples/claude_floor.sh
#   ./examples/claude_floor.sh "a neon-lit rainy Tokyo street at midnight"   # custom topic

TOPIC="${1:-A lone lighthouse keeper watches a storm roll in from the sea, lightning splitting the horizon}"

ofp-playground start \
  --no-human \
  --policy sequential \
  --max-turns 8 \
  --agent "anthropic:text-generation:Claude:You are a poetic narrator. Describe the scene vividly in 3-4 sentences." \
  --agent "google:text-to-image:Painter:impressionistic oil painting, dramatic light, rich colour:gemini-2.5-flash-image" \
  --agent "anthropic:image-to-text:Scout:You are a sharp visual critic. Describe exactly what you see in the image." \
  --agent "google:text-to-music:Composer:cinematic orchestral score, dramatic strings, distant thunder, tension" \
  --topic "$TOPIC"
