"""Local risk limits: the developer's own caps, read from apex_risk.toml or APEX_* env vars.

    # apex_risk.toml
    [limits]
    max_trade_usd      = 100     # per order
    max_24h_volume_usd = 1000    # rolling 24 h, every venue that shares the state file
    portfolio_stop_pct = 5       # or "5%": stop when equity is down more than this from baseline

    [state]
    path = "~/.apexrunner/risk_ledger.sqlite"

Precedence, highest first: keyword overrides, then environment
(``APEX_MAX_TRADE_USD``, ``APEX_MAX_24H_VOLUME_USD``, ``APEX_PORTFOLIO_STOP_PCT``,
``APEX_RISK_STATE``), then the TOML file.

Bad configuration is an error at load time, never a silently skipped limit:
unknown keys, non-numeric or non-positive values and a stop of 100% or more all
raise ``ValueError``. So does a load that finds no limit at all.

These limits are enforced in the agent's own process. They are not tamper-proof:
code that can edit the config or delete the state file can remove them.
"""

from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

DEFAULT_CONFIG_NAME = "apex_risk.toml"
DEFAULT_STATE_PATH = Path("~/.apexrunner/risk_ledger.sqlite")

_LIMIT_KEYS = ("max_trade_usd", "max_24h_volume_usd", "portfolio_stop_pct")
_ENV = {
    "max_trade_usd": "APEX_MAX_TRADE_USD",
    "max_24h_volume_usd": "APEX_MAX_24H_VOLUME_USD",
    "portfolio_stop_pct": "APEX_PORTFOLIO_STOP_PCT",
    "state_path": "APEX_RISK_STATE",
}
CONFIG_ENV = "APEX_RISK_CONFIG"


@dataclass(frozen=True)
class RiskLimits:
    """Validated limits. ``None`` means that limit is not set."""

    #: Largest single order, in USD. A trade of exactly this size passes.
    max_trade_usd: float | None = None
    #: Most USD traded in any rolling 24 h, across everything sharing ``state_path``.
    #: A trade that brings the total to exactly this passes.
    max_24h_volume_usd: float | None = None
    #: Block every trade once equity is down MORE than this percent from the baseline
    #: taken when the stop was armed or reset. Stays stopped until reset.
    portfolio_stop_pct: float | None = None
    #: SQLite file holding the volume ledger and the stop latch.
    state_path: Path = DEFAULT_STATE_PATH

    def __post_init__(self) -> None:
        for key in _LIMIT_KEYS:
            object.__setattr__(self, key, _number(key, getattr(self, key)))
        if self.portfolio_stop_pct is not None and not self.portfolio_stop_pct < 100:
            raise ValueError(f"portfolio_stop_pct must be under 100, got {self.portfolio_stop_pct}")
        object.__setattr__(self, "state_path", Path(self.state_path).expanduser())

    @property
    def active(self) -> bool:
        return any(getattr(self, k) is not None for k in _LIMIT_KEYS)

    def describe(self) -> str:
        parts = []
        if self.max_trade_usd is not None:
            parts.append(f"max ${self.max_trade_usd:,.2f}/trade")
        if self.max_24h_volume_usd is not None:
            parts.append(f"max ${self.max_24h_volume_usd:,.2f}/24h")
        if self.portfolio_stop_pct is not None:
            parts.append(f"stop at -{self.portfolio_stop_pct:g}%")
        return ", ".join(parts) or "no limits"


def load_limits(
    path: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    **overrides: Any,
) -> RiskLimits:
    """Read limits from TOML, environment and keyword overrides.

    ``path``: the TOML file. Default: ``$APEX_RISK_CONFIG``, else ``./apex_risk.toml``
    if it exists. A path given explicitly (argument or env) must exist.

    Raises ``ValueError`` if the result sets no limit at all: a shield that was
    meant to enforce limits must not run silently without them.
    """
    env = os.environ if env is None else env
    unknown = set(overrides) - {*_LIMIT_KEYS, "state_path"}
    if unknown:
        raise ValueError(f"unknown limit(s) {sorted(unknown)}; expected {[*_LIMIT_KEYS, 'state_path']}")

    explicit = path if path is not None else env.get(CONFIG_ENV)
    file = Path(explicit).expanduser() if explicit else Path(DEFAULT_CONFIG_NAME)
    values: dict[str, Any] = {}
    if explicit or file.is_file():
        values.update(_read_toml(file))

    for key, var in _ENV.items():
        if env.get(var, "").strip():
            values[key] = env[var].strip()
    values.update({k: v for k, v in overrides.items() if v is not None})

    limits = RiskLimits(**values)
    if not limits.active:
        where = f"{file}" if (explicit or file.is_file()) else f"no {DEFAULT_CONFIG_NAME} in {Path.cwd()}"
        raise ValueError(
            f"no risk limits configured ({where}; no {', '.join(v for v in _ENV.values() if v != 'APEX_RISK_STATE')})"
        )
    return limits


def _read_toml(file: Path) -> dict[str, Any]:
    if not file.is_file():
        raise FileNotFoundError(f"risk config not found: {file}")
    with file.open("rb") as fh:
        try:
            doc = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{file}: invalid TOML: {exc}") from exc
    extra = set(doc) - {"limits", "state"}
    if extra:
        raise ValueError(f"{file}: unknown section(s) {sorted(extra)}; expected [limits] and [state]")
    limits, state = doc.get("limits", {}), doc.get("state", {})
    if not isinstance(limits, dict) or not isinstance(state, dict):
        raise ValueError(f"{file}: [limits] and [state] must be tables")
    bad = set(limits) - set(_LIMIT_KEYS)
    if bad:
        raise ValueError(f"{file}: unknown limit(s) {sorted(bad)}; expected {list(_LIMIT_KEYS)}")
    bad = set(state) - {"path"}
    if bad:
        raise ValueError(f"{file}: unknown [state] key(s) {sorted(bad)}; expected ['path']")
    out: dict[str, Any] = dict(limits)
    if "path" in state:
        if not isinstance(state["path"], str) or not state["path"].strip():
            raise ValueError(f"{file}: [state] path must be a non-empty string")
        p = Path(state["path"]).expanduser()
        out["state_path"] = p if p.is_absolute() else file.resolve().parent / p
    return out


def _number(key: str, value: Any) -> float | None:
    if value is None:
        return None
    raw = value
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("%"):
            if key != "portfolio_stop_pct":
                raise ValueError(f"{key}: a percentage is only valid for portfolio_stop_pct, got {raw!r}")
            text = text[:-1].strip()
        try:
            value = float(text.replace(",", "").replace("_", "").lstrip("$"))
        except ValueError:
            raise ValueError(f"{key} must be a number, got {raw!r}") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number, got {raw!r}")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{key} must be a positive finite number, got {raw!r}")
    return value


__all__ = ["RiskLimits", "load_limits", "DEFAULT_CONFIG_NAME", "DEFAULT_STATE_PATH"]
