"""LangChain tools. Requires ``pip install apexrunner-tools[langchain]``.

    from apexrunner_tools.langchain import get_apex_tools
    tools = get_apex_tools(private_key=os.environ["AGENT_WALLET_KEY"])
    agent = create_agent(model, tools=tools)

Each tool returns ``content_and_artifact``: the model sees compact JSON, and the
ToolMessage artifact carries the full data plus the payment receipt.
Failures raise ToolException, which ``handle_tool_error=True`` turns into a
message the model can read and act on.
"""

from __future__ import annotations

from typing import Any, Literal

try:
    from langchain_core.tools import BaseTool, ToolException
except ImportError as exc:  # pragma: no cover
    raise ImportError("Install the LangChain extra: pip install 'apexrunner-tools[langchain]'") from exc

from pydantic import ConfigDict, Field

from .client import ApexClient
from .errors import ApexError
from .signals import SIGNALS


class ApexTool(BaseTool):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: ApexClient = Field(exclude=True)
    signal: str
    response_format: Literal["content", "content_and_artifact"] = "content_and_artifact"
    handle_tool_error: bool = True

    def _run(self, *args: Any, run_manager: Any = None, **kwargs: Any) -> tuple[str, dict]:
        try:
            result = self.client.call(self.signal, **kwargs)
        except ApexError as exc:
            raise ToolException(str(exc)) from exc
        return result.to_content(), result.to_artifact()

    async def _arun(self, *args: Any, run_manager: Any = None, **kwargs: Any) -> tuple[str, dict]:
        try:
            result = await self.client.acall(self.signal, **kwargs)
        except ApexError as exc:
            raise ToolException(str(exc)) from exc
        return result.to_content(), result.to_artifact()


def get_apex_tools(client: ApexClient | None = None, **client_kwargs: Any) -> list[ApexTool]:
    """The four APEX tools sharing one client (one wallet, one budget, one cache).

    Pass an existing ``client``, or the ApexClient arguments (``private_key=...``,
    ``budget_usdc=...``, ...) to create one.
    """
    if client is None:
        client = ApexClient(**client_kwargs)
    elif client_kwargs:
        raise ValueError("Pass either client or client arguments, not both.")
    return [
        ApexTool(
            name=spec.name,
            description=spec.description,
            args_schema=spec.input_model,
            client=client,
            signal=spec.name,
        )
        for spec in SIGNALS
    ]
