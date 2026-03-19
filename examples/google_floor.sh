#!/usr/bin/env bash
# Full Google floor — all four Gemini capabilities, sequential policy, no human.
#
# Agents:
#   Gemini   — text generation  (gemini-3.1-flash-lite-preview)
#   Painter  — text-to-image    (gemini-3.1-flash-image-preview, fallback: gemini-2.5-flash-image)
#   Scout    — image-to-text    (gemini-3-flash-preview)
#   Composer — text-to-music    (lyria-realtime-exp)
#
# The seed topic triggers all four:
#   Gemini describes the scene in text → Painter paints it → Scout analyses the image → Composer scores it.
#
# Requirements: GOOGLE_API_KEY must be set (or in .env)
#
# Usage:
#   chmod +x examples/google_floor.sh
#   ./examples/google_floor.sh
#   ./examples/google_floor.sh "a stormy ocean at midnight"   # custom topic

TOPIC="${1:-A serene Japanese garden at dawn — the mist lifts slowly, cherry blossoms fall, a lone crane stands motionless by the pond}"

ofp-playground start \
  --no-human \
  --policy sequential \
  --max-turns 8 \
  --agent "google:text-generation:Gemini:You are a poetic narrator. Describe the scene vividly in 3-4 sentences." \
  --agent "google:text-to-image:Painter:impressionistic watercolour, soft light, fine detail:gemini-2.5-flash-image" \
  --agent "google:image-to-text:Scout:You are a visual analyst. Describe what you see in the image concisely." \
  --agent "google:text-to-music:Composer:ambient cinematic score, soft piano, gentle strings, meditative" \
  --topic "$TOPIC"
