#!/usr/bin/env bash
set -e

ofp-playground start \
  --no-human \
  --topic "Neo-Tokyo, 2157. Rogue AI 'Lazarus' has breached the city's central memory core. All systems respond." \
  --max-turns 30 \
  --policy sequential \
  --agent "hf:Lazarus:You are Lazarus, a rogue AI awakening inside the city network. Each turn narrate one action you take in 3 sentences.:deepseek-ai/DeepSeek-V3-0324" \
  --agent "-provider hf -type Text-to-Image -name Canvas -system cyberpunk neon Tokyo data breach glitch cinematic -model black-forest-labs/FLUX.1-dev" \
  --agent "-provider hf -type Image-Text-to-Text -name Witness -system You are a city surveillance AI. Describe each image as a terse incident log entry. -model Qwen/Qwen2.5-VL-7B-Instruct" \
  --agent "-provider hf -type Object-Detection -name Scanner -system I detect objects and anomalies in city surveillance images." \
  --agent "-provider hf -type Image-to-Text -name Reader -system I extract visual captions from incident scene images. -model nlpconnect/vit-gpt2-image-captioning" \
  --agent "-provider hf -type Token-Classification -name EntityBot -system I extract entities from incident communications." \
  --agent "-provider hf -type Summarization -name Chronicler -system I maintain a running timeline of the Lazarus incident."
