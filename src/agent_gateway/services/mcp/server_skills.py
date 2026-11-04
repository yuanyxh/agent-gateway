"""Skill discovery for the MCP server.

Skills live under `<notes>/MCP/Skills`. A directory containing `SKILL.md` is a
directory-skill; its `references/`, `agents/` and `scripts/` companions are
skipped so only the skill itself is registered. Shared by the tool schema and
`tool_resource`.
"""

from __future__ import annotations

import logging
import os
import time

from agent_gateway.core.config import notes_dir

logger = logging.getLogger(__name__)

SKILLS_DIR = os.path.join(notes_dir(), "MCP", "Skills")


def parse_skill_frontmatter(raw: str, filename: str) -> tuple[str, str, str, bool]:
    name, description, content, enabled = None, "", raw, True
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1].strip()
            content = parts[2].strip()
            for line in frontmatter.split("\n"):
                line = line.strip()
                if ":" in line:
                    key, _, value = line.partition(":")
                    key = key.strip().lower()
                    value = value.strip().strip('"').strip("'")
                    if key == "name":
                        name = value
                    elif key == "description":
                        description = value
                    elif key == "enable":
                        # 缺省或任何非 false 值都注册；只有显式 false 才跳过
                        enabled = value.lower() != "false"
    if not name:
        name = filename[:-3]
    return name, description, content, enabled


def normalize_name(name: str) -> str:
    return name.replace("-", "_")


SKILL_SKIP_DIRS = {"references", "agents", "scripts"}


def scan_skills() -> dict[str, dict]:
    skills: dict[str, dict] = {}
    if not os.path.isdir(SKILLS_DIR):
        return skills
    try:
        # Recursively walk all subdirectories under SKILLS_DIR, scanning .md files deeply.
        # Only when the current directory is a Waza-style directory skill (containing
        # SKILL.md) do we skip its references/agents/scripts companion subdirectories and
        # register only SKILL.md itself; a plain directory without SKILL.md is not
        # skipped, and all .md files inside it (including those in references etc.
        # subdirectories) are registered as usual.
        for root, dirs, files in os.walk(SKILLS_DIR):
            if "SKILL.md" in files:
                dirs[:] = [d for d in dirs if d not in SKILL_SKIP_DIRS]
                files = [f for f in files if f == "SKILL.md"]
            dirs.sort()
            for f in sorted(files):
                if not f.endswith(".md"):
                    continue
                path = os.path.join(root, f)
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        raw = fh.read()
                    name, description, content, enabled = parse_skill_frontmatter(
                        raw, f
                    )
                    if name and enabled:
                        skills[normalize_name(name)] = {
                            "name": name,
                            "description": description,
                            "content": content,
                        }
                except Exception:
                    pass
    except Exception:
        pass
    return skills


def _skill_reader(name: str):
    """Read the skill body on every call so edits to SKILL.md take effect without a restart."""

    async def _read_skill() -> str:
        started = time.perf_counter()
        ok = True
        try:
            skill = scan_skills().get(name)
            if skill is None:
                ok = False
                raise ValueError(f"Unknown skill: {name}")
            return skill["content"]
        except Exception as e:
            ok = False
            logger.error(f"Error in {name}: {e}", exc_info=True)
            raise
        finally:
            logger.info(
                f"tool_call name={name} ok={ok} "
                f"elapsed_ms={int((time.perf_counter() - started) * 1000)}"
            )

    return _read_skill


def register_skill_tools(mcp) -> None:
    """Register each enabled skill as a no-argument tool returning its Markdown."""
    for name, skill in scan_skills().items():
        mcp.add_tool(
            _skill_reader(name),
            name=name,
            description=skill["description"],
            structured_output=False,
        )


def find_skill_dir(name: str) -> str | None:
    """Locate a skill directory by its registered name (post-normalize); returns the absolute path, or None if not found."""
    direct = os.path.join(SKILLS_DIR, name)
    if os.path.isfile(os.path.join(direct, "SKILL.md")):
        return direct
    # The registered name comes from SKILL.md frontmatter's name (post-normalize); the directory name may keep the original name/hyphens
    try:
        entries = os.listdir(SKILLS_DIR)
    except OSError:
        return None
    for entry in sorted(entries):
        skill_dir = os.path.join(SKILLS_DIR, entry)
        skill_md = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        try:
            with open(skill_md, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except Exception:
            continue
        fname, _, _, _ = parse_skill_frontmatter(raw, skill_md)
        if fname and normalize_name(fname) == name:
            return skill_dir
    return None
