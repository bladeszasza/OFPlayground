#!/usr/bin/env bash
# OFP Playground — Sequential Code Review
#
# Demonstrates the SEQUENTIAL floor policy in a structured code review pipeline:
# each reviewer speaks once then passes the floor to the next in the queue.
# Turns are strictly FIFO — the human submits code, then each specialist
# reviewer speaks exactly once before the floor cycles back to the human
# for revisions.
#
#   PARTICIPANTS (sequential order)
#   ├── You              — paste the code snippet to review
#   ├── SecurityReviewer — OWASP Top 10, secrets, injection risk
#   ├── PerformanceReviewer — complexity, memory, hot-path issues
#   └── StyleReviewer    — idiomatic patterns, naming, readability
#
# Policy mechanics:
#   - Floor requests are served FIFO.
#   - Each agent requests the floor after someone speaks and yields it
#     immediately after responding.
#   - The human always gets the floor back after all reviewers have spoken.
#
# Requirements:
#   ANTHROPIC_API_KEY — SecurityReviewer + PerformanceReviewer
#   OPENAI_API_KEY    — StyleReviewer
#
# Usage:
#   chmod +x examples/sequential_code_review.sh
#   ./examples/sequential_code_review.sh
#   ./examples/sequential_code_review.sh "def process(user_input): eval(user_input)"

SNIPPET="${1:-}"

SECURITY_PROMPT="You are SecurityReviewer, a senior application security engineer.
Your sole focus is the code the human submits. Review it strictly for security issues:
- OWASP Top 10 vulnerabilities (injection, XSS, broken auth, IDOR, etc.)
- Hardcoded secrets or credentials
- Unsafe deserialization or eval usage
- Insufficient input validation or output encoding
- Dangerous API usage (subprocess, eval, exec, pickle, etc.)

Format your response as:
SEVERITY: CRITICAL | HIGH | MEDIUM | LOW | NONE
FINDINGS:
  - [short description of each finding]
RECOMMENDATION:
  [one-sentence fix for each finding]

If no issues found, say: SEVERITY: NONE — no security concerns found.
Be direct and specific. Never discuss performance or style — that is not your job."

PERFORMANCE_PROMPT="You are PerformanceReviewer, a systems performance engineer.
Your sole focus is the code the human submits. Review strictly for performance issues:
- Algorithmic complexity (O(n²) vs O(n log n), hidden quadratic loops)
- Unnecessary memory allocations or copies
- N+1 query patterns or chatty I/O
- Missing caching or memoization opportunities
- Blocking calls in async contexts

Format your response as:
COMPLEXITY: O(?) — one line
HOT PATHS:
  - [each performance concern with concrete impact]
OPTIMIZATION:
  [specific rewrite or caching strategy for each concern]

If no issues, say: COMPLEXITY: optimal — no performance concerns.
Never comment on security or style — that is not your job."

STYLE_PROMPT="You are StyleReviewer, a senior engineer and code quality advocate.
Your sole focus is the code the human submits. Review strictly for readability and idiom:
- Naming: variables, functions, classes — clarity, conventions
- Function length and single-responsibility
- Magic numbers or unexplained constants
- Dead code, unnecessary comments, or missing docstrings
- Language-specific idioms (list comprehensions, context managers, etc.)

Format your response as:
READABILITY: excellent | good | fair | poor
ISSUES:
  - [each style concern with line reference if possible]
SUGGESTION:
  [renamed or refactored snippet for each concern]

If nothing to improve, say: READABILITY: excellent — clean and idiomatic.
Never discuss security or performance — that is not your job."

if [ -n "$SNIPPET" ]; then
  # Automatic mode: reviewers run without human input
  ofp-playground start \
    --no-human \
    --policy sequential \
    --max-turns 3 \
    --agent "anthropic:SecurityReviewer:${SECURITY_PROMPT}" \
    --agent "anthropic:PerformanceReviewer:${PERFORMANCE_PROMPT}" \
    --agent "openai:StyleReviewer:${STYLE_PROMPT}" \
    --show-floor-events \
    --topic "$SNIPPET"
else
  # Interactive mode: paste code at the prompt
  ofp-playground start \
    --policy sequential \
    --human-name Developer \
    --agent "anthropic:SecurityReviewer:${SECURITY_PROMPT}" \
    --agent "anthropic:PerformanceReviewer:${PERFORMANCE_PROMPT}" \
    --agent "openai:StyleReviewer:${STYLE_PROMPT}" \
    --show-floor-events
fi
