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


class FeishuConfig(BaseModel):
    """Feishu/Lark channel configuration using WebSocket long connection."""
    enabled: bool = False
    app_id: str = ""  # App ID from Feishu Open Platform
    app_secret: str = ""  # App Secret from Feishu Open Platform
    encrypt_key: str = ""  # Encrypt Key for event subscription (optional)
    verification_token: str = ""  # Verification Token for event subscription (optional)
    allow_from: list[str] = Field(default_factory=list)  # Allowed user open_ids


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
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)


class AgentDefaults(BaseModel):
    """Default agent configuration."""
    workspace: str = "~/.kyber/workspace"
    model: str = "google/gemini-2.5-flash-preview"
    provider: str = "openrouter"  # Default provider
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


class ProvidersConfig(BaseModel):
    """Configuration for LLM providers."""
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)


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
    
    def get_api_key(self) -> str | None:
        """Get API key in priority order, unless a provider is explicitly set."""
        preferred = self._preferred_provider()
        if preferred:
            provider = getattr(self.providers, preferred, None)
            return provider.api_key if provider else None
        return (
            self.providers.openrouter.api_key or
            self.providers.deepseek.api_key or
            self.providers.anthropic.api_key or
            self.providers.openai.api_key or
            self.providers.gemini.api_key or
            self.providers.zhipu.api_key or
            self.providers.groq.api_key or
            self.providers.vllm.api_key or
            None
        )
    
    def get_api_base(self) -> str | None:
        """Get API base URL if using OpenRouter, Zhipu or vLLM."""
        preferred = self._preferred_provider()
        if preferred == "openrouter":
            return self.providers.openrouter.api_base or "https://openrouter.ai/api/v1"
        if preferred == "zhipu":
            return self.providers.zhipu.api_base
        if preferred == "vllm":
            return self.providers.vllm.api_base
        if preferred:
            return None
        if self.providers.openrouter.api_key:
            return self.providers.openrouter.api_base or "https://openrouter.ai/api/v1"
        if self.providers.zhipu.api_key:
            return self.providers.zhipu.api_base
        if self.providers.vllm.api_base:
            return self.providers.vllm.api_base
        return None

    def get_provider_name(self) -> str | None:
        """Return the selected provider name based on configured keys."""
        preferred = self._preferred_provider()
        if preferred:
            return preferred if hasattr(self.providers, preferred) else None
        if self.providers.openrouter.api_key:
            return "openrouter"
        if self.providers.deepseek.api_key:
            return "deepseek"
        if self.providers.anthropic.api_key:
            return "anthropic"
        if self.providers.openai.api_key:
            return "openai"
        if self.providers.gemini.api_key:
            return "gemini"
        if self.providers.zhipu.api_key:
            return "zhipu"
        if self.providers.groq.api_key:
            return "groq"
        if self.providers.vllm.api_base or self.providers.vllm.api_key:
            return "vllm"
        return None
    
    class Config:
        env_prefix = "KYBER_"
        env_nested_delimiter = "__"
