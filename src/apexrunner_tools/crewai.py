"""CrewAI tools. Requires ``pip install apexrunner-tools[crewai]``.

    from apexrunner_tools.crewai import get_apex_tools
    analyst = Agent(role="...", goal="...", backstory="...",
                    tools=get_apex_tools(private_key=os.environ["AGENT_WALLET_KEY"]))

CrewAI's own tool cache is switched off for these tools: it has no expiry, so
it would keep serving market data long after it went stale. ApexClient caches
each result only for as long as the response says it is valid.

Failures are returned as JSON with ``success: false`` and the reason, so the
agent can read them instead of the crew crashing.
"""

from __future__ import annotations

import json
from typing import Any

try:
    from crewai.tools import BaseTool
except ImportError as exc:  # pragma: no cover
    raise ImportError("Install the CrewAI extra: pip install 'apexrunner-tools[crewai]'") from exc

from pydantic import ConfigDict, Field

from .client import ApexClient
from .errors import ApexError
from .signals import SIGNALS


def _never_cache(_args: Any = None, _result: Any = None) -> bool:
    return False


class ApexCrewTool(BaseTool):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: ApexClient = Field(exclude=True)
    signal: str
    cache_function: Any = _never_cache

    def _run(self, **kwargs: Any) -> str:
        try:
            return self.client.call(self.signal, **kwargs).to_content()
        except ApexError as exc:
            return _failure(self.signal, exc)

    async def _arun(self, **kwargs: Any) -> str:
        try:
            return (await self.client.acall(self.signal, **kwargs)).to_content()
        except ApexError as exc:
            return _failure(self.signal, exc)


def _failure(signal: str, exc: ApexError) -> str:
    return json.dumps({"success": False, "signal": signal, "error": str(exc)})


def get_apex_tools(client: ApexClient | None = None, **client_kwargs: Any) -> list[ApexCrewTool]:
    """The four APEX tools sharing one client (one wallet, one budget, one cache)."""
    if client is None:
        client = ApexClient(**client_kwargs)
    elif client_kwargs:
        raise ValueError("Pass either client or client arguments, not both.")
    return [
        ApexCrewTool(
            name=spec.name,
            description=spec.description,
            args_schema=spec.input_model,
            client=client,
            signal=spec.name,
        )
        for spec in SIGNALS
    ]
