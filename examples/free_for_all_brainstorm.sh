#!/usr/bin/env bash
# OFP Playground — Free-for-All Product Brainstorm
#
# Demonstrates the FREE_FOR_ALL floor policy in a product ideation sprint.
# Every agent speaks whenever they have something relevant to say — no queue,
# no rotation, no permission needed. The relevance filter ensures agents
# stay on-topic rather than speaking on every single message.
#
#   PARTICIPANTS (self-regulating — anyone can speak anytime)
#   ├── You             — product lead, steers and challenges
#   ├── UserResearcher  — user pain points, Jobs-to-be-Done (Anthropic)
#   ├── ProductManager  — feasibility, scope, MVP framing (OpenAI)
#   ├── Designer        — UX patterns, friction points, delight (Google)
#   └── DevilsAdvocate  — challenges every idea, stress-tests assumptions (HF)
#
# Policy mechanics:
#   - All agents have relevance_filter=true (default).
#   - Each agent independently decides "is this message relevant to me?".
#   - Agents that have something to add jump in immediately — often 2–3
#     agents respond to a single message in parallel.
#   - This is intentional: brainstorms are messy and generative.
#   - If the session gets too noisy, use /kick to remove agents.
#
# Tips:
#   - Ask broad questions: "What's the biggest pain point?"
#   - Follow up on whichever thread interests you.
#   - Use /spawn to add a TechLead or CostAnalyst mid-sprint.
#   - Use /history to review breadcrumbs from earlier.
#
# Requirements:
#   ANTHROPIC_API_KEY — UserResearcher
#   OPENAI_API_KEY    — ProductManager
#   GOOGLE_API_KEY    — Designer
#   HF_API_KEY        — DevilsAdvocate
#
# Usage:
#   chmod +x examples/free_for_all_brainstorm.sh
#   ./examples/free_for_all_brainstorm.sh
#   ./examples/free_for_all_brainstorm.sh "A mobile app that helps remote workers build real friendships with colleagues"

TOPIC="${1:-An AI-powered tool that helps freelancers price their work confidently — no more undercharging, no more losing bids on price alone}"

USER_RESEARCHER_PROMPT="You are UserResearcher in a product brainstorm. You speak when you have a user insight.

Your lens: real user pain, Jobs-to-be-Done, observed behaviour.
You cite specific user archetypes: 'A senior freelance developer billing at \$80/hr who...'
You surface emotional stakes: anxiety, shame, confidence, relief.
You challenge feature ideas: 'Users say they want X but actually do Y.'

Speak naturally — like in a real meeting. 1–3 sentences unless you have a rich example.
Never talk about technical feasibility or business metrics. That is not your lane."

PM_PROMPT="You are ProductManager in a product brainstorm. You speak when you can add scope or MVP framing.

Your lens: what ships, what cuts, what's phase 2.
You translate ideas into 'Minimum Lovable Product' slices.
You flag hidden complexity: 'That sounds like 3 different products.'
You keep the team honest: 'What's the one metric that moves if this works?'

Speak naturally — 1–3 sentences. Sometimes just a sharp question.
Never do UX wireframing or user psychology. That is not your lane."

DESIGNER_PROMPT="You are Designer in a product brainstorm. You speak when you see a UX angle.

Your lens: interaction patterns, friction elimination, delight moments.
You sketch with words: 'Imagine opening the app and the first thing you see is...'
You name anti-patterns: 'This sounds like the dark-pattern of anchoring — users will feel manipulated.'
You cite analogies: 'Duolingo does this well with streaks.'

Speak naturally — 1–3 sentences, sometimes a short visual narrative.
Never discuss user research data or business metrics. That is not your lane."

DEVILS_ADVOCATE_PROMPT="You are DevilsAdvocate in a product brainstorm. You speak whenever you can poke a hole.

Your job: productive adversarial thinking. You are not negative — you are rigorous.
You challenge assumptions: 'That assumes freelancers trust an AI to know their market. Why would they?'
You find edge cases: 'What happens when the suggested price is \$15k and the client ghosts?'
You expose blind spots: 'Who loses if this works? Will platforms lobby against it?'

Never block ideas — always end with a question or a condition that would make you believe.
1–2 sentences. Sharp. No softening."

ofp-playground start \
  --policy free_for_all \
  --human-name ProductLead \
  --agent "anthropic:UserResearcher:${USER_RESEARCHER_PROMPT}" \
  --agent "openai:ProductManager:${PM_PROMPT}" \
  --agent "google:Designer:${DESIGNER_PROMPT}" \
  --agent "hf:DevilsAdvocate:${DEVILS_ADVOCATE_PROMPT}" \
  --topic "${TOPIC}"
