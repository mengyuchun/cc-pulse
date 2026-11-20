---
name: cc-pulse
description: Zero-dependency CLI health check, capability probe, and authenticity sentinel for Claude Code / Codex / OpenAI-compatible API relays. Use when the user asks why Claude Code isn't working, wants to verify an API key or relay endpoint, suspects silent model downgrades/cross-protocol proxying, or wants a shareable relay evaluation card.
---

# CC-Pulse Relay & API Key Diagnostic Skill

Use `cc-pulse` (or `python src/cc_pulse/cli.py` if running from a local checkout) to diagnose API endpoints safely without printing raw API keys.

## Recommended Progressive Workflow

1. **Zero-Key First — Check Environment Overrides**
   Run `env-check` before touching any API key to catch stale shell environment variables overriding `~/.claude/settings.json`, `~/.codex/config.toml`, or `cc-switch`:
   ```bash
   cc-pulse env-check --json
   ```

2. **Quick Health Check (No `cc-switch.db` Required)**
   - For a single endpoint/key (pass the **env var name**, never raw secrets on CLI):
     ```bash
     cc-pulse check --base-url https://relay.example.com --api-key-env ANTHROPIC_AUTH_TOKEN --model claude-sonnet-4-5 --json
     ```
   - Or auto-discover local `~/.claude/settings.json`, `~/.codex/config.toml`, `~/.cc-pulse/providers.toml`, or `cc-switch.db`:
     ```bash
     cc-pulse check --failover-only --json
     ```

3. **Deep Model Inspection & Shareable Card**
   Diagnose text, streaming/TTFT, thinking suppression, native `tool_use`, cross-pack spoofing, thinking signature presence, usage consistency, and cloud backend origin (`Official` / `AWS Bedrock` / `Google Vertex`):
   ```bash
   cc-pulse inspect --base-url https://relay.example.com --api-key-env ANTHROPIC_AUTH_TOKEN --model claude-sonnet-4-5 --format card
   ```
   Use `--format json` when you need full structured fields to reason over.

## Safety Rules
- Never pass a raw secret key literal on `--api-key-env`; pass the environment variable name (e.g. `ANTHROPIC_AUTH_TOKEN`).
- Never paste unmasked keys from configs into responses.
- Keep `cc-pulse` strictly read-only toward `cc-switch.db`.
