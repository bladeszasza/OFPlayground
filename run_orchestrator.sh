#!/usr/bin/env bash
# Orchestrator-driven romantic comedy novella for 4-year-olds with lemurs
# Policy: showrunner_driven — Orchestrator assigns one chunk at a time,
# accepts contributions into a shared manuscript, and signals TASK_COMPLETE.
set -e

MISSION="Create a short romantic comedy novella for 4-year-olds featuring lemurs. \
The story must have a Beginning (two lemurs meet in the jungle), a Middle (a funny \
misunderstanding keeps them apart), and an Ending (they make up and share a mango). \
Total target: 600-800 words. Warm, silly tone. Simple vocabulary."

ofp-playground start \
  --no-human \
  --topic "$MISSION" \
  --max-turns 60 \
  --policy showrunner_driven \
  --agent "-provider orchestrator -name Director -system $MISSION -model openai/gpt-oss-120b" \
  --agent "-provider hf -name StoryWriter -system You write warm simple prose for 4-year-olds. Write EXACTLY what the Director assigned you — nothing more. Short sentences. Vivid jungle details. Friendly animals. -max-tokens 1200 -model zai-org/GLM-5" \
  --agent "-provider hf -name DialogWriter -system You write funny simple dialogue for 4-year-olds. Write EXACTLY what the Director assigned you. Lemurs speak in short excited sentences. Lots of oh-no and tee-hee. -max-tokens 600 -model moonshotai/Kimi-K2-Instruct-0905" \
  --agent "-provider hf -name ComedyBeats -system You write one silly physical comedy moment. Write EXACTLY what the Director assigned you. Setup plus punchline. Lemurs trip bump tails or drop fruit. -max-tokens 300 -model openai/gpt-oss-20b" \
  --agent "-provider hf -name CliffWriter -system You write gentle scene transitions or cliffhangers for 4-year-olds. Write EXACTLY what the Director assigned you. End with oh-no or I-wonder. -max-tokens 300 -model openai/gpt-oss-20b"
