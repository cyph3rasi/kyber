"""
kyber - A lightweight AI agent framework
"""

import warnings

# Suppress litellm cost-calculation warnings for custom/unmapped models.
# OpenHands uses litellm internally; it spams these on every LLM call when
# the model isn't in litellm's pricing database.
warnings.filterwarnings(
    "ignore",
    message="Cost calculation failed.*",
    category=UserWarning,
)

__version__ = "2026.2.13.20"
__logo__ = "💎"
