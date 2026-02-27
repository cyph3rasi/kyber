"""
kyber - A lightweight AI agent framework
"""

import warnings
from importlib.metadata import PackageNotFoundError, version

# Suppress litellm cost-calculation warnings for custom/unmapped models.
# OpenHands uses litellm internally; it spams these on every LLM call when
# the model isn't in litellm's pricing database.
warnings.filterwarnings(
    "ignore",
    message="Cost calculation failed.*",
    category=UserWarning,
)

try:
    __version__ = version("kyber-chat")
except PackageNotFoundError:
    __version__ = "0.0.0.dev0"
__logo__ = "💎"
