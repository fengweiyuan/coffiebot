"""Message bus module for decoupled channel-agent communication."""

from coffiebot.bus.events import InboundMessage, OutboundMessage
from coffiebot.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
