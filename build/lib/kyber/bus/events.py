"""Event types for the message bus."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class InboundMessage:
    """Message received from a chat channel."""
    
    channel: str  # telegram, discord, slack, whatsapp
    sender_id: str  # User identifier
    chat_id: str  # Chat/channel identifier
    content: str  # Message text
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)  # Media URLs
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data
    
    @property
    def session_key(self) -> str:
        """Unique key for session identification."""
        return f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    """Message to send to a chat channel."""
    
    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # If True, this is a background update (progress, completion) that
    # should not cancel the typing indicator for the main response.
    is_background: bool = False


@dataclass
class StatusUpdate:
    """Status update for tool execution progress.
    
    Used to show the user what the agent is doing while it works.
    Channels that support message editing (Discord, Telegram) can
    display a live-updating status message.
    """
    
    channel: str
    chat_id: str
    kind: str  # "start", "tool", "end"
    tool_name: str = ""
    tool_line: str = ""  # Formatted line like "┊ 🔍 search    query  1.2s"
    duration: float = 0.0


