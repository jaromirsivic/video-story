"""The plugin manifests and the skill folders agree with each other and with the formats
the hosts document.

- ``.claude-plugin/plugin.json`` and ``marketplace.json``: Claude Code (Codex also reads
  the marketplace file as its "legacy-compatible" location).
- ``plugin.json`` at the root: the Agent Plugins 1.0.0 manifest, read by Codex and Cursor;
  validated against a copy of https://agent-plugins.org/schemas/1.0.0/plugin.schema.json.
- ``skills/<name>/SKILL.md``: the Agent Skills rules (agentskills.io/specification); Cursor
  requires the ``name`` to match the folder.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILLS = ("video-story", "video-story-edit")

# Verbatim copy of https://agent-plugins.org/schemas/1.0.0/plugin.schema.json (fetched
# 13 Nov 2026). Clients must not fetch it at load time either (spec section 5.2).
AGENT_PLUGIN_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
AGENT_PLUGIN_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": AGENT_PLUGIN_SCHEMA_ID,
    "title": "Agent Plugins Manifest",
    "type": "object",
    "properties": {
        "$schema": {"const": AGENT_PLUGIN_SCHEMA_ID},
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "pattern": r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$",
        },
        "version": {"type": "string"},
        "description": {"type": "string"},
        "author": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "email": {"type": "string"},
                "url": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "homepage": {"type": "string"},
        "repository": {"type": "string"},
        "license": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "extensions": {"type": "object", "additionalProperties": {"type": "object"}},
    },
    "required": ["$schema", "name"],
    "additionalProperties": False,
}


def _json(relative: str) -> dict:
    return json.loads((REPO / relative).read_text(encoding="utf-8"))


def _pyproject() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _frontmatter(skill: str) -> dict[str, str]:
    """The single-line ``key: value`` pairs between the two ``---`` lines of SKILL.md."""
    lines = (REPO / "skills" / skill / "SKILL.md").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---", f"{skill}/SKILL.md must start with YAML frontmatter"
    end = lines.index("---", 1)
    fields = {}
    for line in lines[1:end]:
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


@pytest.mark.parametrize(
    "relative", [".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", "plugin.json"]
)
def test_manifests_are_json_objects(relative: str):
    assert isinstance(_json(relative), dict)


def test_names_versions_and_descriptions_agree():
    project = _pyproject()
    claude = _json(".claude-plugin/plugin.json")
    portable = _json("plugin.json")
    (entry,) = _json(".claude-plugin/marketplace.json")["plugins"]
    for manifest in (claude, portable, entry):
        assert manifest["name"] == project["name"] == "video-story"
        assert manifest["version"] == project["version"]
    assert portable["description"] == claude["description"]
    assert portable["author"] == claude["author"]
    assert portable["license"] == claude["license"]


def test_portable_manifest_follows_the_agent_plugins_schema():
    jsonschema = pytest.importorskip("jsonschema")
    manifest = _json("plugin.json")
    jsonschema.Draft202012Validator(AGENT_PLUGIN_SCHEMA).validate(manifest)
    # The schema is closed: no component paths in the manifest (fixed locations instead).
    assert "skills" not in manifest and "mcpServers" not in manifest


def test_paths_the_manifests_name_exist():
    claude = _json(".claude-plugin/plugin.json")
    assert (REPO / claude["skills"]).resolve() == REPO / "skills"  # relative to the root
    (entry,) = _json(".claude-plugin/marketplace.json")["plugins"]
    # Claude Code and Codex resolve a local source against the marketplace root (the
    # repository); "./" is the plugin root itself.
    assert entry["source"] == "./"
    plugin_root = (REPO / entry["source"]).resolve()
    assert (plugin_root / ".claude-plugin" / "plugin.json").is_file()
    assert (plugin_root / "plugin.json").is_file()
    # The Agent Plugins fixed location: every immediate child of skills/ with a SKILL.md.
    found = sorted(p.parent.name for p in (plugin_root / "skills").glob("*/SKILL.md"))
    assert found == sorted(SKILLS)


def test_no_mcp_server_is_bundled():
    """The skills drive the CLI; a bundled server would start with every host session and
    offer a second set of tools (docs/installation.md, "Plugin")."""
    for name in ("mcp.json", ".mcp.json"):
        assert not (REPO / name).exists(), name


@pytest.mark.parametrize("skill", SKILLS)
def test_skill_frontmatter_follows_the_agent_skills_rules(skill: str):
    fields = _frontmatter(skill)
    name, description = fields.get("name", ""), fields.get("description", "")
    assert name == skill  # Cursor: "Must match the parent folder name."
    assert len(name) <= 64 and "--" not in name
    assert re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", name)
    assert 1 <= len(description) <= 1024


@pytest.mark.parametrize("skill", SKILLS)
def test_codex_metadata_uses_only_documented_sections(skill: str):
    """agents/openai.yaml (learn.chatgpt.com/docs/build-skills, "Optional metadata")."""
    text = (REPO / "skills" / skill / "agents" / "openai.yaml").read_text(encoding="utf-8")
    top = {line.split(":", 1)[0] for line in text.splitlines() if re.match(r"[a-z_]+:", line)}
    assert top <= {"interface", "policy", "dependencies"}
    for key in ("display_name", "short_description", "default_prompt"):
        assert re.search(rf"^  {key}: \".+\"$", text, re.MULTILINE), key
    assert f"${skill} " in text  # the default prompt names the skill as Codex mentions it
