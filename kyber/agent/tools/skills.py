"""Skill management tools."""

import json
import re
import shutil
from pathlib import Path
from typing import Any

from kyber.agent.skills import SkillsLoader
from kyber.agent.tools.base import Tool
from kyber.agent.tools.registry import registry


DEFAULT_WORKSPACE = Path.home() / ".kyber" / "workspace"


def _get_loader(kwargs: dict[str, Any]) -> SkillsLoader:
    """Build a SkillsLoader from runtime context."""
    agent_core = kwargs.get("agent_core")
    workspace = getattr(agent_core, "workspace", None) if agent_core else None
    if not isinstance(workspace, Path):
        workspace = DEFAULT_WORKSPACE
    return SkillsLoader(workspace)


def _resolve_category(skill_file: Path, loader: SkillsLoader, source: str) -> str:
    """Infer category label for display."""
    skill_dir = skill_file.parent

    if source == "workspace":
        root = loader.workspace_skills
    else:
        root = loader.builtin_skills

    if root and skill_dir.parent == root:
        return source
    return skill_dir.parent.name


def _list_available_skills(loader: SkillsLoader) -> list[dict[str, str]]:
    """List all available skills across workspace/builtin sources."""
    skills = []
    entries = loader.list_skills(filter_unavailable=False)
    for entry in entries:
        path = Path(entry["path"])
        content = path.read_text(encoding="utf-8", errors="ignore")
        description = _extract_description(content)
        skills.append({
            "name": entry["name"],
            "category": _resolve_category(path, loader, entry["source"]),
            "description": description,
            "source": entry["source"],
            "path": str(path),
        })
    return skills


def _extract_description(content: str) -> str:
    """Extract description from SKILL.md frontmatter."""
    match = re.search(r"^description:\s*(.+)$", content, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return ""


def _iter_skill_dirs(root: Path) -> list[Path]:
    """Return candidate skill dirs under a root (direct + one nested level)."""
    if not root.exists():
        return []
    candidates: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        # Direct skill layout: <root>/<name>/SKILL.md
        if (child / "SKILL.md").exists():
            candidates.append(child)
            continue
        # Legacy nested layout: <root>/<category>/<name>/SKILL.md
        for nested in child.iterdir():
            if nested.is_dir() and (nested / "SKILL.md").exists():
                candidates.append(nested)
    return candidates


def _get_skill_path(name: str, loader: SkillsLoader, category: str | None = None) -> Path | None:
    """Find a mutable workspace skill dir by name."""
    normalized_name = (name or "").strip()
    if not normalized_name:
        return None

    direct = loader.workspace_skills / normalized_name
    if (direct / "SKILL.md").exists():
        return direct

    # Optional category hint for legacy nested layouts.
    if category:
        nested = loader.workspace_skills / category / normalized_name
        if (nested / "SKILL.md").exists():
            return nested

    # Fallback: scan known layouts.
    for skill_dir in _iter_skill_dirs(loader.workspace_skills):
        if skill_dir.name == normalized_name:
            return skill_dir

    return None


class SkillsListTool(Tool):
    """List available skills."""

    toolset = "skills"

    @property
    def name(self) -> str:
        return "skills_list"

    @property
    def description(self) -> str:
        return "List available skills (name + description). Use skill_view(name) to load full content."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Optional category filter to narrow results",
                },
            },
            "required": [],
        }

    async def execute(self, category: str | None = None, **kwargs) -> str:
        loader = _get_loader(kwargs)
        skills = _list_available_skills(loader)
        
        if category:
            skills = [s for s in skills if s["category"] == category]
        
        return json.dumps({
            "skills": skills,
            "count": len(skills),
            "skills_dir": str(loader.workspace_skills),
            "skills_dirs": [
                str(loader.workspace_skills),
                str(loader.builtin_skills),
            ],
        }, ensure_ascii=False)


