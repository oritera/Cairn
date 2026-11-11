"""Model-agnostic token cost accounting.

Workers report only *tokens* (via ``WorkerDriver.extract_usage``); pricing lives
here, centrally, so any backend — GLM via routerai, Claude, OpenAI/Codex, or some
unknown provider — is costed by one consistent rule:

    price = worker env override (PI_PRICE_*)  ->  central table (glob match)  ->  none

A model with no price is still *counted* (its tokens accumulate) but its cost is
reported as unavailable — we never silently bill an unknown model at zero. The
tracker aggregates per currency, so mixed-currency fleets (RUB GLM + USD Claude)
stay honest instead of being summed into a meaningless number.

Override the table at runtime with ``CAIRN_PRICING_JSON`` — either inline JSON or a
path to a JSON file — a list of ``{"pattern","input","output","cache_read"?,
"cache_write"?,"currency"?}`` rows, prepended to the defaults (first match wins).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Price:
    """Price per 1,000,000 tokens, in ``currency``."""

    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0
    currency: str = "USD"


# (glob pattern on model id, Price). Case-insensitive; FIRST match wins, so put
# specific patterns before broad ones. These are best-effort public list prices —
# EDIT to your real rates, or override per-worker with PI_PRICE_* env vars.
_DEFAULT_PRICES: list[tuple[str, Price]] = [
    # --- GLM (Z.ai family). routerai resells in RUB; these are USD list-price
    #     ESTIMATES so the panel is not empty — set PI_PRICE_* for the real rate. ---
    ("*glm-5*", Price(1.00, 3.20)),
    ("*flashx*", Price(0.37, 1.25)),
    ("*glm*flash*", Price(0.10, 0.40)),
    ("*glm*air*", Price(0.20, 1.10)),
    ("*glm*", Price(0.60, 2.20)),
    # --- Anthropic Claude (USD list prices; verify) ---
    ("*opus*", Price(15.0, 75.0, 1.50, 18.75)),
    ("*sonnet*", Price(3.0, 15.0, 0.30, 3.75)),
    ("*haiku*", Price(0.80, 4.0, 0.08, 1.0)),
    ("claude*", Price(3.0, 15.0)),
    # --- OpenAI / Codex (USD list prices; verify) ---
    ("*gpt-4o-mini*", Price(0.15, 0.60)),
    ("*gpt-4o*", Price(2.50, 10.0)),
    ("*gpt-4.1-mini*", Price(0.40, 1.60)),
    ("*gpt-4.1*", Price(2.0, 8.0)),
    ("*o3-mini*", Price(1.10, 4.40)),
    ("*o3*", Price(2.0, 8.0)),
    ("*codex*", Price(1.50, 6.0)),
]


def _env_price(worker_env: dict | None) -> Price | None:
    """A per-worker price declared in env (PI_PRICE_INPUT/OUTPUT/...), or None."""
    if not worker_env:
        return None
    try:
        pin = worker_env.get("PI_PRICE_INPUT")
        pout = worker_env.get("PI_PRICE_OUTPUT")
        if pin is None or pout is None:
            return None
        return Price(
            input=float(pin),
            output=float(pout),
            cache_read=float(worker_env.get("PI_PRICE_CACHE_READ") or 0.0),
            cache_write=float(worker_env.get("PI_PRICE_CACHE_WRITE") or 0.0),
            currency=(worker_env.get("PI_PRICE_CURRENCY") or "USD").strip() or "USD",
        )
    except (TypeError, ValueError):
        return None


def _load_overrides() -> list[tuple[str, Price]]:
    raw = os.environ.get("CAIRN_PRICING_JSON")
    if not raw:
        return []
    text = raw
    try:
        if not raw.lstrip().startswith("["):
            with open(raw, encoding="utf-8") as fh:
                text = fh.read()
        rows = json.loads(text)
        out: list[tuple[str, Price]] = []
        for r in rows:
            out.append(
                (
                    str(r["pattern"]),
                    Price(
                        input=float(r["input"]),
                        output=float(r["output"]),
                        cache_read=float(r.get("cache_read") or 0.0),
                        cache_write=float(r.get("cache_write") or 0.0),
                        currency=str(r.get("currency") or "USD"),
                    ),
                )
            )
        return out
    except Exception as exc:  # malformed override must not break the dispatcher
        LOG.warning("ignoring CAIRN_PRICING_JSON: %s", exc)
        return []


_OVERRIDES = _load_overrides()


def table_price(model_id: str | None) -> Price | None:
    if not model_id:
        return None
    needle = model_id.lower()
    for pattern, price in (*_OVERRIDES, *_DEFAULT_PRICES):
        if fnmatch.fnmatch(needle, pattern.lower()):
            return price
    return None


def resolve_price(worker_env: dict | None, model_id: str | None) -> Price | None:
    """Env override first, then the central table, else None (unpriced)."""
    return _env_price(worker_env) or table_price(model_id)


def _cost_of(price: Price, usage: dict) -> float:
    def tok(key: str) -> int:
        try:
            return int(usage.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return (
        tok("input") / 1_000_000 * price.input
        + tok("output") / 1_000_000 * price.output
        + tok("cache_read") / 1_000_000 * price.cache_read
        + tok("cache_write") / 1_000_000 * price.cache_write
    )


@dataclass
class _ModelAcc:
    tasks: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float = 0.0
    currency: str | None = None
    priced: bool = False


class CostTracker:
    """Thread-safe per-model token/cost accumulator for the dispatcher session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._models: dict[str, _ModelAcc] = {}
        self._since = datetime.now(timezone.utc).isoformat()

    def record(self, worker_env: dict | None, model_id: str | None, usage: dict | None) -> None:
        if not usage:
            return
        key = model_id or "unknown"
        price = resolve_price(worker_env, model_id)
        with self._lock:
            acc = self._models.get(key)
            if acc is None:
                acc = self._models[key] = _ModelAcc()
            acc.tasks += 1
            for f in ("input", "output", "cache_read", "cache_write"):
                try:
                    setattr(acc, f, getattr(acc, f) + int(usage.get(f) or 0))
                except (TypeError, ValueError):
                    pass
            if price is not None:
                acc.priced = True
                acc.currency = price.currency
                acc.cost += _cost_of(price, usage)
            elif usage.get("provider_cost"):
                # worker computed its own cost (models.json cost config) — use it as
                # a fallback when the central table has no row for this model.
                try:
                    acc.priced = True
                    acc.currency = acc.currency or "USD"
                    acc.cost += float(usage["provider_cost"])
                except (TypeError, ValueError):
                    pass

    def snapshot(self) -> dict:
        with self._lock:
            if not self._models:
                return {"available": False}
            totals: dict[str, float] = {}
            tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
            by_model = []
            unpriced = []
            tasks = 0
            for model, acc in sorted(self._models.items()):
                tasks += acc.tasks
                for f in tokens:
                    tokens[f] += getattr(acc, f)
                if acc.priced and acc.currency:
                    totals[acc.currency] = round(totals.get(acc.currency, 0.0) + acc.cost, 6)
                else:
                    unpriced.append(model)
                by_model.append(
                    {
                        "model": model,
                        "tasks": acc.tasks,
                        "input": acc.input,
                        "output": acc.output,
                        "cost": round(acc.cost, 6) if acc.priced else None,
                        "currency": acc.currency if acc.priced else None,
                        "priced": acc.priced,
                    }
                )
            by_model.sort(key=lambda m: (m["cost"] or 0.0, m["input"] + m["output"]), reverse=True)
            return {
                "available": True,
                "since": self._since,
                "tasks": tasks,
                "totals": totals,
                "tokens": tokens,
                "by_model": by_model,
                "unpriced_models": unpriced,
            }


# Process-wide singleton: tasks record into it, the scheduler reads it for telemetry.
COST_TRACKER = CostTracker()
