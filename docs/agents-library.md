# Agent Library — SOUL.md Personas

The `agents/` directory at the repo root is a curated library of 200+ agent personas. Each persona is a `SOUL.md` file — a structured system prompt that defines an agent's identity, operating principles, and output format.

## Structure

```
agents/
└── <category>/
    └── <agent-name>/
        └── SOUL.md
```

Every directory name is part of the slug. `agents/development/code-reviewer/SOUL.md` is addressable as `@development/code-reviewer`.

## Using a Persona (@slug syntax)

Reference a persona anywhere you'd write a system prompt:

```bash
# Flag format (-system)
ofp-playground start \
  --agent "-provider anthropic -name Alice -system @creative/brand-designer"

# Colon format (description field)
ofp-playground start \
  --agent "anthropic:Alice:@creative/brand-designer"

# Mix multiple agents
ofp-playground start \
  --agent "anthropic:Alice:@development/code-reviewer" \
  --agent "google:Bob:@development/bug-hunter"
```

The `@slug` is resolved at agent construction time by [`src/ofp_playground/agents/library.py`](../src/ofp_playground/agents/library.py), which reads the SOUL.md file and substitutes its content as the system prompt. The full library is cached for the process lifetime.

```bash
ofp-playground agents    # list all available slugs
```

## Categories

| Category | Agents | Example slugs |
|----------|-------:|---------------|
| `marketing` | 28 | `@marketing/seo-writer`, `@marketing/newsletter`, `@marketing/social-media` |
| `development` | 32 | `@development/coding-agent`, `@development/code-reviewer`, `@development/bug-hunter` |
| `business` | 14 | `@business/sales-assistant`, `@business/customer-support`, `@business/radar` |
| `creative` | 13 | `@creative/brand-designer`, `@creative/copywriter`, `@creative/storyboard-writer` |
| `finance` | 10 | `@finance/trading-bot`, `@finance/fraud-detector`, `@finance/tax-preparer` |
| `devops` | 10 | `@devops/incident-responder`, `@devops/deploy-guardian`, `@devops/log-analyzer` |
| `productivity` | 9 | `@productivity/meeting-notes`, `@productivity/inbox-zero`, `@productivity/daily-standup` |
| `data` | 9 | `@data/sql-assistant`, `@data/etl-pipeline`, `@data/dashboard-builder` |
| `hr` | 8 | `@hr/recruiter`, `@hr/resume-screener`, `@hr/performance-reviewer` |
| `education` | 8 | `@education/tutor`, `@education/quiz-maker`, `@education/curriculum-designer` |
| `personal` | 7 | `@personal/travel-planner`, `@personal/fitness-coach`, `@personal/daily-planner` |
| `healthcare` | 7 | `@healthcare/symptom-triage`, `@healthcare/wellness-coach`, `@healthcare/meal-planner` |
| `ecommerce` | 7 | `@ecommerce/pricing-optimizer`, `@ecommerce/product-lister`, `@ecommerce/review-responder` |
| `security` | 6 | `@security/threat-monitor`, `@security/vuln-scanner`, `@security/phishing-detector` |
| `saas` | 6 | `@saas/product-scrum`, `@saas/churn-prevention`, `@saas/onboarding-flow` |
| `legal` | 6 | `@legal/contract-reviewer`, `@legal/nda-generator`, `@legal/patent-analyzer` |
| `automation` | 6 | `@automation/overnight-coder`, `@automation/morning-briefing`, `@automation/negotiation-agent` |
| `real-estate` | 5 | `@real-estate/listing-scout`, `@real-estate/market-analyzer`, `@real-estate/lead-qualifier` |
| `freelance` | 4 | `@freelance/proposal-writer`, `@freelance/upwork-proposal`, `@freelance/client-manager` |
| `compliance` | 4 | `@compliance/gdpr-auditor`, `@compliance/soc2-preparer`, `@compliance/risk-assessor` |
| `voice` | 3 | `@voice/interview-bot`, `@voice/phone-receptionist`, `@voice/voicemail-transcriber` |
| `supply-chain` | 3 | `@supply-chain/vendor-evaluator`, `@supply-chain/route-optimizer`, `@supply-chain/inventory-forecaster` |
| `moltbook` | 3 | `@moltbook/scout`, `@moltbook/growth-agent`, `@moltbook/community-manager` |
| `customer-success` | 2 | `@customer-success/onboarding-guide`, `@customer-success/nps-followup` |

## `development/` — Coding-Aware Personas

The `development/` category contains personas with embedded software engineering methodology (TDD, systematic debugging, verification discipline). These are used automatically by coding agents when no system prompt is provided.

| Slug | Auto-loaded by | What it encodes |
|------|---------------|-----------------|
| `@development/coding-agent` | `BaseCodingAgent` (all providers) | Senior dev identity; Red-Green-Refactor rhythm; 4-phase debugging; verification-before-done contract |
| `@development/tdd-expert` | — | Iron Law (no prod code without failing test); RED/GREEN/REFACTOR cycle; rationalizations table |
| `@development/technical-planner` | — | File-first decomposition; bite-sized tasks; no-placeholders rule; YAGNI |
| `@development/tech-lead` | — | Decompose→delegate→review→integrate; two-stage review (spec + quality); quality gates |
| `@development/code-reviewer` | — | Plan alignment review; architecture checklist; when-to-invoke methodology; review output format |
| `@development/bug-hunter` | — | 4-phase systematic debugging; root-cause-before-fix; 3-fix architectural trigger |
| `@development/test-writer` | — | TDD core; red-green-refactor steps; test quality checklist |
| `@development/qa-tester` | — | Verification before completion; 5-step gate; evidence before assertions |
| `@development/pr-merger` | — | Branch completion options; git worktrees guide; safety rules |

## OFPlayground Research Experts

Four development personas cover Open Floor Protocol, this repository, multi-agent communication, and emergent communication. See the [expert panel guide](expert-agents.md) for their slugs, research sources, CLI/Canvas launch commands, and runtime limitations. The [July 2026 Hugging Face brief](research/hugging-face-2026-communication.md) explains the incident evidence behind the emergent-communication persona.

## Writing a New Persona

Create the directory and file:

```
agents/<category>/<agent-name>/SOUL.md
```

The file format is free-form Markdown. The library loader extracts the display name from the first meaningful heading (strips `# `, `SOUL.md — `, `Agent: ` prefixes). There is no required frontmatter.

The slug is immediately available without any registration step — the library rescans on the next process start (or call `ofp_playground.agents.library.load.cache_clear()` in tests to force a reload).

## Loading API

```python
from ofp_playground.agents.library import load, resolve_slug

# Full index: {slug: {slug, category, name, display_name, content}}
index = load()

# Resolve a single slug to its SOUL.md content
system_prompt = resolve_slug("@development/code-reviewer")
```
