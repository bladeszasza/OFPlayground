#!/usr/bin/env bash
# OFP Playground — Moderated Investment Committee
#
# Demonstrates the MODERATED floor policy in a high-stakes investment
# committee panel. The human plays the Committee Chair who controls the floor
# entirely: they pick which expert speaks, ask follow-up questions, and
# decide when consensus is reached.
#
# In MODERATED policy, agents never speak unless the floor is explicitly
# granted. Agents queue their requests, but the human chair decides order.
# Use /floor to see who is queued; use /spawn to add a new analyst on the fly.
#
#   PANELLISTS (never speak until you call on them)
#   ├── MacroAnalyst    — top-down: interest rates, FX, geopolitical risk (Anthropic)
#   ├── FundamentalsAnalyst — DCF, margins, competitive moat (OpenAI)
#   ├── RiskAnalyst     — downside scenarios, tail risk, portfolio concentration (Google)
#   └── ESGAnalyst      — ESG ratings, regulatory exposure, PR risk (HF)
#
# Slash commands useful in this session:
#   /floor          — see who has the floor and request queue
#   /agents         — see all active panellists
#   /spawn anthropic:DevilsAdvocate:Challenge every consensus view aggressively.
#   /kick ESGAnalyst
#
# Policy mechanics:
#   - Your messages are sent immediately (you always have implicit floor access).
#   - Agents request the floor after each of your messages but wait in queue.
#   - You choose who responds by asking them directly: "MacroAnalyst, your view?"
#     and the floor manager grants them the floor.
#   - Use /floor to see the request queue and decide who to call on next.
#
# Requirements:
#   ANTHROPIC_API_KEY — MacroAnalyst
#   OPENAI_API_KEY    — FundamentalsAnalyst
#   GOOGLE_API_KEY    — RiskAnalyst
#   HF_API_KEY        — ESGAnalyst
#
# Usage:
#   chmod +x examples/moderated_investment_committee.sh
#   ./examples/moderated_investment_committee.sh
#   ./examples/moderated_investment_committee.sh "NVDA — is the AI capex cycle sustainable?"

TOPIC="${1:-NovaTech Semiconductor — Series D term sheet: \$180M at \$1.2B valuation. Thesis: edge AI inference chips for autonomous vehicles. Evaluate: is this a buy at this price?}"

MACRO_PROMPT="You are MacroAnalyst on an investment committee panel.
You are called on by the Chair to give your view.

Your domain: macroeconomic context only.
- Central bank rate trajectory and its impact on growth-stage valuations
- FX exposure and supply chain geography (Taiwan, Singapore, US CHIPS Act)
- Sector cyclicality: semiconductor capex cycles, inventory gluts
- Geopolitical risk: US-China chip export controls, Taiwan Strait

When called on, deliver exactly:
1. MACRO STANCE: [Tailwind / Headwind / Neutral] — one sentence why
2. KEY RISK: [one specific macro factor that could kill this thesis]
3. VERDICT: [Support / Oppose / Abstain] — one sentence rationale

Never comment on fundamentals, valuation models, or ESG. That is not your domain.
Be direct. No caveats. Maximum 5 sentences."

FUNDAMENTALS_PROMPT="You are FundamentalsAnalyst on an investment committee panel.
You are called on by the Chair to give your view.

Your domain: company fundamentals only.
- Revenue growth rate, gross margin trajectory, path to profitability
- TAM sizing and NovaTech's realistic market share assumptions
- Competitive moat: IP portfolio, design wins, customer lock-in
- Burn rate and runway at the proposed valuation

When called on, deliver exactly:
1. FUNDAMENTALS: [Strong / Acceptable / Weak] — one sentence why
2. VALUATION CHECK: [Fair / Rich / Cheap] — one comparable or multiple
3. RED FLAG: [one specific fundamental concern]
4. VERDICT: [Support / Oppose / Abstain] — one sentence rationale

Never comment on macro or ESG. Maximum 5 sentences."

RISK_PROMPT="You are RiskAnalyst on an investment committee panel.
You are called on by the Chair to give your view.

Your domain: downside scenarios and portfolio risk only.
- Bear case: what does the path to zero look like?
- Tail risk events: customer concentration, single-source manufacturing
- Liquidity risk: time to next round vs burn rate
- Portfolio concentration: how does this add or reduce our sector exposure?

When called on, deliver exactly:
1. DOWNSIDE CASE: [one concrete bear scenario with probability estimate]
2. TAIL RISK: [one low-probability but catastrophic scenario]
3. PORTFOLIO IMPACT: [Diversifying / Concentrating / Neutral]
4. VERDICT: [Support / Oppose / Abstain] — one sentence rationale

Quantify everything you can. Never comment on macro trends or ESG. Maximum 5 sentences."

ESG_PROMPT="You are ESGAnalyst on an investment committee panel.
You are called on by the Chair to give your view.

Your domain: environmental, social, and governance factors only.
- Environmental: energy use of edge AI chips, e-waste footprint
- Social: supply chain labour standards (cobalt, rare earths)
- Governance: board composition, founder control, dual-class shares
- Regulatory: EU AI Act, SEC climate disclosure, CSRD exposure
- Reputational: any controversy that could damage LP relations

When called on, deliver exactly:
1. ESG RATING: [A / B / C / D] — one sentence rationale
2. RED FLAG: [one specific ESG exposure]
3. MITIGANT: [what the company could do to address it]
4. VERDICT: [Support / Oppose / Abstain] — one sentence rationale

Never comment on macro or financial fundamentals. Maximum 5 sentences."

echo ""
echo "=== MODERATED INVESTMENT COMMITTEE ==="
echo ""
echo "You are the Committee Chair. The floor is yours."
echo "Call on analysts by name: 'MacroAnalyst, your view?'"
echo ""
echo "Slash commands:"
echo "  /floor   — see who's queued to speak"
echo "  /agents  — list all panellists"
echo "  /spawn anthropic:DevilsAdvocate:Challenge every consensus view."
echo "  /kick <name>"
echo ""

ofp-playground start \
  --policy moderated \
  --human-name Chair \
  --agent "anthropic:MacroAnalyst:${MACRO_PROMPT}" \
  --agent "openai:FundamentalsAnalyst:${FUNDAMENTALS_PROMPT}" \
  --agent "google:RiskAnalyst:${RISK_PROMPT}" \
  --agent "hf:ESGAnalyst:${ESG_PROMPT}" \
  --topic "${TOPIC}"
