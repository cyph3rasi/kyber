"""Skill management tools."""

import json
import re
import shutil
from pathlib import Path
from typing import Any

from kyber.agent.skills import SkillsLoader
from kyber.agent.tools.base import Tool
from kyber.agent.tools.registry import registry


SKILLS_DIR = Path.home() / ".kyber" / "skills"
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
    elif source == "managed":
        root = loader.managed_skills
    else:
        root = loader.builtin_skills

    if root and skill_dir.parent == root:
        return source
    return skill_dir.parent.name


def _list_available_skills(loader: SkillsLoader) -> list[dict[str, str]]:
    """List all available skills across workspace/managed/builtin sources."""
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


def _get_skill_path(name: str, category: str | None = None) -> Path | None:
    """Find a managed skill path by name (for skill_manage actions)."""
    if not SKILLS_DIR.exists():
        return None
    
    if category:
        path = SKILLS_DIR / category / name
        if path.exists():
            return path
    
    # Search all categories
    for category_dir in SKILLS_DIR.iterdir():
        if not category_dir.is_dir():
            continue
        skill_dir = category_dir / name
        if skill_dir.exists():
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
            "skills_dir": str(SKILLS_DIR),
            "skills_dirs": [
                str(loader.workspace_skills),
                str(loader.managed_skills),
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
                    "description": "Optional category/domain for organizing the skill (e.g. 'devops', 'data-science'). Creates a subdirectory grouping.",
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
        
        if action == "create":
            if not content:
                return json.dumps({"error": "content required for create action"}, ensure_ascii=False)
            
            category = category or "general"
            skill_dir = SKILLS_DIR / category / name
            skill_dir.mkdir(parents=True, exist_ok=True)
            
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(content)
            
            return json.dumps({
                "status": "created",
                "name": name,
                "category": category,
                "path": str(skill_dir),
            }, ensure_ascii=False)
        
        elif action == "patch":
            skill_path = _get_skill_path(name, category)
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
                "file": str(target_file),
            }, ensure_ascii=False)
        
        elif action == "edit":
            if not content:
                return json.dumps({"error": "content required for edit action"}, ensure_ascii=False)
            
            skill_path = _get_skill_path(name, category)
            if not skill_path:
                return json.dumps({"error": f"Skill '{name}' not found"}, ensure_ascii=False)
            
            skill_file = skill_path / "SKILL.md"
            skill_file.write_text(content)
            
            return json.dumps({
                "status": "edited",
                "name": name,
            }, ensure_ascii=False)
        
        elif action == "delete":
            skill_path = _get_skill_path(name, category)
            if not skill_path:
                return json.dumps({"error": f"Skill '{name}' not found"}, ensure_ascii=False)
            
            shutil.rmtree(skill_path)
            
            return json.dumps({
                "status": "deleted",
                "name": name,
            }, ensure_ascii=False)
        
        elif action == "write_file":
            if not file_path or not file_content:
                return json.dumps({"error": "file_path and file_content required for write_file action"}, ensure_ascii=False)
            
            skill_path = _get_skill_path(name, category)
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
                "file": file_path,
            }, ensure_ascii=False)
        
        elif action == "remove_file":
            if not file_path:
                return json.dumps({"error": "file_path required for remove_file action"}, ensure_ascii=False)
            
            skill_path = _get_skill_path(name, category)
            if not skill_path:
                return json.dumps({"error": f"Skill '{name}' not found"}, ensure_ascii=False)
            
            target_file = skill_path / file_path
            if not target_file.exists():
                return json.dumps({"error": f"File not found: {file_path}"}, ensure_ascii=False)
            
            target_file.unlink()
            
            return json.dumps({
                "status": "removed_file",
                "name": name,
                "file": file_path,
            }, ensure_ascii=False)
        
        else:
            return json.dumps({"error": f"Unknown action: {action}"}, ensure_ascii=False)


# Self-register
registry.register(SkillsListTool())
registry.register(SkillViewTool())
registry.register(SkillManageTool())
