"""Skill install/update/remove for Kyber.

Design goals:
- Compatible with the skills.sh ecosystem (directory containing SKILL.md).
- Install into managed dir: ~/.kyber/skills/<skill-name>/SKILL.md
- Keep a small manifest so updates/removals are possible.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kyber.agent.skills import MANAGED_SKILLS_DIR


def _manifest_path() -> Path:
    return MANAGED_SKILLS_DIR / ".kyber-manifest.json"


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_slug(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "-", s)
    s = s.strip("-._")
    return s


@dataclass
class InstallSpec:
    url: str
    subpath: str | None = None
    ref: str | None = None  # branch/tag/sha


def parse_source(source: str) -> InstallSpec:
    """Parse source into a git URL + optional subpath.

    Supported:
    - owner/repo
    - https://github.com/owner/repo
    - https://github.com/owner/repo/tree/<ref>/<path>
    - https://github.com/owner/repo.git
    """
    s = (source or "").strip()
    if not s:
        raise ValueError("source is required")

    # owner/repo
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", s):
        return InstallSpec(url=f"https://github.com/{s}.git")

    if s.startswith("https://github.com/") or s.startswith("http://github.com/"):
        # Normalize to https
        s = "https://" + s.split("://", 1)[1]

        m = re.match(r"^https://github\\.com/([^/]+)/([^/]+?)(?:\\.git)?(?:/tree/([^/]+)(/.*)?)?$", s)
        if not m:
            raise ValueError("unsupported GitHub URL")
        owner, repo, ref, rest = m.group(1), m.group(2), m.group(3), m.group(4)
        url = f"https://github.com/{owner}/{repo}.git"
        subpath = rest.lstrip("/") if rest else None
        return InstallSpec(url=url, subpath=subpath or None, ref=ref or None)

    # Raw git URL fallback (.git required)
    if s.endswith(".git") and (s.startswith("https://") or s.startswith("http://")):
        return InstallSpec(url=s)

    raise ValueError("unsupported source format; use owner/repo or a GitHub URL")


def _read_head_sha(repo_dir: Path) -> str | None:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_dir))
        return out.decode("utf-8").strip()
    except Exception:
        return None


def _clone_repo(url: str, ref: str | None = None) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="kyber-skillhub-"))
    args = ["git", "clone", "--depth", "1"]
    if ref:
        args += ["--branch", ref]
    args += [url, str(tmp)]
    subprocess.check_call(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return tmp


def _has_skill_md(dir_path: Path) -> bool:
    return (dir_path / "SKILL.md").exists()


def find_skill_dirs(root: Path) -> list[Path]:
    """Find directories containing SKILL.md, using skills.sh CLI search priority."""
    base = root
    priority = [
        base,
        base / "skills",
        base / "skills" / ".curated",
        base / "skills" / ".experimental",
        base / "skills" / ".system",
        base / ".agent" / "skills",
        base / ".agents" / "skills",
        base / ".claude" / "skills",
        base / ".cline" / "skills",
        base / ".codebuddy" / "skills",
        base / ".codex" / "skills",
        base / ".commandcode" / "skills",
        base / ".continue" / "skills",
        base / ".cursor" / "skills",
        base / ".github" / "skills",
        base / ".goose" / "skills",
        base / ".iflow" / "skills",
        base / ".junie" / "skills",
        base / ".kilocode" / "skills",
        base / ".kiro" / "skills",
        base / ".mux" / "skills",
        base / ".neovate" / "skills",
        base / ".opencode" / "skills",
        base / ".openhands" / "skills",
        base / ".pi" / "skills",
        base / ".qoder" / "skills",
        base / ".roo" / "skills",
        base / ".trae" / "skills",
        base / ".windsurf" / "skills",
        base / ".zencoder" / "skills",
    ]

    found: list[Path] = []

    # Direct SKILL.md at a priority path
    for p in priority:
        if _has_skill_md(p):
            found.append(p)

    # Child directories of priority paths
    for p in priority:
        if not p.exists() or not p.is_dir():
            continue
        try:
            for child in p.iterdir():
                if not child.is_dir():
                    continue
                if _has_skill_md(child):
                    found.append(child)
        except Exception:
            continue

    if found:
        # de-dupe stable order
        seen: set[str] = set()
        out: list[Path] = []
        for d in found:
            k = str(d.resolve())
            if k in seen:
                continue
            seen.add(k)
            out.append(d)
        return out

    # Recursive fallback: SKILL.md anywhere under root (cap depth by pruning hidden + node_modules).
    out: list[Path] = []
    for skill_md in root.rglob("SKILL.md"):
        parts = {p.lower() for p in skill_md.parts}
        if "node_modules" in parts:
            continue
        if any(p.startswith(".git") for p in skill_md.parts):
            continue
        out.append(skill_md.parent)
    return out


def _load_manifest() -> dict[str, Any]:
    path = _manifest_path()
    if not path.exists():
        return {"installed": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"installed": {}}


def _save_manifest(obj: dict[str, Any]) -> None:
    MANAGED_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    _manifest_path().write_text(json.dumps(obj, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def reconcile_manifest() -> dict[str, Any]:
    """Prune manifest entries that no longer exist on disk.

    This keeps `update-all` from reinstalling deleted skills.
    """
    manifest = _load_manifest()
    installed = manifest.get("installed", {})
    if not isinstance(installed, dict):
        installed = {}

    existing: set[str] = set()
    if MANAGED_SKILLS_DIR.exists():
        for p in MANAGED_SKILLS_DIR.iterdir():
            if not p.is_dir():
                continue
            if (p / "SKILL.md").exists():
                existing.add(p.name)

    changed = False
    for k, rec in list(installed.items()):
        if not isinstance(rec, dict):
            installed.pop(k, None)
            changed = True
            continue
        skills = rec.get("skills")
        if isinstance(skills, list):
            new_skills = [s for s in skills if s in existing]
            if new_skills != skills:
                rec["skills"] = new_skills
                rec["updated_at"] = _utcnow()
                changed = True
        # Drop empty records
        if not rec.get("skills"):
            installed.pop(k, None)
            changed = True

    manifest["installed"] = installed
    if changed:
        _save_manifest(manifest)
    return manifest


def list_managed_installs() -> dict[str, Any]:
    return reconcile_manifest()


def install_from_source(
    source: str,
    skill: str | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    # Ensure manifest doesn't contain stale entries before we add/update.
    reconcile_manifest()
    spec = parse_source(source)

    repo_dir = _clone_repo(spec.url, ref=spec.ref)
    try:
        base = repo_dir / (spec.subpath or "")
        if not base.exists():
            raise ValueError(f"subpath not found in repo: {spec.subpath}")

        skill_dirs = find_skill_dirs(base)
        if not skill_dirs:
            raise ValueError("no SKILL.md found in source")

        if skill:
            wanted = _safe_slug(skill)
            skill_dirs = [d for d in skill_dirs if _safe_slug(d.name) == wanted]
            if not skill_dirs:
                raise ValueError(f"skill '{skill}' not found in source")

        installed: list[str] = []
        for d in skill_dirs:
            name = d.name
            if not name or name in {".", ".."}:
                continue
            if name.startswith("."):
                # Don't install hidden skills by default.
                continue

            dest = MANAGED_SKILLS_DIR / name
            if dest.exists():
                if not replace:
                    continue
                shutil.rmtree(dest)
            shutil.copytree(d, dest)
            installed.append(name)

        sha = _read_head_sha(repo_dir)
        manifest = _load_manifest()
        inst = manifest.setdefault("installed", {})
        inst_key = _safe_slug(source) or _safe_slug(spec.url)
        inst[inst_key] = {
            "source": source,
            "url": spec.url,
            "ref": spec.ref,
            "subpath": spec.subpath,
            "skills": sorted(set((inst.get(inst_key, {}).get("skills") or []) + installed)),
            "updated_at": _utcnow(),
            "revision": sha,
        }
        _save_manifest(manifest)
        return {"ok": True, "installed": installed, "revision": sha}
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def remove_skill(name: str) -> dict[str, Any]:
    skill_name = (name or "").strip()
    if not skill_name:
        raise ValueError("name is required")
    dest = MANAGED_SKILLS_DIR / skill_name
    if dest.exists():
        shutil.rmtree(dest)

    manifest = _load_manifest()
    installed = manifest.get("installed", {})
    if isinstance(installed, dict):
        # Remove from any package records
        for k, rec in list(installed.items()):
            skills = rec.get("skills") if isinstance(rec, dict) else None
            if isinstance(skills, list) and skill_name in skills:
                rec["skills"] = [s for s in skills if s != skill_name]
                rec["updated_at"] = _utcnow()
            if isinstance(rec, dict) and rec.get("skills") == []:
                installed.pop(k, None)
        manifest["installed"] = installed
        _save_manifest(manifest)

    # Also prune any stale entries (in case the skill was already missing).
    reconcile_manifest()
    return {"ok": True}


def update_all(replace: bool = True) -> dict[str, Any]:
    manifest = reconcile_manifest()
    installed = manifest.get("installed", {})
    if not isinstance(installed, dict) or not installed:
        return {"ok": True, "updated": []}

    updated: list[dict[str, Any]] = []
    for _, rec in installed.items():
        if not isinstance(rec, dict):
            continue
        src = rec.get("source") or rec.get("url")
        if not src:
            continue
        try:
            res = install_from_source(str(src), replace=replace)
            updated.append({"source": src, "installed": res.get("installed", []), "revision": res.get("revision")})
        except Exception as e:
            updated.append({"source": src, "error": str(e)})

    return {"ok": True, "updated": updated}


def _parse_frontmatter(path: Path) -> dict[str, str]:
    """Parse a small YAML-ish frontmatter block for common fields."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    m = re.match(r"^---\\n(.*?)\\n---", text, re.DOTALL)
    if not m:
        return {}
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip().strip("\"'")
    return meta


