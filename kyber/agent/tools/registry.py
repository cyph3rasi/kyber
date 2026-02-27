"""Tool registry for dynamic tool management.

Enhanced with hermes-agent patterns:
- Toolset grouping and filtering
- Availability gating (tools can hide when deps are missing)
- Singleton instance for module-level self-registration
- Auto-discovery of tool modules
"""

import importlib
import logging
from typing import Any

from kyber.agent.tools.base import Tool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """
    Registry for agent tools.
    
    Allows dynamic registration and execution of tools.
    Supports toolset grouping, availability checks, and auto-discovery.
    
    Usage:
        # Tools self-register at import time:
        from kyber.agent.tools.registry import registry
        
        class MyTool(Tool):
            ...
        
        registry.register(MyTool())
        
        # Or register with toolset override:
        registry.register(MyTool(), toolset="custom")
    """
    
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._toolset_overrides: dict[str, str] = {}  # name -> toolset override
        self._discovered = False
    
    def register(self, tool: Tool, toolset: str | None = None) -> None:
        """Register a tool instance.
        
        Args:
            tool: Tool instance to register.
            toolset: Optional toolset override (otherwise uses tool.toolset).
        """
        self._tools[tool.name] = tool
        if toolset:
            self._toolset_overrides[tool.name] = toolset
    
    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)
        self._toolset_overrides.pop(name, None)
    
    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)
    
    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools
    
    def get_toolset(self, name: str) -> str:
        """Get the effective toolset for a tool."""
        if name in self._toolset_overrides:
            return self._toolset_overrides[name]
        tool = self._tools.get(name)
        return tool.toolset if tool else "unknown"
    
    def get_definitions(
        self,
        toolsets: list[str] | None = None,
        tool_names: list[str] | None = None,
        include_unavailable: bool = False,
    ) -> list[dict[str, Any]]:
        """Get tool definitions in OpenAI function-calling format.
        
        Args:
            toolsets: If provided, only include tools from these toolsets.
            tool_names: If provided, only include these specific tools.
            include_unavailable: If True, include tools where is_available() is False.
        
        Returns:
            List of OpenAI tool schema dicts.
        """
        definitions = []
        for name, tool in self._tools.items():
            # Filter by availability
            if not include_unavailable and not tool.is_available():
                continue
            
            # Filter by toolset
            if toolsets is not None:
                effective_toolset = self.get_toolset(name)
                if effective_toolset not in toolsets:
                    continue
            
            # Filter by specific tool names
            if tool_names is not None and name not in tool_names:
                continue
            
            definitions.append(tool.to_schema())
        
        return definitions
    
    def get_available_tools(self) -> dict[str, Tool]:
        """Get all available tools (passing is_available check)."""
        return {
            name: tool for name, tool in self._tools.items()
            if tool.is_available()
        }
    
    def get_tools_by_toolset(self, toolset: str) -> dict[str, Tool]:
        """Get all tools in a specific toolset."""
        return {
            name: tool for name, tool in self._tools.items()
            if self.get_toolset(name) == toolset
        }
    
    def get_all_toolsets(self) -> dict[str, list[str]]:
        """Get a mapping of toolset -> tool names."""
        toolsets: dict[str, list[str]] = {}
        for name in self._tools:
            ts = self.get_toolset(name)
            toolsets.setdefault(ts, []).append(name)
        return toolsets
    
    async def execute(self, name: str, params: dict[str, Any], **kwargs: Any) -> str:
        """
        Execute a tool by name with given parameters.
        
        Args:
            name: Tool name.
            params: Tool parameters.
            **kwargs: Additional context passed to execute (task_id, session info, etc.)
        
        Returns:
            Tool execution result as string.
        """
        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found"

        try:
            errors = tool.validate_params(params)
            if errors:
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors)
            return await tool.execute(**params, **kwargs)
        except Exception as e:
            logger.exception(f"Error executing tool '{name}'")
            return f"Error executing {name}: {str(e)}"
    
    def discover(self) -> None:
        """Auto-discover and import all tool modules.
        
        This triggers module-level registration in each tool file.
        Safe to call multiple times (idempotent).
        """
        if self._discovered:
            return
        
        # Core tool modules that self-register on import
        _TOOL_MODULES = [
            "kyber.agent.tools.shell",
            "kyber.agent.tools.filesystem",
            "kyber.agent.tools.web",
            "kyber.agent.tools.message",
            "kyber.agent.tools.memory",
            "kyber.agent.tools.todo",
            "kyber.agent.tools.clarify",
            "kyber.agent.tools.cron",
            "kyber.agent.tools.skills",
            "kyber.agent.tools.session_search",
            "kyber.agent.tools.delegate",
            "kyber.agent.tools.mcp",
        ]
        
        for mod_name in _TOOL_MODULES:
            try:
                importlib.import_module(mod_name)
            except Exception as e:
                logger.debug(f"Could not import {mod_name}: {e}")
        
        self._discovered = True
    
    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())
    
    def __len__(self) -> int:
        return len(self._tools)
    
    def __contains__(self, name: str) -> bool:
        return name in self._tools
    
    def summary(self) -> str:
        """Human-readable summary of registered tools."""
        toolsets = self.get_all_toolsets()
        lines = [f"Tool Registry: {len(self._tools)} tools in {len(toolsets)} toolsets"]
        for ts, names in sorted(toolsets.items()):
            available = sum(1 for n in names if self._tools[n].is_available())
            lines.append(f"  [{ts}] {available}/{len(names)} available: {', '.join(sorted(names))}")
        return "\n".join(lines)


# ── Singleton instance ──────────────────────────────────────────────
# Tools import this and call registry.register() at module level.
# This is the hermes pattern: import = registration, no decorators needed.
registry = ToolRegistry()
