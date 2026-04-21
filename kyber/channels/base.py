"""Base channel interface for chat platforms."""

from abc import ABC, abstractmethod
from typing import Any

from kyber.bus.events import InboundMessage, OutboundMessage
from kyber.bus.queue import MessageBus


class BaseChannel(ABC):
    """
    Abstract base class for chat channel implementations.
    
    Each channel (Telegram, Discord, etc.) should implement this interface
    to integrate with the kyber message bus.
    """
    
    name: str = "base"
    
    # Filled in later by ChannelManager.attach_agent so slash commands
    # that need live agent state (/cancel, /usage) can reach it. Stays
    # None on instances that were never attached, in which case those
    # commands degrade with a friendly message.
    agent: Any = None

    def __init__(self, config: Any, bus: MessageBus):
        """
        Initialize the channel.
        
        Args:
            config: Channel-specific configuration.
            bus: The message bus for communication.
        """
        self.config = config
        self.bus = bus
        self._running = False
    
    @abstractmethod
    async def start(self) -> None:
        """
        Start the channel and begin listening for messages.
        
        This should be a long-running async task that:
        1. Connects to the chat platform
        2. Listens for incoming messages
        3. Forwards messages to the bus via _handle_message()
        """
        pass
    
    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel and clean up resources."""
        pass
    
    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """
        Send a message through this channel.
        
        Args:
            msg: The message to send.
        """
        pass
    
    def is_allowed(self, sender_id: str) -> bool:
        """
        Check if a sender is allowed to use this bot.
        
        Args:
            sender_id: The sender's identifier.
        
        Returns:
            True if allowed, False otherwise.
        """
        allow_list = getattr(self.config, "allow_from", [])
        
        # If no allow list, allow everyone
        if not allow_list:
            return True
        
        sender_str = str(sender_id)
        if sender_str in allow_list:
            return True
        if "|" in sender_str:
            for part in sender_str.split("|"):
                if part and part in allow_list:
                    return True
        return False
    
    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None
    ) -> None:
        """
        Handle an incoming message from the chat platform.

        This method checks permissions, handles slash commands directly,
        and forwards ordinary messages to the bus.

        Args:
            sender_id: The sender's identifier.
            chat_id: The chat/channel identifier.
            content: Message text content.
            media: Optional list of media URLs.
            metadata: Optional channel-specific metadata.
        """
        if not self.is_allowed(sender_id):
            return

        # Slash commands are handled inline — no bus, no LLM, no token
        # cost. Every channel that inherits from BaseChannel gets this
        # behaviour automatically; no per-channel wiring needed.
        if await self._maybe_handle_slash_command(sender_id, chat_id, content):
            return

        msg = InboundMessage(
            channel=self.name,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            content=content,
            media=media or [],
            metadata=metadata or {}
        )

        await self.bus.publish_inbound(msg)

    async def _maybe_handle_slash_command(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
    ) -> bool:
        """If ``content`` is a slash command, dispatch + reply. Return True.

        Slash dispatch needs the live AgentCore for commands like /cancel
        and /usage. Channels that don't hand us an agent in the
        constructor still get the simpler commands (/help, /model, /peers,
        etc.) that only touch config.
        """
        from kyber.commands import CommandContext, dispatch, is_slash_command

        if not is_slash_command(content):
            return False

        try:
            from kyber.config.loader import load_config

            cfg = load_config()
        except Exception:
            cfg = None

        ctx = CommandContext(
            channel=self.name,
            session_id=str(chat_id),
            sender_id=str(sender_id),
            sender_name=str(sender_id),
            agent=getattr(self, "agent", None),
            session_key=f"{self.name}:{chat_id}",
            config=cfg,
            supports_markdown=True,
        )
        result = await dispatch(content, ctx)
        if result is None:
            # `is_slash_command` passed but dispatcher didn't consume it —
            # fall back to the normal bus path rather than eating the
            # message.
            return False

        reply = (result.reply_text or "").strip()
        if reply:
            await self.bus.publish_outbound(
                OutboundMessage(channel=self.name, chat_id=str(chat_id), content=reply)
            )
        if result.reset_session and getattr(self, "agent", None) is not None:
            try:
                reset = getattr(self.agent, "reset_session", None)
                if callable(reset):
                    maybe = reset(f"{self.name}:{chat_id}")
                    if hasattr(maybe, "__await__"):
                        await maybe
            except Exception:
                pass
        return True
    
    @property
    def is_running(self) -> bool:
        """Check if the channel is running."""
        return self._running
