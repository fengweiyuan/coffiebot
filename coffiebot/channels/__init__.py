"""Chat channels module with plugin architecture."""

from coffiebot.channels.base import BaseChannel
from coffiebot.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
