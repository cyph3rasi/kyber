"""Channel manager for coordinating chat channels."""

import asyncio
import random
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from kyber.bus.events import OutboundMessage
from kyber.bus.queue import MessageBus
from kyber.channels.base import BaseChannel
from kyber.channels.errors import PermanentDeliveryError, TemporaryDeliveryError
from kyber.config.schema import Config


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.
    
    Responsibilities:
    - Initialize enabled channels (Telegram, WhatsApp, etc.)
    - Start/stop channels
    - Route outbound messages
    """
    
    def __init__(self, config: Config, bus: MessageBus):
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self._dispatch_task: asyncio.Task | None = None
        
        self._init_channels()
    
    def _init_channels(self) -> None:
        """Initialize channels based on config."""
        
        # Telegram channel
        if self.config.channels.telegram.enabled:
            try:
                from kyber.channels.telegram import TelegramChannel
                self.channels["telegram"] = TelegramChannel(
                    self.config.channels.telegram,
                    self.bus,
                    groq_api_key=self.config.providers.groq.api_key,
                )
                logger.info("Telegram channel enabled")
            except ImportError as e:
                logger.warning(f"Telegram channel not available: {e}")
        
        # WhatsApp channel
        if self.config.channels.whatsapp.enabled:
            try:
                from kyber.channels.whatsapp import WhatsAppChannel
                self.channels["whatsapp"] = WhatsAppChannel(
                    self.config.channels.whatsapp, self.bus
                )
                logger.info("WhatsApp channel enabled")
            except ImportError as e:
                logger.warning(f"WhatsApp channel not available: {e}")

        # Discord channel
        if self.config.channels.discord.enabled:
            try:
                from kyber.channels.discord import DiscordChannel
                self.channels["discord"] = DiscordChannel(
                    self.config.channels.discord, self.bus
                )
                logger.info("Discord channel enabled")
            except ImportError as e:
                logger.warning(f"Discord channel not available: {e}")
    
    async def start_all(self) -> None:
        """Start WhatsApp channel and the outbound dispatcher."""
        if not self.channels:
            logger.warning("No channels enabled")
            return
        
        # Start outbound dispatcher
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())
        
        # Start WhatsApp channel
        tasks = []
        for name, channel in self.channels.items():
            logger.info(f"Starting {name} channel...")
            tasks.append(asyncio.create_task(channel.start()))
        
        # Wait for all to complete (they should run forever)
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def stop_all(self) -> None:
        """Stop all channels and the dispatcher."""
        logger.info("Stopping all channels...")
        
        # Stop dispatcher
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
        
        # Stop all channels
        for name, channel in self.channels.items():
            try:
                await channel.stop()
                logger.info(f"Stopped {name} channel")
            except Exception as e:
                logger.error(f"Error stopping {name}: {e}")
    
    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to the appropriate channel."""
        logger.info("Outbound dispatcher started")

        # Pending retries: (retry_at, attempts, message, last_error)
        pending: list[tuple[datetime, int, OutboundMessage, str]] = []

        def _schedule_retry(msg: OutboundMessage, attempts: int, err: Exception) -> None:
            # Exponential backoff with jitter; cap at 5 minutes.
            delay_s = min(300.0, float(2 ** min(8, max(0, attempts - 1))))
            delay_s = delay_s * (0.8 + random.random() * 0.4)  # +/-20%
            retry_at = datetime.now() + timedelta(seconds=delay_s)
            pending.append((retry_at, attempts, msg, str(err)))

        async def _try_send(msg: OutboundMessage, attempts: int = 1) -> None:
            channel = self.channels.get(msg.channel)
            if not channel:
                raise PermanentDeliveryError(f"Unknown channel: {msg.channel}")
            try:
                await asyncio.wait_for(channel.send(msg), timeout=30.0)
            except asyncio.TimeoutError:
                raise TemporaryDeliveryError(
                    f"Send to {msg.channel}:{msg.chat_id} timed out after 30s"
                )
            logger.info(f"Delivered to {msg.channel}:{msg.chat_id} (attempt {attempts})")

        while True:
            try:
                now = datetime.now()
                # Prefer due retries first so completions aren't lost.
                msg: OutboundMessage | None = None
                attempts = 1
                last_err = ""

                if pending:
                    pending.sort(key=lambda x: x[0])
                    retry_at, attempts, msg, last_err = pending[0]
                    if retry_at <= now:
                        pending.pop(0)
                    else:
                        msg = None

                timeout = 1.0
                if pending:
                    pending.sort(key=lambda x: x[0])
                    timeout = min(timeout, max(0.0, (pending[0][0] - now).total_seconds()))

                if msg is None:
                    msg = await asyncio.wait_for(self.bus.consume_outbound(), timeout=timeout)
                    attempts = 1
                    last_err = ""

                try:
                    await _try_send(msg, attempts=attempts)
                except PermanentDeliveryError as e:
                    logger.error(f"Permanent outbound delivery failure to {msg.channel}:{msg.chat_id}: {e}")
                except TemporaryDeliveryError as e:
                    logger.error(
                        f"Temporary outbound delivery failure to {msg.channel}:{msg.chat_id} "
                        f"(attempt {attempts}): {e}"
                    )
                    _schedule_retry(msg, attempts + 1, e)
                except Exception as e:
                    # Treat unknown exceptions as transient by default; channels can
                    # raise PermanentDeliveryError to prevent pointless retries.
                    logger.error(
                        f"Error sending to {msg.channel}:{msg.chat_id} "
                        f"(attempt {attempts}): {e}"
                    )
                    _schedule_retry(msg, attempts + 1, e)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                # CRITICAL: Never let the dispatch loop die. If it crashes,
                # ALL subsequent outbound messages are lost forever.
                logger.error(f"Unexpected error in outbound dispatcher (recovering): {e}")
                await asyncio.sleep(0.5)
    
    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self.channels.get(name)
    
    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        return {
            name: {
                "enabled": True,
                "running": channel.is_running
            }
            for name, channel in self.channels.items()
        }
    
    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self.channels.keys())
