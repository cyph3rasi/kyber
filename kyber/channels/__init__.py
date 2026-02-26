"""Chat channels module with plugin architecture."""

from kyber.channels.base import BaseChannel
from kyber.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
