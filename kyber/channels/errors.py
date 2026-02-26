"""Delivery errors for outbound messages.

Channels should raise these so the dispatcher can decide whether to retry.
"""


class OutboundDeliveryError(RuntimeError):
    """Base class for outbound delivery errors."""


class TemporaryDeliveryError(OutboundDeliveryError):
    """A transient failure (network, reconnect, rate limit). Safe to retry."""


class PermanentDeliveryError(OutboundDeliveryError):
    """A permanent failure (bad chat id, missing permissions). Do not retry."""

