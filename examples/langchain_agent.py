"""LangChain agent that checks risk, timing and slippage before proposing a trade.

    pip install "apexrunner-tools[langchain]" langchain langchain-openai
    AGENT_WALLET_KEY=0x... OPENAI_API_KEY=... python examples/langchain_agent.py

Spends real USDC on Base (about $0.007 per tool call), capped by budget_usdc.
"""

import os

from langchain.agents import create_agent

from apexrunner_tools import ApexClient
from apexrunner_tools.langchain import get_apex_tools

apex = ApexClient(private_key=os.environ["AGENT_WALLET_KEY"], budget_usdc=0.10)
agent = create_agent(
    "openai:gpt-4.1-mini",
    tools=get_apex_tools(apex),
    system_prompt="Before recommending any crypto order, check market risk, the execution window and slippage.",
)
result = agent.invoke({"messages": [{"role": "user", "content": "Should I sell $5,000 of ETH right now?"}]})
print(result["messages"][-1].content)
print(f"APEX spend: ${apex.budget.spent_usdc:.3f} of ${apex.budget.limit_usdc:.2f}")
