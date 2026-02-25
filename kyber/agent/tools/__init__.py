"""Agent tools module.

Tools self-register on import via the singleton registry.
Call discover_tools() or registry.discover() to trigger auto-import
of all built-in tool modules.
"""

from kyber.agent.tools.base import Tool
from kyber.agent.tools.registry import ToolRegistry, registry


def discover_tools() -> None:
    """Discover and register all built-in tools.
    
    Convenience wrapper around registry.discover().
    Safe to call multiple times.
    """
    registry.discover()


__all__ = ["Tool", "ToolRegistry", "registry", "discover_tools"]
