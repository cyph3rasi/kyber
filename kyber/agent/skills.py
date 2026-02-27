"""Skills loader for agent capabilities."""

import json
import os
import re
import shutil
from pathlib import Path

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"

# Legacy managed skills directory (deprecated; kept for compatibility only)
MANAGED_SKILLS_DIR = Path.home() / ".kyber" / "skills"

# Metadata namespace keys we understand (ours + OpenClaw-compatible)
_META_NAMESPACES = ("kyber", "openclaw")


class SkillsLoader:
    """
    Loader for agent skills.
    
    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    
    Compatible with the AgentSkills / OpenClaw skill format.
    Active precedence: workspace > builtin.
    """
    
    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        # Single canonical skills location: workspace/skills.
        self.managed_skills = self.workspace_skills
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR

        # Fresh installs may not have created these directories yet. Create them
        # eagerly so "skills" is a visible concept out of the box.
        try:
            self.workspace_skills.mkdir(parents=True, exist_ok=True)
        except Exception:
            # Best-effort: if workspace is read-only, builtin skills still work.
            pass
    
    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        List all available skills.
        
        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.
        
        Returns:
            List of skill info dicts with 'name', 'path', 'source'.
        """
        skills = []
        seen = set()
        
        def _scan(directory: Path, source: str):
            if not directory.exists():
                return
            for skill_dir in directory.iterdir():
                if skill_dir.is_dir() and skill_dir.name not in seen:
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        seen.add(skill_dir.name)
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": source})
        
        # Precedence: workspace > builtin
        _scan(self.workspace_skills, "workspace")
        if self.builtin_skills:
            _scan(self.builtin_skills, "builtin")
        
        # Filter by requirements
        if filter_unavailable:
            return [s for s in skills if self._check_requirements(self._get_skill_meta(s["name"]))]
        return skills
    
    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.
        
        Args:
            name: Skill name (directory name).
        
        Returns:
            Skill content or None if not found.
        """
        # Precedence: workspace > builtin
        for base in (self.workspace_skills, self.builtin_skills):
            if base:
                skill_file = base / name / "SKILL.md"
                if skill_file.exists():
                    return skill_file.read_text(encoding="utf-8")
        
        return None
    
    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.
        
        Args:
            skill_names: List of skill names to load.
        
        Returns:
            Formatted skills content.
        """
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{content}")
        
        return "\n\n---\n\n".join(parts) if parts else ""
    
    def build_skills_summary(self) -> str:
        """
        Build a summary of all skills (name, description, path, availability).
        
        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed.
        
        Returns:
            XML-formatted skills summary.
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""
        
        def escape_xml(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        
        lines = ["<skills>"]
        for s in all_skills:
            name = escape_xml(s["name"])
            path = s["path"]
            desc = escape_xml(self._get_skill_description(s["name"]))
            skill_meta = self._get_skill_meta(s["name"])
            available = self._check_requirements(skill_meta)
            
            lines.append(f"  <skill available=\"{str(available).lower()}\">")
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{desc}</description>")
            lines.append(f"    <location>{path}</location>")
            
            # Show missing requirements for unavailable skills
            if not available:
                missing = self._get_missing_requirements(skill_meta)
                if missing:
                    lines.append(f"    <requires>{escape_xml(missing)}</requires>")
                install_hint = self._get_install_hint(skill_meta)
                if install_hint:
                    lines.append(f"    <install_hint>{escape_xml(install_hint)}</install_hint>")
            
            lines.append(f"  </skill>")
        lines.append("</skills>")
        
        return "\n".join(lines)
    
    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        missing = []
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI: {b}")
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        return ", ".join(missing)

    def _get_install_hint(self, skill_meta: dict) -> str:
        """Build a practical install hint from skill metadata, if available."""
        install = skill_meta.get("install", [])
        if not isinstance(install, list):
            return ""

        hints: list[str] = []
        for item in install:
            if not isinstance(item, dict):
                continue

            kind = str(item.get("kind", "")).strip().lower()
            label = str(item.get("label", "")).strip()
            formula = str(item.get("formula", "")).strip()
            package = str(item.get("package", "")).strip()
            command = str(item.get("command", "")).strip()

            cmd = ""
            if kind == "brew" and formula:
                cmd = f"brew install {formula}"
            elif kind in ("apt", "apt-get") and package:
                cmd = f"sudo apt-get install -y {package}"
            elif kind in ("dnf", "yum") and package:
                cmd = f"sudo {kind} install -y {package}"
            elif kind == "pip" and package:
                cmd = f"python3 -m pip install {package}"
            elif kind == "npm" and package:
                cmd = f"npm install -g {package}"
            elif kind == "cargo" and package:
                cmd = f"cargo install {package}"
            elif kind == "go" and package:
                cmd = f"go install {package}"
            elif command:
                cmd = command

            if label and cmd:
                hints.append(f"{label}: {cmd}")
            elif cmd:
                hints.append(cmd)
            elif label:
                hints.append(label)

        return " | ".join(hints)
    
    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # Fallback to skill name
    
    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content
    
    def _parse_kyber_metadata(self, raw: str) -> dict:
        """Parse skill metadata JSON from frontmatter.
        
        Understands both kyber and openclaw namespace keys so that
        OpenClaw-format skills work out of the box.
        """
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {}
            for ns in _META_NAMESPACES:
                if ns in data:
                    return data[ns]
            return {}
        except (json.JSONDecodeError, TypeError):
            return {}
    
    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        return True
    
    def _get_skill_meta(self, name: str) -> dict:
        """Get kyber metadata for a skill (cached in frontmatter)."""
        meta = self.get_skill_metadata(name) or {}
        return self._parse_kyber_metadata(meta.get("metadata", ""))
    
    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        result = []
        for s in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_kyber_metadata(meta.get("metadata", ""))
            if skill_meta.get("always") or meta.get("always"):
                result.append(s["name"])
        return result
    
    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.
        
        Args:
            name: Skill name.
        
        Returns:
            Metadata dict or None.
        """
        content = self.load_skill(name)
        if not content:
            return None
        
        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                # Simple YAML parsing
                metadata = {}
                for line in match.group(1).split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        metadata[key.strip()] = value.strip().strip('"\'')
                return metadata
        
        return None
