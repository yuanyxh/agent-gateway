"""Shared implementation for calling the agent CLI: command template + placeholder substitution, with zero parameter assumptions.

Different agent CLIs have wildly different launch flags (claude uses `-p`,
others may not), so the script appends no fixed parameters -- it only does
"write a temp file + substitute placeholders"; the final command is entirely
determined by the template passed in by the caller.

Template placeholders:
- {system_prompt_file}: path of the temp file the system text is written to (required)
- {prompt_file}: path of the temp file the user text is written to (if the
  template contains it, use the file; otherwise user is passed via stdin,
  compatible with `claude -p` reading the prompt from stdin)

Example template (claude -p):
    claude -p --output-format text --append-system-prompt-file {system_prompt_file}
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time

from agent_gateway.core.log_utils import setup_logging

logger = setup_logging("agent_call")

# Timeout for a single agent call (seconds). MCP retrieval rounds are not
# limited (quality first); digging deep into the memory store + writing long
# text can take a while overall. The 25-minute cap prevents "infinite hang":
# on timeout the process is killed and an error is raised, avoiding a task
# hanging for hours.
# Overridable via env var AGENT_TIMEOUT (the Swift layer reads it from
# GeneralConfig.aiTimeout)
AGENT_TIMEOUT = int(os.environ.get("AGENT_TIMEOUT", "1500"))


def _render_template(template: str, values: dict) -> str:
    """Replaces only **registered** placeholders; other braces are left as-is.

    `str.format` cannot be used: the command template is a user config item
    (ArticleConfig.agentCli) and may well contain JSON arguments
    (`--mcp-config {"mcpServers":{}}`) or shell variables (`${HOME}`). format
    would parse them as field names and raise KeyError outright (observed:
    `KeyError: '"mcpServers"'`), so the task cannot even start.
    """
    out = template
    for key, val in values.items():
        out = out.replace("{" + key + "}", val)
    return out


def call_agent(
    agent_template: str, system: str, user: str, cwd: str | None = None
) -> str:
    """Call the agent CLI per the command template and return its stdout text.

    system is always written to a temp file; if the template contains
    {prompt_file}, user is written to a file, otherwise it is passed via stdin.
    The command is entirely determined by the template; the script appends no
    parameters.

    Note: the agent CLI should restrict tool permissions, allowing only reads
    and MCP tools, and forbidding file writes.
    """
    # Output debug file directory
    debug_dir = os.path.expanduser("~/.cache/utility/agent_debug")
    os.makedirs(debug_dir, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.write(system)
        sys_prompt_path = f.name

    user_file = None
    try:
        kwargs = {"system_prompt_file": sys_prompt_path}
        if "{prompt_file}" in agent_template:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".txt", delete=False, encoding="utf-8"
            ) as f:
                f.write(user)
                user_file = f.name
            kwargs["prompt_file"] = user_file
            stdin = None
        else:
            stdin = user.encode("utf-8")

        # Build the full command: template
        base_cmd = _render_template(agent_template, kwargs)
        cmd = shlex.split(base_cmd)
        try:
            logger.info("Calling agent CLI: cmd=%s", " ".join(cmd))
            proc = subprocess.run(
                cmd,
                input=stdin,
                capture_output=True,
                cwd=cwd,
                timeout=AGENT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            logger.error(
                "agent CLI timed out (%ds): cmd=%s", AGENT_TIMEOUT, " ".join(cmd)
            )
            raise SystemExit(
                f"[agent error] timed out ({AGENT_TIMEOUT}s), killed cmd={' '.join(cmd)}"
            )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace")
            logger.error(
                "agent CLI failed: exit=%d, stderr=%s", proc.returncode, stderr[:500]
            )
            raise SystemExit(
                f"[agent error] exit={proc.returncode} cmd={' '.join(cmd)}\n" + stderr
            )
        text = proc.stdout.decode("utf-8", errors="replace").strip()
        if not text:
            logger.error("agent CLI output is empty: cmd=%s", " ".join(cmd))
            raise SystemExit("[agent error] output is empty")

        # Save debug file
        timestamp = int(time.time())
        debug_file = os.path.join(debug_dir, f"agent_output_{timestamp}.txt")
        with open(debug_file, "w", encoding="utf-8") as f:
            f.write(
                f"[System Prompt]\n{system}\n\n[User Prompt]\n{user}\n\n[Output]\n{text}"
            )
        logger.info("agent CLI output: %d chars, debug file: %s", len(text), debug_file)

        return text
    finally:
        try:
            os.unlink(sys_prompt_path)
        except OSError:
            pass
        if user_file:
            try:
                os.unlink(user_file)
            except OSError:
                pass
