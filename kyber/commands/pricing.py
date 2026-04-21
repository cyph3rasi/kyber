"""Simple per-model rate card for ``/usage`` and ``/cost`` commands.

Anthropic and OpenAI publish their prices per million tokens. Values
below are USD per 1M tokens. Keep this short and obvious — we only need
enough coverage to estimate what a session cost. When users run a model
we don't know, we skip cost in the output rather than guessing.

Subscription models (ChatGPT Plus/Pro/Business, Claude Pro/Max) show up
as ``(included in subscription)`` instead of a dollar number — the user
already paid the flat rate, so per-token cost is $0 on the margin.

Last refreshed against public pricing pages on 2026-04-21; tweak when
providers update. If a model maps to a common family, we match by
substring first (e.g. ``claude-sonnet-4-6`` → ``claude-sonnet-4``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Rate:
    input_per_mtok: float   # USD per 1,000,000 input tokens
    output_per_mtok: float  # USD per 1,000,000 output tokens
    note: str = ""


# Exact-match entries are tried first; substring fallbacks below.
EXACT_RATES: dict[str, Rate] = {
    # Anthropic — see anthropic.com/pricing
    "claude-opus-4-7":     Rate(15.0, 75.0, "Claude Opus 4.7"),
    "claude-opus-4-6":     Rate(15.0, 75.0, "Claude Opus 4.6"),
    "claude-opus-4-5":     Rate(15.0, 75.0, "Claude Opus 4.5"),
    "claude-opus-4-1":     Rate(15.0, 75.0, "Claude Opus 4.1"),
    "claude-opus-4":       Rate(15.0, 75.0, "Claude Opus 4"),
    "claude-sonnet-4-6":   Rate(3.0,  15.0, "Claude Sonnet 4.6"),
    "claude-sonnet-4-5":   Rate(3.0,  15.0, "Claude Sonnet 4.5"),
    "claude-sonnet-4":     Rate(3.0,  15.0, "Claude Sonnet 4"),
    "claude-haiku-4-5":    Rate(1.0,  5.0,  "Claude Haiku 4.5"),
    # OpenAI
    "gpt-5.4":             Rate(10.0, 40.0, "GPT-5.4"),
    "gpt-5.4-mini":        Rate(2.5,  10.0, "GPT-5.4 mini"),
    "gpt-5.3-codex":       Rate(5.0,  20.0, "GPT-5.3 Codex"),
    "gpt-5.2-codex":       Rate(5.0,  20.0, "GPT-5.2 Codex"),
    "gpt-5.2":             Rate(5.0,  20.0, "GPT-5.2"),
    "gpt-4.1":             Rate(2.0,  8.0,  "GPT-4.1"),
    "gpt-4o":              Rate(2.5,  10.0, "GPT-4o"),
    "gpt-4o-mini":         Rate(0.15, 0.6,  "GPT-4o mini"),
}


_SUBSCRIPTION_PROVIDER_NAMES = {"CodexProvider"}


def rate_for(model: str) -> Rate | None:
    """Look up a rate by model id, matching exact names first then prefixes."""
    if not model:
        return None
    m = model.strip().lower()
    exact = EXACT_RATES.get(m)
    if exact is not None:
        return exact
    # Try stripping the date suffix — Anthropic ships dated aliases.
    if "-" in m:
        prefix_guess = "-".join(part for part in m.split("-") if not part.isdigit())
        # Not reliable — fall through to substring logic below.
    # Substring fallback: find the longest prefix in EXACT_RATES that starts the model name.
    best: tuple[str, Rate] | None = None
    for key, rate in EXACT_RATES.items():
        if m.startswith(key) or key.startswith(m):
            if best is None or len(key) > len(best[0]):
                best = (key, rate)
    return best[1] if best else None


def is_subscription_provider(provider_class_name: str) -> bool:
    """Whether a provider's per-token cost is subsumed by a flat subscription."""
    return provider_class_name in _SUBSCRIPTION_PROVIDER_NAMES


def estimate_cost(
    *,
    input_tokens: int,
    output_tokens: int,
    model: str,
    provider: str = "",
) -> dict[str, float | str | None]:
    """Return a dict describing the estimated cost of a single turn.

    Keys:
      * ``usd`` — cost in dollars, or 0 for subscription providers.
      * ``subscription`` — True when the user is on a flat-fee plan.
      * ``model_note`` — human-readable model name we matched to, or None.
    """
    if is_subscription_provider(provider):
        return {
            "usd": 0.0,
            "subscription": True,
            "model_note": model or provider,
        }
    rate = rate_for(model)
    if rate is None:
        return {"usd": None, "subscription": False, "model_note": None}
    usd = (
        (input_tokens / 1_000_000.0) * rate.input_per_mtok
        + (output_tokens / 1_000_000.0) * rate.output_per_mtok
    )
    return {"usd": usd, "subscription": False, "model_note": rate.note or model}


def format_usd(usd: float | None) -> str:
    """Render a dollar amount compactly: $0.0012, $0.42, $12.34."""
    if usd is None:
        return "—"
    if usd == 0:
        return "$0.00"
    if usd < 0.01:
        return f"${usd:.4f}"
    if usd < 1:
        return f"${usd:.3f}"
    return f"${usd:,.2f}"
