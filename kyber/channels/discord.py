"""Discord channel implementation using discord.py."""

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from kyber.bus.events import OutboundMessage
from kyber.bus.queue import MessageBus
from kyber.channels.base import BaseChannel
from kyber.config.schema import DiscordConfig

try:
    import discord
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False
    discord = None


class DiscordChannel(BaseChannel):
    """
    Discord channel implementation.
    
    Supports DMs and guild channels. For guilds, it can be configured to
    only respond when mentioned or replied to, to avoid noisy behavior.
    """
    
    name = "discord"
    
    def __init__(self, config: DiscordConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: DiscordConfig = config
        self._client: "discord.Client | None" = None
        self._ready = asyncio.Event()
        self._bot_user_id: int | None = None
        self._typing_tasks: dict[int, asyncio.Task] = {}
        self._typing_counts: dict[int, int] = {}
    
    async def start(self) -> None:
        """Start the Discord bot."""
        if not DISCORD_AVAILABLE:
            logger.error("Discord SDK not installed. Run: pip install discord.py")
            return
        
        if not self.config.token:
            logger.error("Discord bot token not configured")
            return
        
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.guild_messages = True
        intents.dm_messages = True
        intents.message_content = True
        
        self._client = discord.Client(intents=intents)
        
        @self._client.event
        async def on_ready():
            if not self._client or not self._client.user:
                return
            self._bot_user_id = self._client.user.id
            self._ready.set()
            logger.info(f"Discord bot connected as {self._client.user}")
        
        @self._client.event
        async def on_message(message: "discord.Message"):
            await self._on_message(message)
        
        self._running = True
        try:
            await self._client.start(self.config.token)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Discord client error: {e}")
        finally:
            self._running = False
    
    async def stop(self) -> None:
        """Stop the Discord bot."""
        self._running = False
        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()
        self._typing_counts.clear()
        if self._client:
            await self._client.close()
            self._client = None
        self._ready.clear()
    
    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Discord."""
        if not self._client:
            logger.warning("Discord client not initialized")
            return
        
        if not self._ready.is_set():
            logger.warning("Discord client not ready yet")
            return
        
        try:
            channel_id = int(msg.chat_id)
        except ValueError:
            logger.error(f"Invalid Discord channel id: {msg.chat_id}")
            return

        # Stop typing indicator for this channel when we send a response
        self._stop_typing(channel_id)
        
        try:
            channel = self._client.get_channel(channel_id)
            if channel is None:
                channel = await self._client.fetch_channel(channel_id)
            if channel is None:
                logger.error(f"Discord channel not found: {channel_id}")
                return
            
            allowed_mentions = discord.AllowedMentions.none()
            chunks = [chunk for chunk in self._split_message(msg.content) if chunk.strip()]
            if not chunks:
                logger.warning("Skipping empty Discord message")
                return
            for chunk in chunks:
                await channel.send(chunk, allowed_mentions=allowed_mentions)
        except Exception as e:
            logger.error(f"Error sending Discord message: {e}")
    
    async def _on_message(self, message: "discord.Message") -> None:
        """Handle incoming messages from Discord."""
        if not self._running:
            return
        
        if not message.author or message.author.bot:
            return
        
        if self._bot_user_id and message.author.id == self._bot_user_id:
            return
        
        if not await self._should_process_message(message):
            return
        
        sender_id = str(message.author.id)
        if message.author.name:
            sender_id = f"{sender_id}|{message.author.name}"
        
        chat_id = str(message.channel.id)
        is_dm = message.guild is None

        # Start typing indicator while we process the message
        self._start_typing(message.channel.id, message.channel)
        
        content_parts: list[str] = []
        media_paths: list[str] = []
        
        if message.content:
            content_parts.append(message.content)
        
        if message.stickers:
            sticker_names = ", ".join(s.name for s in message.stickers)
            content_parts.append(f"[sticker: {sticker_names}]")
        
        if message.attachments:
            for attachment in message.attachments:
                attachment_path = await self._download_attachment(attachment)
                if attachment_path:
                    media_paths.append(str(attachment_path))
                    content_parts.append(f"[attachment: {attachment_path}]")
                else:
                    content_parts.append(f"[attachment: {attachment.filename} skipped]")
        
        content = "\n".join(content_parts) if content_parts else "[empty message]"
        
        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            media=media_paths,
            metadata={
                "message_id": str(message.id),
                "user_id": str(message.author.id),
                "username": message.author.name,
                "display_name": message.author.display_name,
                "guild_id": str(message.guild.id) if message.guild else None,
                "channel_id": str(message.channel.id),
                "is_dm": is_dm,
            },
        )
    
    async def _should_process_message(self, message: "discord.Message") -> bool:
        """Check guild/channel restrictions and mention rules."""
        # Allowlist check via BaseChannel
        sender_id = str(message.author.id)
        if message.author.name:
            sender_id = f"{sender_id}|{message.author.name}"
        if not self.is_allowed(sender_id):
            return False
        
        # Restrict by guild/channel if configured
        if message.guild:
            if self.config.allow_guilds and str(message.guild.id) not in self.config.allow_guilds:
                return False
            if self.config.allow_channels and str(message.channel.id) not in self.config.allow_channels:
                return False
            
            if self.config.require_mention_in_guilds:
                if self._client and self._client.user in message.mentions:
                    return True
                if await self._is_reply_to_bot(message):
                    return True
                return False
        
        return True
    
    async def _is_reply_to_bot(self, message: "discord.Message") -> bool:
        """Return True if the message replies to the bot."""
        if not message.reference:
            return False
        
        resolved = message.reference.resolved
        if resolved and hasattr(resolved, "author"):
            return self._bot_user_id is not None and resolved.author.id == self._bot_user_id
        
        # If not resolved, avoid extra fetches for safety
        return False

    def _start_typing(self, channel_id: int, channel: "discord.abc.Messageable") -> None:
        """Start typing indicator for a channel (ref-counted)."""
        if not self.config.typing_indicator:
            return
        
        self._typing_counts[channel_id] = self._typing_counts.get(channel_id, 0) + 1
        if channel_id in self._typing_tasks:
            return
        
        async def _typing_loop():
            try:
                # Trigger typing periodically while processing
                while self._running:
                    async with channel.typing():
                        await asyncio.sleep(7)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"Typing indicator error: {e}")
        
        self._typing_tasks[channel_id] = asyncio.create_task(_typing_loop())

    def _stop_typing(self, channel_id: int) -> None:
        """Stop typing indicator for a channel (ref-counted)."""
        if channel_id not in self._typing_counts:
            return
        
        self._typing_counts[channel_id] = max(0, self._typing_counts[channel_id] - 1)
        if self._typing_counts[channel_id] > 0:
            return
        
        self._typing_counts.pop(channel_id, None)
        task = self._typing_tasks.pop(channel_id, None)
        if task:
            task.cancel()
    
    async def _download_attachment(self, attachment: "discord.Attachment") -> Path | None:
        """Download an attachment with size limits."""
        max_bytes = max(0, self.config.max_attachment_mb) * 1024 * 1024
        if attachment.size and attachment.size > max_bytes:
            logger.info(
                f"Skipping attachment {attachment.filename} ({attachment.size} bytes) "
                f"over limit {max_bytes} bytes"
            )
            return None
        
        try:
            media_dir = Path.home() / ".kyber" / "media"
            media_dir.mkdir(parents=True, exist_ok=True)
            
            safe_name = Path(attachment.filename).name
            file_path = media_dir / f"discord_{attachment.id}_{safe_name}"
            await attachment.save(file_path, use_cached=True)
            return file_path
        except Exception as e:
            logger.warning(f"Failed to download attachment {attachment.filename}: {e}")
            return None
    
    def _split_message(self, content: str, limit: int = 2000) -> list[str]:
        """Split long messages to fit Discord limits."""
        if not content:
            return []
        if len(content) <= limit:
            return [content]
        
        chunks = []
        remaining = content
        while len(remaining) > limit:
            split_at = remaining.rfind("\n", 0, limit)
            if split_at <= 0:
                split_at = limit
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip("\n")
        if remaining:
            chunks.append(remaining)
        return chunks
