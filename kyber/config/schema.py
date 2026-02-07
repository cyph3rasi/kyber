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
    require_mention_in_guilds: bool = True  # Only respond in guilds when mentioned/replied
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
    temperature: float = 0.7
    max_tool_iterations: int = 20


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


class ToolsConfig(BaseModel):
    """Tools configuration."""
    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)


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
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

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

    class Config:
        env_prefix = "KYBER_"
        env_nested_delimiter = "__"
