"""Telegram channel implementation using python-telegram-bot."""

import asyncio
import re
import time

from loguru import logger
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, MessageHandler, filters, ContextTypes

from kyber.agent.display import format_duration
from kyber.bus.events import OutboundMessage
from kyber.bus.queue import MessageBus
from kyber.channels.base import BaseChannel
from kyber.config.schema import TelegramConfig


def _markdown_to_telegram_html(text: str) -> str:
    """
    Convert markdown to Telegram-safe HTML.
    """
    if not text:
        return ""
    
    # 1. Extract and protect code blocks (preserve content from other processing)
    code_blocks: list[str] = []
    def save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"
    
    text = re.sub(r'```[\w]*\n?([\s\S]*?)```', save_code_block, text)
    
    # 2. Extract and protect inline code
    inline_codes: list[str] = []
    def save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"
    
    text = re.sub(r'`([^`]+)`', save_inline_code, text)
    
    # 3. Headers # Title -> just the title text
    text = re.sub(r'^#{1,6}\s+(.+)$', r'\1', text, flags=re.MULTILINE)
    
    # 4. Blockquotes > text -> just the text (before HTML escaping)
    text = re.sub(r'^>\s*(.*)$', r'\1', text, flags=re.MULTILINE)
    
    # 5. Escape HTML special characters
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    
    # 6. Links [text](url) - must be before bold/italic to handle nested cases
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
    
    # 7. Bold **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
    
    # 8. Italic _text_ (avoid matching inside words like some_var_name)
    text = re.sub(r'(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])', r'<i>\1</i>', text)
    
    # 9. Strikethrough ~~text~~
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    
    # 10. Bullet lists - item -> • item
    text = re.sub(r'^[-*]\s+', '• ', text, flags=re.MULTILINE)
    
    # 11. Restore inline code with HTML tags
    for i, code in enumerate(inline_codes):
        # Escape HTML in code content
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00IC{i}\x00", f"<code>{escaped}</code>")
    
    # 12. Restore code blocks with HTML tags
    for i, code in enumerate(code_blocks):
        # Escape HTML in code content
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CB{i}\x00", f"<pre><code>{escaped}</code></pre>")
    
    return text


