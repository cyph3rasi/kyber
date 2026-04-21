"""Configuration schema using Pydantic."""

from pathlib import Path
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class WhatsAppConfig(BaseModel):
    """WhatsApp channel configuration."""
    enabled: bool = False
    bridge_url: str = "ws://localhost:3001"
    allow_from: list[str] = Field(default_factory=list)  # Allowed phone numbers


class TelegramConfig(BaseModel):
    """Telegram channel configuration."""
    enabled: bool = False
    token: str = ""  # Bot token from @BotFather
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs or usernames
    proxy: str | None = None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"


class DiscordConfig(BaseModel):
    """Discord channel configuration."""
    enabled: bool = False
    token: str = ""  # Bot token from Discord Developer Portal
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs or usernames
    allow_guilds: list[str] = Field(default_factory=list)  # Allowed guild IDs (servers)
    allow_channels: list[str] = Field(default_factory=list)  # Allowed channel IDs
    require_mention_in_guilds: bool = False  # Only respond in guilds when mentioned/replied
    max_attachment_mb: int = 20  # Max attachment size to download
    typing_indicator: bool = True  # Show "typing" while processing


class ChannelsConfig(BaseModel):
    """Configuration for chat channels."""
    whatsapp: WhatsAppConfig = Field(default_factory=WhatsAppConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)


class AgentDefaults(BaseModel):
    """Default agent configuration."""
    workspace: str = "~/.kyber/workspace"
    provider: str = "openrouter"  # Which provider to use (references a key in providers or custom name)
    max_tokens: int = 8192
    timezone: str = ""  # User timezone (e.g. "America/New_York"). Empty = system local time.

    # --- Token-saving knobs (2026.4.21.54) ---

    # Enable prompt caching hints for providers that support them (Anthropic
    # native API or its OpenAI-compatible endpoint). OpenAI and OpenRouter
    # auto-cache stable prefixes ≥1024 tokens regardless of this flag — the
    # setting only controls explicit cache_control markers.
    enable_prompt_cache: bool = True

    # Hard cap on the size of a single tool result before it enters the
    # message list. Tool results this large almost always contain noise the
    # LLM only needs a summary of.
    tool_result_max_chars: int = 20_000

    # How many recent tool results to keep in full inside the ongoing LLM
    # loop. Older tool messages get compacted to a short summary so they
    # don't dominate the context window on long multi-step tasks.
    tool_result_keep_recent: int = 3

    # When session history exceeds this many messages, older turns are
    # summarized into a compact system block instead of being sent
    # verbatim. Keeps long chats cheap without losing continuity.
    history_summary_trigger: int = 30
    history_summary_keep_recent: int = 12

    # Kept only so old configs that still set `temperature` continue to
    # parse. We never send temperature to any provider now — modern models
    # either reject it (Anthropic) or handle it server-side (Codex).
    temperature: float = 0.7


class AgentsConfig(BaseModel):
    """Agent configuration."""
    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ProviderConfig(BaseModel):
    """LLM provider configuration."""
    api_key: str = ""
    api_base: str | None = None
    model: str = ""  # Selected model for this provider


class CustomProviderConfig(BaseModel):
    """Custom OpenAI-compatible provider configuration."""
    name: str = ""
    api_base: str = ""
    api_key: str = ""
    model: str = ""  # Selected model for this provider


class ChatGPTSubscriptionProviderConfig(BaseModel):
    """ChatGPT Plus/Pro subscription provider configuration."""
    model: str = "gpt-5.3-codex"


class ClaudeSubscriptionProviderConfig(BaseModel):
    """DEPRECATED placeholder for old configs.

    The Claude Pro/Max OAuth integration was removed in 2026.4.21.53
    because Anthropic has been banning accounts that use Claude Code's
    OAuth token from non-CLI clients. This class still exists so older
    ``config.json`` files that mention ``claude_subscription`` continue
    to parse cleanly; the gateway refuses to start against it and
    prints a migration hint pointing users at Codex, OpenRouter, or a
    direct Anthropic API key instead.
    """
    model: str = ""


class ProvidersConfig(BaseModel):
    """Configuration for LLM providers."""
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    chatgpt_subscription: ChatGPTSubscriptionProviderConfig = Field(
        default_factory=ChatGPTSubscriptionProviderConfig
    )
    claude_subscription: ClaudeSubscriptionProviderConfig = Field(
        default_factory=ClaudeSubscriptionProviderConfig
    )
    custom: list[CustomProviderConfig] = Field(default_factory=list)


# Built-in provider names (order matters for fallback detection).
# ``claude_subscription`` is intentionally absent — the integration was
# removed because Anthropic bans accounts that reuse Claude Code's OAuth
# token from non-CLI clients. The schema class for it still exists so
# old configs parse, but the gateway refuses to start against it.
BUILTIN_PROVIDERS = [
    "openrouter",
    "deepseek",
    "anthropic",
    "openai",
    "chatgpt_subscription",
]


class GatewayConfig(BaseModel):
    """Gateway/server configuration."""
    host: str = "0.0.0.0"
    port: int = 18790