def preview_source(source: str, max_skills: int = 25) -> dict[str, Any]:
    """Shallow-preview a source without installing it."""
    spec = parse_source(source)
    repo_dir = _clone_repo(spec.url, ref=spec.ref)
    try:
        base = repo_dir / (spec.subpath or "")
        if not base.exists():
            raise ValueError(f"subpath not found in repo: {spec.subpath}")

        # README preview (best-effort)
        readme_preview = ""
        for name in ("README.md", "readme.md", "README.MD"):
            p = base / name
            if p.exists():
                try:
                    readme_preview = p.read_text(encoding="utf-8")[:3000].strip()
                except Exception:
                    readme_preview = ""
                break

        skill_dirs = find_skill_dirs(base)
        skills: list[dict[str, Any]] = []
        for d in skill_dirs[: max(1, int(max_skills))]:
            skill_md = d / "SKILL.md"
            meta = _parse_frontmatter(skill_md)
            desc = meta.get("description", "") or meta.get("summary", "")
            skills.append(
                {
                    "name": d.name,
                    "description": desc,
                    "path": str(d.relative_to(repo_dir)),
                }
            )

        return {
            "ok": True,
            "source": source,
            "url": spec.url,
            "ref": spec.ref,
            "subpath": spec.subpath,
            "revision": _read_head_sha(repo_dir),
            "readme_preview": readme_preview,
            "skills": skills,
        }
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def fetch_skill_md(source: str, skill: str) -> dict[str, Any]:
    """Fetch the full SKILL.md for a specific skill in a source repo.

    This is used for the dashboard "Details" panel for skills.sh search results.
    """
    spec = parse_source(source)
    wanted = _safe_slug(skill)
    if not wanted:
        raise ValueError("skill is required")

    repo_dir = _clone_repo(spec.url, ref=spec.ref)
    try:
        base = repo_dir / (spec.subpath or "")
        if not base.exists():
            raise ValueError(f"subpath not found in repo: {spec.subpath}")

        skill_dirs = find_skill_dirs(base)
        if not skill_dirs:
            raise ValueError("no SKILL.md found in source")

        # Prefer exact dir-name match (skills.sh uses directory names as IDs).
        match = None
        for d in skill_dirs:
            if _safe_slug(d.name) == wanted:
                match = d
                break
        if match is None:
            raise ValueError(f"skill '{skill}' not found in source")

        skill_md = match / "SKILL.md"
        try:
            content = skill_md.read_text(encoding="utf-8")
        except Exception as e:
            raise ValueError(f"failed to read SKILL.md: {e}")

        # Cap to avoid huge payloads.
        if len(content) > 120_000:
            content = content[:120_000] + "\n\n...(truncated)\n"

        return {
            "ok": True,
            "source": source,
            "url": spec.url,
            "ref": spec.ref,
            "subpath": spec.subpath,
            "revision": _read_head_sha(repo_dir),
            "skill": {
                "name": match.name,
                "path": str(match.relative_to(repo_dir)),
                "content": content,
            },
        }
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)
