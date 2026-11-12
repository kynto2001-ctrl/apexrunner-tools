"""CrewAI crew with an execution trader that uses APEX Runner tools.

    pip install "apexrunner-tools[crewai]"
    AGENT_WALLET_KEY=0x... OPENAI_API_KEY=... python examples/crewai_crew.py

Spends real USDC on Base (about $0.007 per tool call), capped by budget_usdc.
"""

import os

from crewai import Agent, Crew, Task

from apexrunner_tools import ApexClient
from apexrunner_tools.crewai import get_apex_tools

apex = ApexClient(private_key=os.environ["AGENT_WALLET_KEY"], budget_usdc=0.10)
trader = Agent(
    role="Execution trader",
    goal="Execute crypto orders at the lowest total cost and risk",
    backstory="You never place an order without checking risk, timing and slippage first.",
    tools=get_apex_tools(apex),
)
task = Task(
    description="Decide how and when to sell $5,000 of ETH. Use the tools, then give a concrete plan.",
    expected_output="A short execution plan: now or later, order type, and whether to split the order.",
    agent=trader,
)
print(Crew(agents=[trader], tasks=[task]).kickoff())
print(f"APEX spend: ${apex.budget.spent_usdc:.3f} of ${apex.budget.limit_usdc:.2f}")
