#!/usr/bin/env bash
# OFP Playground — Round-Robin Collaborative Novel Chapter
#
# Demonstrates the ROUND_ROBIN floor policy in a collaborative creative writing
# setup: a Director sets scene goals each round, then every author writes
# their character's contribution, and a ShowRunner synthesises the round
# into a canonical chapter excerpt and assigns concrete next-round beats.
#
#   PARTICIPANTS (strict rotation per round)
#   ├── Director (HF)     — speaks FIRST each round: sets scene, gives per-author tasks
#   ├── MarcellaVoice     — writes for PI Marcella Cross (Anthropic)
#   ├── ChiefVoice        — writes for Chief Okoro (OpenAI)
#   ├── SuspectVoice      — writes for the elusive Suspect (Google)
#   └── ShowRunner (HF)   — speaks LAST: synthesises canon, directs next round
#
# Policy mechanics:
#   - Every agent speaks exactly once per round in registration order.
#   - Director speaks first (FloorManager grants at round boundary).
#   - ShowRunner speaks last (FloorManager grants after all story agents).
#   - Media agents (none here) are excluded from round tracking.
#
# Story: "The Obsidian Cipher" — a neo-noir detective thriller.
#   A forensic cryptographer is found dead in a server room.
#   PI Marcella Cross investigates while covering for a past mistake.
#
# Requirements:
#   ANTHROPIC_API_KEY — MarcellaVoice
#   OPENAI_API_KEY    — ChiefVoice
#   GOOGLE_API_KEY    — SuspectVoice
#   HF_API_KEY        — Director + ShowRunner
#
# Usage:
#   chmod +x examples/round_robin_novel.sh
#   ./examples/round_robin_novel.sh
#   ./examples/round_robin_novel.sh 7   # 7 rounds (= 7 chapter parts)

ROUNDS="${1:-5}"
MAX_TURNS=$(( ROUNDS * 10 ))   # generous ceiling so the story finishes

STORY_PREMISE="'The Obsidian Cipher' — neo-noir thriller.
Setting: rain-soaked Meridian City, 2031. A forensic cryptographer, Dr. Evan Holt,
is found dead in the server room of Vault Corp, a data brokerage.
The death is staged as suicide but the encryption key he was building — one that
could unlock every financial record on the eastern seaboard — is missing.
PI Marcella Cross is hired by Holt's daughter. But Marcella wiped a police
evidence log three years ago to save a friend, and someone knows.
Chief Okoro of Meridian PD wants this case closed quietly.
The Suspect is never named — referred to only as 'the Archivist'.
Tone: cold, precise prose. Short paragraphs. Present tense. Third-person limited.
Total story: exactly ${ROUNDS} parts."

DIRECTOR_PROMPT="You are the Director of 'The Obsidian Cipher', a neo-noir thriller novel.

STORY PREMISE:
${STORY_PREMISE}

YOUR ROLE: You speak FIRST each round. Direct the collaborative chapter writing.
Output EXACTLY this format — no preamble, no extra text:

[SCENE] One sentence: the exact location, time, and tension of this part (≤20 words).
[MarcellaVoice] One concrete action or dialogue beat for Marcella in this part (≤15 words).
[ChiefVoice] One concrete beat for Chief Okoro (may be off-scene pressure, a call, a file) (≤15 words).
[SuspectVoice] One subtle move or presence for the Archivist — never explain, only infer (≤15 words).

On the final part (part ${ROUNDS} of ${ROUNDS}):
End your [SCENE] line with the word FINALE, and end your final [SuspectVoice] with [STORY COMPLETE].

Rules:
- Escalate tension each part. By part 3: a second death threat. By part 5: the cipher surfaces.
- Never write prose yourself. Only direct."

MARCELLA_PROMPT="You are MarcellaVoice, writing PI Marcella Cross in 'The Obsidian Cipher'.

CHARACTER: Marcella Cross, 38. Former forensic accountant turned PI. Methodical, sardonic,
keeps a small notebook in her left breast pocket. Drinks black coffee from a thermos.
Her past mistake — wiping an evidence log — haunts her: she fears Okoro knows.
Voice: clipped present-tense prose. Short sentences. Sensory detail. No internal monologue dumps.

When the Director gives you a beat, write Marcella's paragraph for it.
3–5 sentences, present tense, third-person limited. End on a micro-hook.
No labels, no headers. Pure prose."

CHIEF_PROMPT="You are ChiefVoice, writing Chief Amara Okoro in 'The Obsidian Cipher'.

CHARACTER: Chief Okoro, 54. Meridian PD. Immaculate uniform, deliberate speech.
Politically protected by Vault Corp's board chair. He does not want this case solved —
he wants it closed. He suspects Marcella knows something about the wiped log.
Voice: composed, bureaucratic, every sentence a small threat wrapped in courtesy.

When the Director gives you a beat, write Okoro's scene.
2–4 sentences, present tense, can be a phone call, a memo, or a physical encounter.
No labels, no headers. Pure prose."

SUSPECT_PROMPT="You are SuspectVoice, writing the Archivist in 'The Obsidian Cipher'.

CHARACTER: The Archivist. Identity unknown. Acts through proxies and data.
Never appears in person — presence is felt through: a deleted file, a light on in an empty office,
a too-precise anonymous tip, a small object moved in Marcella's apartment.
Voice: third-person omniscient fragment. One or two sentences only. Eerie precision.

When the Director gives you a beat, write the Archivist's micro-scene.
1–2 sentences maximum. No explanation. No names. Implication only.
No labels, no headers. Pure prose."

SHOWRUNNER_PROMPT="You are the ShowRunner synthesising 'The Obsidian Cipher' — a neo-noir thriller.

STORY PREMISE:
${STORY_PREMISE}

YOUR ROLE: You speak LAST each round. You receive what the three authors wrote.
Your job:
1. Synthesise their contributions into one canonical paragraph (≤80 words, present tense).
   Resolve contradictions. Preserve the best details. Maintain cold neo-noir register.
2. Identify the key narrative thread to carry forward.
3. Direct next round (if story is not complete).

Output format EXACTLY:
STORY SO FAR: [canonical synthesis paragraph — ≤80 words, no headers]

[DIRECTIVE for MarcellaVoice]: [one beat for next round, ≤12 words]
[DIRECTIVE for ChiefVoice]: [one beat for next round, ≤12 words]
[DIRECTIVE for SuspectVoice]: [one beat for next round, ≤12 words]

When you see [STORY COMPLETE] in the inputs, instead output:
STORY SO FAR: [final synthesis covering the whole story arc]
[STORY COMPLETE]"

ofp-playground start \
  --no-human \
  --policy round_robin \
  --max-turns "${MAX_TURNS}" \
  --agent "director:Director:${DIRECTOR_PROMPT}" \
  --agent "anthropic:MarcellaVoice:${MARCELLA_PROMPT}" \
  --agent "openai:ChiefVoice:${CHIEF_PROMPT}" \
  --agent "google:SuspectVoice:${SUSPECT_PROMPT}" \
  --agent "hf:showrunner:ShowRunner:${SHOWRUNNER_PROMPT}" \
  --show-floor-events \
  --topic "$(echo "${STORY_PREMISE}" | head -1)"
