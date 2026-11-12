"""Pinned values. The payment policy refuses any payment request that differs."""

__version__ = "0.1.0"

APEX_BASE_URL = "https://apexrunner.ai/signals"

# Base mainnet, CAIP-2. APEX is paid here only.
BASE_MAINNET = "eip155:8453"

# Native USDC on Base (6 decimals).
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6

# APEX Runner's receiving address, as served in every 402 challenge.
APEX_PAY_TO = "0x8D0663BBb967ebF1c047405A888DDf865f3E8854"

# Per-call ceiling. List price today is $0.01 (charged $0.007 until 2027-01-01).
DEFAULT_MAX_PAYMENT_USDC = 0.02
# Hard ceiling for any configured per-call cap: a typo must not authorise $10 calls.
MAX_ALLOWED_PAYMENT_USDC = 1.00

# Total the client may sign for over its lifetime.
DEFAULT_BUDGET_USDC = 1.00

REQUEST_TIMEOUT_S = 20.0

# Never serve a cached result older than this, whatever the response claims.
MAX_CACHE_SECONDS = 300

# Keeps one signal payload from flooding the model's context window.
MAX_CONTENT_CHARS = 16_000

# Distinct UA so APEX can attribute traffic from this package.
USER_AGENT = f"apexrunner-tools/{__version__} (python; +https://apexrunner.ai)"