class SkillViewTool(Tool):
    """View a skill's full content."""

    toolset = "skills"

    @property
    def name(self) -> str:
        return "skill_view"

    @property
    def description(self) -> str:
        return (
            "Skills allow for loading information about specific tasks and workflows. "
            "Load a skill's full content or access its linked files (references, templates, scripts). "
            "First call returns SKILL.md content plus a 'linked_files' dict showing available references/templates/scripts."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill name (use skills_list to see available skills)",
                },
                "file_path": {
                    "type": "string",
                    "description": "OPTIONAL: Path to a linked file within the skill (e.g. 'references/api.md'). Omit to get the main SKILL.md content.",
                },
            },
            "required": ["name"],
        }

    async def execute(self, name: str, file_path: str | None = None, **kwargs) -> str:
        loader = _get_loader(kwargs)
        entry = next(
            (s for s in loader.list_skills(filter_unavailable=False) if s["name"] == name),
            None,
        )
        if not entry:
            return json.dumps({"error": f"Skill '{name}' not found"}, ensure_ascii=False)
        skill_file = Path(entry["path"])
        skill_path = skill_file.parent
        
        if file_path:
            # Access a linked file
            file_full_path = skill_path / file_path
            if not file_full_path.exists():
                return json.dumps({"error": f"File '{file_path}' not found in skill '{name}'"}, ensure_ascii=False)
            content = file_full_path.read_text()
            return json.dumps({
                "name": name,
                "file_path": file_path,
                "content": content,
            }, ensure_ascii=False)
        
        # Load main SKILL.md
        if not skill_file.exists():
            return json.dumps({"error": f"SKILL.md not found for skill '{name}'"}, ensure_ascii=False)
        content = skill_file.read_text(encoding="utf-8", errors="ignore")
        
        # Find linked files
        linked_files = {}
        for subdir in ["references", "templates", "scripts", "assets"]:
            subdir_path = skill_path / subdir
            if subdir_path.exists() and subdir_path.is_dir():
                files = list(subdir_path.glob("*"))
                if files:
                    linked_files[subdir] = [f.name for f in files if f.is_file()]
        
        return json.dumps({
            "name": name,
            "category": skill_path.parent.name,
            "source": entry.get("source", "unknown"),
            "content": content,
            "linked_files": linked_files,
        }, ensure_ascii=False)