class NetworkConfig(BaseModel):
    """Kyber network (multi-machine) configuration.

    Actual pairing state (peer ids, HMAC secrets, host URL) lives in
    ``~/.kyber/network.json`` because it contains secrets. This config
    section only decides what this instance should DO — whether to host,
    act as a spoke, or stay alone.
    """

    # "standalone" | "host" | "spoke"
    role: str = "standalone"

    # Tools in the local tool registry that other paired Kybers are allowed
    # to invoke remotely. The default ``["*"]`` exposes everything
    # registered on this node — appropriate for a fleet you own end to
    # end. Replace with an explicit list (e.g. ``["exec", "read_file"]``)
    # on shared/untrusted machines, or ``[]`` to disable remote invocation
    # entirely. Matching is case-sensitive and exact; ``*`` is not a glob,
    # it's a literal "everything" sentinel.
    exposed_tools: list[str] = Field(default_factory=lambda: ["*"])


class WebSearchConfig(BaseModel):
    """Web search tool configuration."""
    api_key: str = ""  # Brave Search API key
    max_results: int = 5


class WebToolsConfig(BaseModel):
    """Web tools configuration."""
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class DashboardConfig(BaseModel):
    """Web dashboard configuration."""
    # Bind on all interfaces by default so Tailscale / LAN clients can reach
    # the dashboard on a VPS or homelab. The bearer token still gates every
    # request — binding to 0.0.0.0 doesn't expose anything without it.
    host: str = "0.0.0.0"
    port: int = 18890
    auth_token: str = ""  # Bearer token for dashboard access
    allowed_hosts: list[str] = Field(default_factory=list)  # Extra allowed Host headers


class ExecToolConfig(BaseModel):
    """Shell exec tool configuration."""
    restrict_to_workspace: bool = False  # If true, block commands accessing paths outside workspace


class SkillScannerConfig(BaseModel):
    """Cisco AI Defense skill-scanner configuration."""
    llm_api_key: str = ""  # API key for LLM analyzer (auto-detected from providers if empty)
    llm_model: str = ""  # Model for LLM analyzer (e.g. "claude-3-5-sonnet-20241022")
    virustotal_api_key: str = ""  # VirusTotal API key for binary scanning
    ai_defense_api_key: str = ""  # Cisco AI Defense API key
    use_llm: bool = True  # Enable LLM-based semantic analysis (uses active provider key)
    use_behavioral: bool = True  # Enable behavioral dataflow analysis
    use_virustotal: bool = False  # Enable VirusTotal binary scanning (requires API key)
    use_aidefense: bool = False  # Enable Cisco AI Defense cloud scanning (requires API key)
    enable_meta: bool = True  # Enable meta-analyzer for false positive filtering


class MCPServerConfig(BaseModel):
    """Single MCP server definition (stdio transport)."""
    name: str = ""
    enabled: bool = True
    transport: str = "stdio"  # "stdio" or "http"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = 30


class MCPToolsConfig(BaseModel):
    """MCP tool integration config."""
    servers: list[MCPServerConfig] = Field(default_factory=list)


class ChannelToolPolicy(BaseModel):
    """Per-channel tool allow/deny policy.

    Channels are identified by the value of ``InboundMessage.channel``
    (``"discord"``, ``"telegram"``, ``"whatsapp"``, ``"dashboard"``,
    ``"cli"``, ``"tui"``, ...). Allowing a subset of tools per channel
    shrinks the tool schema sent on every LLM call — a big savings when
    long loops repeatedly re-send the same catalog.

    Rules:
    - Empty ``allow`` (the default) means *every* tool in the registry.
    - ``allow`` accepts a wildcard ``"*"`` that means "everything".
    - Entries in ``deny`` are always removed, even if ``allow`` is
      ``["*"]``. Deny wins over allow.
    """

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class ToolsConfig(BaseModel):
    """Tools configuration."""
    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    skill_scanner: SkillScannerConfig = Field(default_factory=SkillScannerConfig)
    mcp: MCPToolsConfig = Field(default_factory=MCPToolsConfig)

    # Per-channel tool exposure. Missing channel = no filter (full catalog).
    # Keys match ``InboundMessage.channel`` (lowercase).
    per_channel: dict[str, ChannelToolPolicy] = Field(default_factory=dict)


