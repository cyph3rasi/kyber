"""Discord channel implementation using discord.py."""

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from kyber.bus.events import OutboundMessage
from kyber.bus.queue import MessageBus
from kyber.channels.base import BaseChannel
from kyber.channels.errors import PermanentDeliveryError, TemporaryDeliveryError
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
    
    Also supports status message updates - a "thinking" message that gets
    edited as tools are executed, showing progress to the user.
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
        # Status message tracking: (chat_id, status_key) -> (message, tool_lines)
        self._status_messages: dict[tuple[int, str], tuple["discord.Message", list[str]]] = {}
        self._status_lock = asyncio.Lock()
    
    async def start(self) -> None:
        """Start the Discord bot."""
        if not DISCORD_AVAILABLE:
            logger.error("Discord SDK not installed. Run: uv pip install discord.py")
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
            raise TemporaryDeliveryError("Discord client not initialized")

        if not self._ready.is_set():
            raise TemporaryDeliveryError("Discord client not ready yet")

        channel_id: int | None = None
        should_stop_typing = False
        channel: "discord.abc.Messageable | None" = None

        try:
            is_background = bool(getattr(msg, "is_background", False))
            channel_id = int(msg.chat_id)
            has_active_typing = self._typing_counts.get(channel_id, 0) > 0
            # If inbound handling already started typing, guarantee we stop it
            # even when channel lookup/sending fails later in this method.
            if not is_background and self.config.typing_indicator and has_active_typing:
                should_stop_typing = True

            channel = self._client.get_channel(channel_id)
            if channel is None:
                channel = await self._client.fetch_channel(channel_id)
            if channel is None:
                raise PermanentDeliveryError(f"Discord channel not found: {channel_id}")

            if not is_background and self.config.typing_indicator:
                if not has_active_typing:
                    # Non-Discord callers can send without having started typing
                    # yet. Start + stop here keeps UX consistent.
                    self._start_typing(channel_id, channel)
                    should_stop_typing = True

            allowed_mentions = discord.AllowedMentions.none()
            chunks = [chunk for chunk in self._split_message(msg.content) if chunk.strip()]
            if not chunks:
                raise PermanentDeliveryError("Empty Discord message content")

            for chunk in chunks:
                await channel.send(chunk, allowed_mentions=allowed_mentions)

        except ValueError:
            raise PermanentDeliveryError(f"Invalid Discord channel id: {msg.chat_id}")
        except (PermanentDeliveryError, TemporaryDeliveryError):
            raise
        except Exception as e:
            # Classify common discord.py errors so the dispatcher can retry smartly.
            if DISCORD_AVAILABLE and discord is not None:
                if isinstance(e, discord.Forbidden):
                    raise PermanentDeliveryError(f"Discord forbidden: {e}") from e
                if isinstance(e, discord.NotFound):
                    raise PermanentDeliveryError(f"Discord not found: {e}") from e
                if isinstance(e, discord.HTTPException):
                    raise TemporaryDeliveryError(f"Discord HTTP error: {e}") from e
            raise TemporaryDeliveryError(f"Discord send failed: {e}") from e
        finally:
            if should_stop_typing and channel_id is not None:
                self._stop_typing(channel_id)
    
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

        if self.config.typing_indicator:
            self._start_typing(message.channel.id, message.channel)
        
        sender_id = str(message.author.id)
        if message.author.name:
            sender_id = f"{sender_id}|{message.author.name}"
        
        chat_id = str(message.channel.id)
        is_dm = message.guild is None

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
            finally:
                task = self._typing_tasks.get(channel_id)
                if task is asyncio.current_task():
                    self._typing_tasks.pop(channel_id, None)
                self._typing_counts.pop(channel_id, None)

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

    def _status_scope(self, channel_id: int, status_key: str = "") -> tuple[int, str]:
        """Build a stable key for a specific in-flight request in a channel."""
        scope = (status_key or "").strip() or "default"
        return (channel_id, scope)

    async def start_status_message(self, channel_id: int, status_key: str = "") -> None:
        """Create or update a status message showing the agent is working.
        
        This sends an initial "thinking" message that will be edited as
        tools are executed.
        """
        if not self._client or not self._ready.is_set():
            return
        
        async with self._status_lock:
            await self._create_status_message_locked(channel_id, status_key)

    async def _create_status_message_locked(self, channel_id: int, status_key: str = "") -> None:
        """Create or replace status message while holding _status_lock."""
        scope_key = self._status_scope(channel_id, status_key)

        # Clean up any existing status message for this specific task scope
        if scope_key in self._status_messages:
            old_msg, _ = self._status_messages[scope_key]
            try:
                await old_msg.delete()
            except Exception:
                pass
        
        try:
            channel = self._client.get_channel(channel_id)
            if channel is None:
                channel = await self._client.fetch_channel(channel_id)
            if channel is None:
                return
            
            content = "💎 Working..."
            msg = await channel.send(content)
            self._status_messages[scope_key] = (msg, [])
        except Exception as e:
            logger.debug(f"Failed to create status message: {e}")

    async def update_status_message(self, channel_id: int, tool_line: str, status_key: str = "") -> None:
        """Update the status message with a new tool execution line.
        
        Auto-creates a status message if one doesn't exist yet.
        
        Args:
            channel_id: The Discord channel ID
            tool_line: A formatted tool status line.
        """
        if not self._client or not self._ready.is_set():
            return
        
        async with self._status_lock:
            scope_key = self._status_scope(channel_id, status_key)
            # Auto-create status message if it doesn't exist
            if scope_key not in self._status_messages:
                await self._create_status_message_locked(channel_id, status_key)
                # Check again in case start failed
                if scope_key not in self._status_messages:
                    return
            
            msg, tool_lines = self._status_messages[scope_key]
            tool_lines.append(tool_line)
            
            # Keep only the last 15 tool lines to avoid message getting too long
            if len(tool_lines) > 15:
                tool_lines = tool_lines[-15:]
            
            try:
                header = "💎 Working...\n"
                content = header + "\n".join(tool_lines)
                
                # Discord message limit is 2000 chars
                if len(content) > 1950:
                    content = content[:1950] + "\n..."
                
                await msg.edit(content=content)
                self._status_messages[scope_key] = (msg, tool_lines)
            except discord.NotFound:
                # Message was deleted, clean up
                self._status_messages.pop(scope_key, None)
            except Exception as e:
                logger.debug(f"Failed to update status message: {e}")

    async def clear_status_message(self, channel_id: int, status_key: str = "") -> None:
        """Remove the status message for a channel."""
        async with self._status_lock:
            scope_key = self._status_scope(channel_id, status_key)
            if scope_key not in self._status_messages:
                return
            
            msg, _ = self._status_messages.pop(scope_key)
            try:
                await msg.delete()
            except Exception:
                pass

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
