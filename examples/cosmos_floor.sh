#!/usr/bin/env bash
# Cosmos Floor — multi-agent deep-dive on a cosmology topic.
#
# Agents:
#   Narrator   — HuggingFace (text)        — frames the question and synthesizes findings
#   Stella     — Remote OFP (NASA images) — retrieves real astronomical imagery
#   ArXiv      — Remote OFP (arXiv)       — surfaces the latest research papers
#   Wikipedia  — Remote OFP (Wikipedia)   — provides authoritative background context
#   Verity     — Remote OFP (fact-check)  — checks claims and flags hallucinations
#
# Policy: sequential — each agent contributes one turn before the next takes the floor.
# No human needed — fully autonomous.
#
# Requirements: HF_API_KEY must be set (or in .env)
# Swap --agent line to anthropic:Narrator if you want Claude instead of HF.
#
# Usage:
#   chmod +x examples/cosmos_floor.sh
#   ./examples/cosmos_floor.sh
#   ./examples/cosmos_floor.sh "What do the latest JWST observations reveal about the early universe?"

TOPIC="${1:-What do the latest James Webb Space Telescope observations tell us about galaxy formation in the first 500 million years after the Big Bang, and how does this challenge our current cosmological models?}"

ofp-playground start \
  --no-human \
  --policy sequential \
  --max-turns 10 \
  --agent "hf:Narrator:You are a science communicator specializing in astrophysics and cosmology. Your role is to frame the central question clearly, interpret findings from the other agents, and synthesize everything into a coherent narrative accessible to an educated general audience. Highlight tensions with current models." \
  --remote stella \
  --remote arxiv \
  --remote wikipedia \
  --remote verity \
  --topic "$TOPIC"