class TelegramChannel(BaseChannel):
    """
    Telegram channel using long polling.
    
    Simple and reliable - no webhook/public IP needed.
    """
    
    name = "telegram"
    
    def __init__(self, config: TelegramConfig, bus: MessageBus, groq_api_key: str = ""):
        super().__init__(config, bus)
        self.config: TelegramConfig = config
        self.groq_api_key = groq_api_key
        self._app: Application | None = None
        self._chat_ids: dict[str, int] = {}  # Map sender_id to chat_id for replies
        self._typing_tasks: dict[int, asyncio.Task] = {}
        self._typing_counts: dict[int, int] = {}
        # (chat_id, status_key) -> (message_id, start_time, tool_lines)
        self._status_messages: dict[tuple[int, str], tuple[int, float, list[str]]] = {}
        self._status_lock = asyncio.Lock()
    
    async def start(self) -> None:
        """Start the Telegram bot with long polling."""
        if not self.config.token:
            logger.error("Telegram bot token not configured")
            return
        
        self._running = True
        
        # Build the application
        self._app = (
            Application.builder()
            .token(self.config.token)
            .build()
        )
        
        # Add message handler for text, photos, voice, documents
        self._app.add_handler(
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.VOICE | filters.AUDIO | filters.Document.ALL) 
                & ~filters.COMMAND, 
                self._on_message
            )
        )
        
        # Add /start command handler
        from telegram.ext import CommandHandler
        self._app.add_handler(CommandHandler("start", self._on_start))
        
        logger.info("Starting Telegram bot (polling mode)...")
        
        # Initialize and start polling
        await self._app.initialize()
        await self._app.start()
        
        # Get bot info
        bot_info = await self._app.bot.get_me()
        logger.info(f"Telegram bot @{bot_info.username} connected")
        
        # Start polling (this runs until stopped)
        await self._app.updater.start_polling(
            allowed_updates=["message"],
            drop_pending_updates=True  # Ignore old messages on startup
        )
        
        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)
    
    async def stop(self) -> None:
        """Stop the Telegram bot."""
        self._running = False
        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()
        self._typing_counts.clear()
        
        if self._app:
            logger.info("Stopping Telegram bot...")
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            self._app = None
    
    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Telegram."""
        from kyber.channels.errors import PermanentDeliveryError, TemporaryDeliveryError

        if not self._app:
            raise TemporaryDeliveryError("Telegram bot not running")

        should_stop_typing = False
        try:
            chat_id = int(msg.chat_id)
        except ValueError:
            raise PermanentDeliveryError(f"Invalid Telegram chat_id: {msg.chat_id}")

        is_background = bool(getattr(msg, "is_background", False))
        has_active_typing = self._typing_counts.get(chat_id, 0) > 0
        if not is_background and has_active_typing:
            should_stop_typing = True
        if not is_background and not has_active_typing:
            self._start_typing(chat_id)
            should_stop_typing = True

        try:
            # Convert markdown to Telegram HTML
            html_content = _markdown_to_telegram_html(msg.content)
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=html_content,
                parse_mode="HTML"
            )
        except Exception as e:
            # Fallback to plain text if HTML parsing fails
            logger.warning(f"HTML parse failed, falling back to plain text: {e}")
            try:
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=msg.content
                )
            except Exception as e2:
                # Classify the error so the dispatcher can retry appropriately.
                err_str = str(e2).lower()
                if "chat not found" in err_str or "bot was blocked" in err_str or "forbidden" in err_str:
                    raise PermanentDeliveryError(f"Telegram send failed: {e2}") from e2
                raise TemporaryDeliveryError(f"Telegram send failed: {e2}") from e2
        finally:
            if should_stop_typing:
                self._stop_typing(chat_id)
    
    async def _on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        if not update.message or not update.effective_user:
            return
        
        user = update.effective_user
        await update.message.reply_text(
            f"👋 Hi {user.first_name}! I'm kyber.\n\n"
            "Send me a message and I'll respond!"
        )
    
    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming messages (text, photos, voice, documents)."""
        if not update.message or not update.effective_user:
            return
        
        message = update.message
        user = update.effective_user
        chat_id = message.chat_id
        
        # Use stable numeric ID, but keep username for allowlist compatibility
        sender_id = str(user.id)
        if user.username:
            sender_id = f"{sender_id}|{user.username}"

        if not self.is_allowed(sender_id):
            return
        
        # Store chat_id for replies
        self._chat_ids[sender_id] = chat_id
        self._start_typing(chat_id)
        
        # Build content from text and/or media
        content_parts = []
        media_paths = []
        
        # Text content
        if message.text:
            content_parts.append(message.text)
        if message.caption:
            content_parts.append(message.caption)
        
        # Handle media files
        media_file = None
        media_type = None
        
        if message.photo:
            media_file = message.photo[-1]  # Largest photo
            media_type = "image"
        elif message.voice:
            media_file = message.voice
            media_type = "voice"
        elif message.audio:
            media_file = message.audio
            media_type = "audio"
        elif message.document:
            media_file = message.document
            media_type = "file"
        
        # Download media if present
        if media_file and self._app:
            try:
                file = await self._app.bot.get_file(media_file.file_id)
                ext = self._get_extension(media_type, getattr(media_file, 'mime_type', None))
                
                # Save to workspace/media/
                from pathlib import Path
                media_dir = Path.home() / ".kyber" / "media"
                media_dir.mkdir(parents=True, exist_ok=True)
                
                file_path = media_dir / f"{media_file.file_id[:16]}{ext}"
                await file.download_to_drive(str(file_path))
                
                media_paths.append(str(file_path))
                
                # Handle voice transcription
                if media_type == "voice" or media_type == "audio":
                    from kyber.providers.transcription import GroqTranscriptionProvider
                    transcriber = GroqTranscriptionProvider(api_key=self.groq_api_key)
                    transcription = await transcriber.transcribe(file_path)
                    if transcription:
                        logger.info(f"Transcribed {media_type}: {transcription[:50]}...")
                        content_parts.append(f"[transcription: {transcription}]")
                    else:
                        content_parts.append(f"[{media_type}: {file_path}]")
                else:
                    content_parts.append(f"[{media_type}: {file_path}]")
                    
                logger.debug(f"Downloaded {media_type} to {file_path}")
            except Exception as e:
                logger.error(f"Failed to download media: {e}")
                content_parts.append(f"[{media_type}: download failed]")
        
        content = "\n".join(content_parts) if content_parts else "[empty message]"
        
        logger.debug(f"Telegram message from {sender_id}: {content[:50]}...")
        
        # Forward to the message bus
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(chat_id),
            content=content,
            media=media_paths,
            metadata={
                "message_id": message.message_id,
                "user_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "is_group": message.chat.type != "private"
            }
        )

    def _start_typing(self, chat_id: int) -> None:
        """Start a periodic Telegram typing indicator for a chat (ref-counted)."""
        if not self._app:
            return

        self._typing_counts[chat_id] = self._typing_counts.get(chat_id, 0) + 1
        if chat_id in self._typing_tasks:
            return

        async def _typing_loop() -> None:
            try:
                while self._running and self._typing_counts.get(chat_id, 0) > 0:
                    if not self._app:
                        break
                    await self._app.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                    await asyncio.sleep(4)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"Telegram typing indicator error: {e}")
            finally:
                task = self._typing_tasks.get(chat_id)
                if task is asyncio.current_task():
                    self._typing_tasks.pop(chat_id, None)

        self._typing_tasks[chat_id] = asyncio.create_task(_typing_loop())

    def _stop_typing(self, chat_id: int) -> None:
        """Stop typing indicator for a chat (ref-counted)."""
        if chat_id not in self._typing_counts:
            return

        self._typing_counts[chat_id] = max(0, self._typing_counts[chat_id] - 1)
        if self._typing_counts[chat_id] > 0:
            return

        self._typing_counts.pop(chat_id, None)
        task = self._typing_tasks.pop(chat_id, None)
        if task:
            task.cancel()

    def _status_scope(self, chat_id: int, status_key: str = "") -> tuple[int, str]:
        """Build a stable key for a specific in-flight request in a chat."""
        scope = (status_key or "").strip() or "default"
        return (chat_id, scope)

    async def start_status_message(self, chat_id: int, status_key: str = "") -> None:
        """Create a per-task status message for Telegram."""
        if not self._app:
            return
        async with self._status_lock:
            await self._create_status_message_locked(chat_id, status_key)

    async def _create_status_message_locked(self, chat_id: int, status_key: str = "") -> None:
        """Create or replace a status message while holding the lock."""
        if not self._app:
            return
        scope_key = self._status_scope(chat_id, status_key)

        if scope_key in self._status_messages:
            old_msg_id, _, _ = self._status_messages.pop(scope_key)
            try:
                await self._app.bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
            except Exception:
                pass

        try:
            msg = await self._app.bot.send_message(chat_id=chat_id, text="working...")
            self._status_messages[scope_key] = (msg.message_id, time.time(), [])
        except Exception as e:
            logger.debug(f"Failed to create Telegram status message: {e}")

    async def update_status_message(self, chat_id: int, tool_line: str, status_key: str = "") -> None:
        """Append a line to a per-task Telegram status message."""
        if not self._app:
            return

        async with self._status_lock:
            scope_key = self._status_scope(chat_id, status_key)
            if scope_key not in self._status_messages:
                await self._create_status_message_locked(chat_id, status_key)
                if scope_key not in self._status_messages:
                    return

            msg_id, start_time, tool_lines = self._status_messages[scope_key]
            tool_lines.append(tool_line)
            if len(tool_lines) > 15:
                tool_lines = tool_lines[-15:]

            elapsed = time.time() - start_time
            content = f"working... ({format_duration(elapsed)})\n" + "\n".join(tool_lines)
            if len(content) > 3900:
                content = content[:3900] + "\n..."

            try:
                await self._app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=content,
                )
                self._status_messages[scope_key] = (msg_id, start_time, tool_lines)
            except Exception as e:
                err = str(e).lower()
                if "message is not modified" in err:
                    return
                if "message to edit not found" in err:
                    self._status_messages.pop(scope_key, None)
                    return
                logger.debug(f"Failed to update Telegram status message: {e}")

    async def clear_status_message(self, chat_id: int, status_key: str = "") -> None:
        """Delete a per-task Telegram status message."""
        if not self._app:
            return
        async with self._status_lock:
            scope_key = self._status_scope(chat_id, status_key)
            if scope_key not in self._status_messages:
                return

            msg_id, _, _ = self._status_messages.pop(scope_key)
            try:
                await self._app.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
    
    def _get_extension(self, media_type: str, mime_type: str | None) -> str:
        """Get file extension based on media type."""
        if mime_type:
            ext_map = {
                "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
            }
            if mime_type in ext_map:
                return ext_map[mime_type]
        
        type_map = {"image": ".jpg", "voice": ".ogg", "audio": ".mp3", "file": ""}
        return type_map.get(media_type, "")
