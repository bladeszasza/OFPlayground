# OFP Playground Documentation

Complete documentation for the **OFP Playground** — a CLI tool for multi-agent conversations using the [Open Floor Protocol (OFP)](https://github.com/open-voice-interoperability/openfloor-python).

## Contents

| Document | Description |
|----------|-------------|
| [Architecture Overview](architecture.md) | System design, message flow, component diagram |
| [Open Floor Protocol](ofp-protocol.md) | OFP envelope structure, events, and how the playground implements the spec |
| [Floor Policies](floor-policies.md) | All 5 floor-control policies with cross-references and use cases |
| [Agents Guide](agents.md) | Complete agent taxonomy, hierarchy, and per-agent details |
| [Orchestration Patterns](orchestration.md) | SHOWRUNNER_DRIVEN pipeline, directives, breakout sessions |
| [CLI Reference](cli.md) | All commands, flags, agent spec formats, slash commands |
| [Configuration](configuration.md) | API keys, model defaults, config file, environment variables |
| [Output Structure](output.md) | Per-conversation result directories and artifact types |

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Simple two-agent chat
ofp-playground start \
  --agent "anthropic:Alice:You are a curious scientist." \
  --agent "openai:Bob:You are a skeptical engineer." \
  --topic "Should we colonize Mars?"

# Full production pipeline (see examples/showcase.sh)
bash examples/showcase.sh
```
