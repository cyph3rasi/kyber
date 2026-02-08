"""skills.sh search integration.

skills.sh exposes a simple search endpoint used by the official `skills` CLI:
  GET https://skills.sh/api/search?q=<query>&limit=<n>
"""

from __future__ import annotations

from typing import Any

import httpx


async def search_skills_sh(query: str, limit: int = 10) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if len(q) < 2:
        return []
    lim = max(1, min(int(limit), 25))

    url = "https://skills.sh/api/search"
    params = {"q": q, "limit": str(lim)}

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    skills = data.get("skills", []) if isinstance(data, dict) else []
    out: list[dict[str, Any]] = []
    for s in skills:
        if not isinstance(s, dict):
            continue
        out.append(
            {
                "id": s.get("id", ""),
                "skill_id": s.get("skillId", "") or s.get("skill_id", "") or "",
                "name": s.get("name", ""),
                "source": s.get("source", "") or "",
                "installs": int(s.get("installs", 0) or 0),
            }
        )
    return out