class SkillManageTool(Tool):
    """Manage skills (create, update, delete)."""

    toolset = "skills"

    @property
    def name(self) -> str:
        return "skill_manage"

    @property
    def description(self) -> str:
        return (
            "Manage skills (create, update, delete). Skills are procedural memory — reusable approaches for recurring task types. "
            "Newly created skills are workspace-local by default (workspace/skills/<name>/SKILL.md). "
            "Create when: complex task succeeded (5+ calls), errors overcome, user-corrected approach worked, non-trivial workflow discovered. "
            "Update when: instructions stale/wrong, OS-specific failures, missing steps or pitfalls found during use."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "patch", "edit", "delete", "write_file", "remove_file"],
                    "description": "The action to perform.",
                },
                "name": {
                    "type": "string",
                    "description": "Skill name (lowercase, hyphens/underscores, max 64 chars). Must match an existing skill for patch/edit/delete.",
                },
                "content": {
                    "type": "string",
                    "description": "Full SKILL.md content (YAML frontmatter + markdown body). Required for 'create' and 'edit'.",
                },
                "category": {
                    "type": "string",
                    "description": "Optional lookup hint for legacy nested skill layouts when patching/editing/deleting.",
                },
                "old_string": {
                    "type": "string",
                    "description": "Text to find in the file (required for 'patch'). Must be unique unless replace_all=true.",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text (required for 'patch'). Can be empty string to delete the matched text.",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "For 'patch': replace all occurrences instead of requiring a unique match.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Path to a supporting file within the skill directory. For 'write_file'/'remove_file': required, must be under references/, templates/, scripts/, or assets/.",
                },
                "file_content": {
                    "type": "string",
                    "description": "Content for the file. Required for 'write_file'.",
                },
            },
            "required": ["action", "name"],
        }

    async def execute(
        self,
        action: str,
        name: str,
        content: str | None = None,
        category: str | None = None,
        old_string: str | None = None,
        new_string: str | None = None,
        replace_all: bool = False,
        file_path: str | None = None,
        file_content: str | None = None,
        **kwargs
    ) -> str:
        loader = _get_loader(kwargs)
        
        if action == "create":
            if not content:
                return json.dumps({"error": "content required for create action"}, ensure_ascii=False)

            # Bot-created skills are workspace-local by default.
            skill_dir = loader.workspace_skills / name
            skill_dir.mkdir(parents=True, exist_ok=True)
            
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(content)
            
            return json.dumps({
                "status": "created",
                "name": name,
                "source": "workspace",
                "path": str(skill_dir),
            }, ensure_ascii=False)
        
        elif action == "patch":
            skill_path = _get_skill_path(name, loader, category)
            if not skill_path:
                return json.dumps({"error": f"Skill '{name}' not found"}, ensure_ascii=False)
            
            target_file = skill_path / (file_path or "SKILL.md")
            if not target_file.exists():
                return json.dumps({"error": f"File not found: {target_file}"}, ensure_ascii=False)
            
            if not old_string:
                return json.dumps({"error": "old_string required for patch action"}, ensure_ascii=False)
            
            file_content_text = target_file.read_text()
            
            if replace_all:
                new_text = file_content_text.replace(old_string, new_string or "")
            else:
                if old_string not in file_content_text:
                    return json.dumps({"error": "old_string not found in file"}, ensure_ascii=False)
                new_text = file_content_text.replace(old_string, new_string or "", 1)
            
            target_file.write_text(new_text)
            
            return json.dumps({
                "status": "patched",
                "name": name,
                "source": "workspace",
                "file": str(target_file),
            }, ensure_ascii=False)
        
        elif action == "edit":
            if not content:
                return json.dumps({"error": "content required for edit action"}, ensure_ascii=False)
            
            skill_path = _get_skill_path(name, loader, category)
            if not skill_path:
                return json.dumps({"error": f"Skill '{name}' not found"}, ensure_ascii=False)
            
            skill_file = skill_path / "SKILL.md"
            skill_file.write_text(content)
            
            return json.dumps({
                "status": "edited",
                "name": name,
                "source": "workspace",
            }, ensure_ascii=False)
        
        elif action == "delete":
            skill_path = _get_skill_path(name, loader, category)
            if not skill_path:
                return json.dumps({"error": f"Skill '{name}' not found"}, ensure_ascii=False)
            
            shutil.rmtree(skill_path)
            
            return json.dumps({
                "status": "deleted",
                "name": name,
                "source": "workspace",
            }, ensure_ascii=False)
        
        elif action == "write_file":
            if not file_path or not file_content:
                return json.dumps({"error": "file_path and file_content required for write_file action"}, ensure_ascii=False)
            
            skill_path = _get_skill_path(name, loader, category)
            if not skill_path:
                return json.dumps({"error": f"Skill '{name}' not found"}, ensure_ascii=False)
            
            # Validate file_path is under allowed directories
            allowed = ["references", "templates", "scripts", "assets"]
            if not any(file_path.startswith(d + "/") for d in allowed):
                return json.dumps({"error": f"file_path must be under one of: {allowed}"}, ensure_ascii=False)
            
            target_file = skill_path / file_path
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(file_content)
            
            return json.dumps({
                "status": "wrote_file",
                "name": name,
                "source": "workspace",
                "file": file_path,
            }, ensure_ascii=False)
        
        elif action == "remove_file":
            if not file_path:
                return json.dumps({"error": "file_path required for remove_file action"}, ensure_ascii=False)
            
            skill_path = _get_skill_path(name, loader, category)
            if not skill_path:
                return json.dumps({"error": f"Skill '{name}' not found"}, ensure_ascii=False)
            
            target_file = skill_path / file_path
            if not target_file.exists():
                return json.dumps({"error": f"File not found: {file_path}"}, ensure_ascii=False)
            
            target_file.unlink()
            
            return json.dumps({
                "status": "removed_file",
                "name": name,
                "source": "workspace",
                "file": file_path,
            }, ensure_ascii=False)
        
        else:
            return json.dumps({"error": f"Unknown action: {action}"}, ensure_ascii=False)


# Self-register
registry.register(SkillsListTool())
registry.register(SkillViewTool())
registry.register(SkillManageTool())
