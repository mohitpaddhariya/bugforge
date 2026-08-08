"""bugforge triage agent — the deterministic half of the bug-triage skill.

Harness-agnostic by construction: the only capabilities assumed of the host are
reading files, writing files, and running shell commands. Everything else is this
CLI. No MCP server, no subagents, no harness-specific tooling.
"""

__version__ = "0.1.0"
