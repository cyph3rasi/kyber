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
    chat_provider: str = ""  # Provider for conversational replies (empty = use default provider)
    task_provider: str = ""  # Provider for background workers/tasks (empty = use default provider)
    max_tokens: int = 8192
    temperature: float = 0.7
    timezone: str = ""  # User timezone (e.g. "America/New_York"). Empty = system local time.
    # Background outbound messages (progress pings + completion notifications).
    # If false, tasks still run, but users only see updates when they ask for status.
    background_progress_updates: bool = True


class AgentsConfig(BaseModel):
    """Agent configuration."""
    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ProviderConfig(BaseModel):
    """LLM provider configuration."""
    api_key: str = ""
    api_base: str | None = None
    model: str = ""  # Selected model for this provider (legacy / fallback)
    chat_model: str = ""  # Model used for conversational replies
    task_model: str = ""  # Model used for background workers/tasks


class CustomProviderConfig(BaseModel):
    """Custom OpenAI-compatible provider configuration."""
    name: str = ""
    api_base: str = ""
    api_key: str = ""
    model: str = ""  # Selected model for this provider (legacy / fallback)
    chat_model: str = ""  # Model used for conversational replies
    task_model: str = ""  # Model used for background workers/tasks


class ProvidersConfig(BaseModel):
    """Configuration for LLM providers."""
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    custom: list[CustomProviderConfig] = Field(default_factory=list)


# Built-in provider names (order matters for fallback detection)
BUILTIN_PROVIDERS = ["openrouter", "deepseek", "anthropic", "openai", "gemini", "groq"]


class GatewayConfig(BaseModel):
    """Gateway/server configuration."""
    host: str = "0.0.0.0"
    port: int = 18790


class WebSearchConfig(BaseModel):
    """Web search tool configuration."""
    api_key: str = ""  # Brave Search API key
    max_results: int = 5


class WebToolsConfig(BaseModel):
    """Web tools configuration."""
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class DashboardConfig(BaseModel):
    """Web dashboard configuration."""
    host: str = "127.0.0.1"
    port: int = 18890
    auth_token: str = ""  # Bearer token for dashboard access
    allowed_hosts: list[str] = Field(default_factory=list)  # Extra allowed Host headers


class ExecToolConfig(BaseModel):
    """Shell exec tool configuration."""
    timeout: int = 60
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


class ToolsConfig(BaseModel):
    """Tools configuration."""
    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    skill_scanner: SkillScannerConfig = Field(default_factory=SkillScannerConfig)


class Config(BaseSettings):
    """Root configuration for kyber."""
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    
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
            if prov.api_key:
                return prov.api_key
        return None
    
    def get_api_base(self) -> str | None:
        """Get API base URL for the active provider."""
        preferred = self._preferred_provider()
        if preferred:
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
            if prov.api_key:
                return name
        return None

    def get_model(self) -> str:
        """Get the model from the active provider's config."""
        preferred = self._preferred_provider()
        if preferred:
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
            if prov.api_key and prov.model:
                return prov.model
        return "openrouter/google/gemini-3-flash-preview"

    def is_custom_provider(self) -> bool:
        """Check if the active provider is a custom one."""
        preferred = self._preferred_provider()
        if not preferred:
            return False
        return self._find_custom_provider(preferred) is not None

    # ── Role-based provider/model resolution ──

    def _resolve_provider_details(self, provider_name: str) -> dict:
        """Resolve api_key, api_base, is_custom, and provider_name for a given provider."""
        name = provider_name.strip().lower()
        if not name:
            return {}
        # Check custom providers
        cp = self._find_custom_provider(name)
        if cp:
            return {
                "provider_name": name,
                "api_key": cp.api_key or None,
                "api_base": cp.api_base or None,
                "is_custom": True,
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
            }
        return {}

    def _get_model_for_role(self, provider_name: str, role: str) -> str:
        """Get the model for a specific role (chat or task) from a provider.

        Falls back: role-specific model → legacy ``model`` field → empty string.
        """
        name = provider_name.strip().lower()
        if not name:
            return ""
        cp = self._find_custom_provider(name)
        if cp:
            if role == "chat":
                return cp.chat_model or cp.model or ""
            return cp.task_model or cp.model or ""
        prov = getattr(self.providers, name, None)
        if prov and isinstance(prov, ProviderConfig):
            if role == "chat":
                return prov.chat_model or prov.model or ""
            return prov.task_model or prov.model or ""
        return ""

    def get_chat_provider_name(self) -> str | None:
        """Return the provider name to use for chat (conversational) responses."""
        explicit = (self.agents.defaults.chat_provider or "").strip().lower()
        return explicit or self.get_provider_name()

    def get_task_provider_name(self) -> str | None:
        """Return the provider name to use for background tasks/workers."""
        explicit = (self.agents.defaults.task_provider or "").strip().lower()
        return explicit or self.get_provider_name()

    def get_chat_model(self) -> str:
        """Get the model to use for chat responses."""
        prov = self.get_chat_provider_name()
        if prov:
            m = self._get_model_for_role(prov, "chat")
            if m:
                return m
        return self.get_model()

    def get_task_model(self) -> str:
        """Get the model to use for background tasks/workers."""
        prov = self.get_task_provider_name()
        if prov:
            m = self._get_model_for_role(prov, "task")
            if m:
                return m
        return self.get_model()

    def get_chat_provider_details(self) -> dict:
        """Get full provider details (api_key, api_base, etc.) for chat."""
        prov = self.get_chat_provider_name()
        if prov:
            details = self._resolve_provider_details(prov)
            if details:
                details["model"] = self.get_chat_model()
                return details
        # Fallback to default
        return {
            "provider_name": self.get_provider_name(),
            "api_key": self.get_api_key(),
            "api_base": self.get_api_base(),
            "is_custom": self.is_custom_provider(),
            "model": self.get_model(),
        }

    def get_task_provider_details(self) -> dict:
        """Get full provider details (api_key, api_base, etc.) for tasks."""
        prov = self.get_task_provider_name()
        if prov:
            details = self._resolve_provider_details(prov)
            if details:
                details["model"] = self.get_task_model()
                return details
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