class Config(BaseSettings):
    """Root configuration for kyber."""
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    
    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path (ensured to exist)."""
        from kyber.utils.helpers import ensure_dir
        return ensure_dir(Path(self.agents.defaults.workspace).expanduser())

    def _preferred_provider(self) -> str | None:
        """Return the explicitly configured provider, if any."""
        value = (self.agents.defaults.provider or "").strip().lower()
        return value or None

    def _find_custom_provider(self, name: str) -> CustomProviderConfig | None:
        """Find a custom provider by name."""
        for cp in self.providers.custom:
            if cp.name.strip().lower() == name:
                return cp
        return None

    def get_api_key(self) -> str | None:
        """Get API key for the active provider."""
        preferred = self._preferred_provider()
        if preferred:
            if preferred in ("chatgpt_subscription", "claude_subscription"):
                # claude_subscription is a deprecated graveyard entry;
                # handled further down in get_provider_details.
                return None
            # Check built-in providers first
            provider = getattr(self.providers, preferred, None)
            if provider and isinstance(provider, ProviderConfig):
                return provider.api_key or None
            # Check custom providers
            cp = self._find_custom_provider(preferred)
            if cp:
                return cp.api_key or None
            return None
        # Fallback: first built-in with a key
        for name in BUILTIN_PROVIDERS:
            prov = getattr(self.providers, name)
            if getattr(prov, "api_key", ""):
                return prov.api_key
        return None
    
    def get_api_base(self) -> str | None:
        """Get API base URL for the active provider."""
        preferred = self._preferred_provider()
        if preferred:
            if preferred in ("chatgpt_subscription", "claude_subscription"):
                return None
            # Check custom providers
            cp = self._find_custom_provider(preferred)
            if cp:
                return cp.api_base or None
            # Built-in overrides
            if preferred == "openrouter":
                return self.providers.openrouter.api_base or "https://openrouter.ai/api/v1"
            return None
        # Fallback
        if self.providers.openrouter.api_key:
            return self.providers.openrouter.api_base or "https://openrouter.ai/api/v1"
        return None

    def get_provider_name(self) -> str | None:
        """Return the selected provider name."""
        preferred = self._preferred_provider()
        if preferred:
            if hasattr(self.providers, preferred):
                return preferred
            if self._find_custom_provider(preferred):
                return preferred
            return None
        for name in BUILTIN_PROVIDERS:
            prov = getattr(self.providers, name)
            if getattr(prov, "api_key", ""):
                return name
        return None

    def get_model(self) -> str:
        """Get the model from the active provider's config."""
        preferred = self._preferred_provider()
        if preferred:
            if preferred == "chatgpt_subscription":
                model = (self.providers.chatgpt_subscription.model or "").strip()
                return model or "gpt-5.3-codex"
            if preferred == "claude_subscription":
                # Deprecated; surface the last-known model so the gateway's
                # migration error can reference it, but callers should not
                # actually use this provider.
                return (self.providers.claude_subscription.model or "").strip()
            # Check built-in
            provider = getattr(self.providers, preferred, None)
            if provider and isinstance(provider, ProviderConfig) and provider.model:
                return provider.model
            # Check custom
            cp = self._find_custom_provider(preferred)
            if cp and cp.model:
                return cp.model
        # Fallback: first built-in with a key and model set
        for name in BUILTIN_PROVIDERS:
            prov = getattr(self.providers, name)
            if getattr(prov, "api_key", "") and getattr(prov, "model", ""):
                return prov.model
        return "openrouter/anthropic/claude-sonnet-4"

    def is_custom_provider(self) -> bool:
        """Check if the active provider is a custom one."""
        preferred = self._preferred_provider()
        if not preferred:
            return False
        return self._find_custom_provider(preferred) is not None

    def get_provider_details(self) -> dict:
        """Get full provider details (api_key, api_base, model, etc.) for the active provider."""
        prov_name = self.get_provider_name()
        if prov_name:
            name = prov_name.strip().lower()
            if name == "chatgpt_subscription":
                return {
                    # Runtime provider remains OpenAI; auth happens via Codex OAuth.
                    "provider_name": "openai",
                    "configured_provider_name": "chatgpt_subscription",
                    "api_key": None,
                    "api_base": None,
                    "is_custom": False,
                    "is_subscription": True,
                    "subscription_kind": "chatgpt",
                    "model": self.get_model(),
                }
            if name == "claude_subscription":
                # Deprecated — flag it so the factory prints a migration
                # message instead of trying to import a provider that no
                # longer exists.
                return {
                    "provider_name": "anthropic",
                    "configured_provider_name": "claude_subscription",
                    "api_key": None,
                    "api_base": None,
                    "is_custom": False,
                    "is_subscription": True,
                    "subscription_kind": "claude",
                    "is_deprecated": True,
                    "model": self.get_model(),
                }
            # Check custom providers
            cp = self._find_custom_provider(name)
            if cp:
                return {
                    "provider_name": name,
                    "api_key": cp.api_key or None,
                    "api_base": cp.api_base or None,
                    "is_custom": True,
                    "model": cp.model or self.get_model(),
                }
            # Check built-in
            prov = getattr(self.providers, name, None)
            if prov and isinstance(prov, ProviderConfig):
                api_base = prov.api_base
                if name == "openrouter":
                    api_base = api_base or "https://openrouter.ai/api/v1"
                return {
                    "provider_name": name,
                    "api_key": prov.api_key or None,
                    "api_base": api_base,
                    "is_custom": False,
                    "model": prov.model or self.get_model(),
                }
        # Fallback to default
        return {
            "provider_name": self.get_provider_name(),
            "api_key": self.get_api_key(),
            "api_base": self.get_api_base(),
            "is_custom": self.is_custom_provider(),
            "model": self.get_model(),
        }

    model_config = {
        "env_prefix": "KYBER_",
        "env_nested_delimiter": "__",
    }
