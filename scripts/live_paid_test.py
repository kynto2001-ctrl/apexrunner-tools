#!/usr/bin/env python3
"""Live paid test: one real call per tool against apexrunner.ai. NOT shipped in the wheel.

Spends AT MOST $0.021 USDC: the client's budget is exactly 3 x $0.007, so a
4th signature is impossible. Each tool goes through a different code path:

    risk_assessment_bundle      CrewAI tool          (sync,  requests)
    execution_window_optimizer  ApexClient.acall     (async, httpx)
    slippage_forecast           LangChain tool       (sync,  requests, content_and_artifact)

Key handling follows apex-signals/scripts/wallet_key.py: --key-file (mode 0600)
or an interactive getpass prompt. Deliberately NOT read from the environment.

    .venv/bin/python scripts/live_paid_test.py --yes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
import sys
from getpass import getpass
from pathlib import Path

from eth_account import Account

from apexrunner_tools import ApexClient
from apexrunner_tools.crewai import get_apex_tools as crew_tools
from apexrunner_tools.langchain import get_apex_tools as lc_tools

DEFAULT_KEY_FILE = Path("/root/.apex-secrets/bazaar_wallet.key")
SEED_ADDRESS = "0x55ca549Bd9106f97D30d22c591bF849c52d470Ec"
PRICE = 0.007
TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def load_key(path: Path | None) -> str:
    if path is not None and path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            sys.exit(f"Refusing {path}: mode {oct(mode)} is readable by group/other (want 0600).")
        return path.read_text().strip()
    return getpass("Buyer wallet private key (not echoed): ").strip()


def check(name: str, ok: bool, detail: str) -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")
    return ok


def verify_payment(label: str, payment: dict) -> bool:
    tx = payment.get("transaction") or ""
    return all([
        check(label, abs(payment.get("amount_usdc", 0) - PRICE) < 1e-9, f"signed ${payment.get('amount_usdc')}"),
        check(label, bool(TX_RE.match(tx)), f"settlement tx {tx or '(none)'}"),
    ])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--yes", action="store_true", help="confirm spending up to $0.021 USDC on Base")
    ap.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    ap.add_argument("--expect-address", default=SEED_ADDRESS)
    ap.add_argument("--base-url", default=None, help=argparse.SUPPRESS)  # rehearsal against a local mock only
    args = ap.parse_args()
    if not args.yes:
        sys.exit("This spends real USDC (up to $0.021). Re-run with --yes.")

    account = Account.from_key(load_key(args.key_file))
    if account.address.lower() != args.expect_address.lower():
        sys.exit(f"Key derives to {account.address}, expected {args.expect_address}. Aborting; nothing spent.")
    print(f"Buyer {account.address}; budget $0.021 (3 x $0.007), cache off\n")

    extra = {"base_url": args.base_url} if args.base_url else {}
    client = ApexClient(account=account, budget_usdc=0.021, max_payment_usdc=0.01, cache=False, **extra)
    del account
    ok = True
    receipts = []

    print("1. risk_assessment_bundle  [CrewAI tool, sync]")
    out = json.loads(crew_tools(client)[0].run())
    if out.get("success") is False:
        ok = check("risk", False, out["error"])
    else:
        d = out["data"]
        components = {"liquidation_pressure", "liquidation_magnet", "portfolio_heat", "whale_sentiment"}
        ok &= check("risk", d.get("components_delivered") == 4, f"components_delivered={d.get('components_delivered')}")
        ok &= check("risk", components <= d.keys(), f"has the 4 components: {sorted(components & d.keys())}")
        ok &= check("risk", "oi_divergence" not in d, "no oi_divergence block")
        note = d.get("bundle_note", "")
        ok &= check("risk", "retired" in note and "5th" not in note, f"bundle_note: {note[:70]}...")
        ok &= verify_payment("risk", out["payment"])
        receipts.append(("risk_assessment_bundle", out["payment"]["transaction"]))

    print("\n2. execution_window_optimizer  [ApexClient.acall, async]")
    try:
        r = asyncio.run(client.acall("execution_window_optimizer", coin="ETH"))
        verdicts = {"EXECUTE_NOW", "GOOD_WINDOW", "WAIT_15MIN", "WAIT_SESSION"}
        ok &= check("window", r.data.get("verdict") in verdicts, f"verdict={r.data.get('verdict')}")
        score = r.data.get("window_score")
        ok &= check("window", isinstance(score, (int, float)) and 0 <= score <= 100, f"window_score={score}")
        ok &= verify_payment("window", r.payment())
        receipts.append(("execution_window_optimizer", r.transaction))
    except Exception as exc:  # noqa: BLE001
        ok = check("window", False, f"{type(exc).__name__}: {exc}")

    print("\n3. slippage_forecast  [LangChain tool, content_and_artifact]")
    msg = lc_tools(client)[2].invoke(
        {"name": "slippage_forecast", "args": {"coin": "ETH", "size_usd": 5000, "side": "sell"},
         "id": "live-1", "type": "tool_call"}
    )
    if msg.status == "error":
        ok = check("slippage", False, msg.content)
    else:
        d = msg.artifact["data"]
        recs = {"EXECUTE_NOW", "EXECUTE", "SPLIT_ORDER", "USE_LIMIT"}
        ok &= check("slippage", d.get("recommendation") in recs, f"recommendation={d.get('recommendation')}")
        ok &= check("slippage", isinstance(d.get("expected_slippage_pct"), (int, float)),
                    f"expected_slippage_pct={d.get('expected_slippage_pct')}")
        ok &= check("slippage", d.get("size_usd") in (5000, 5000.0) and d.get("side") == "sell",
                    f"echoed size_usd={d.get('size_usd')} side={d.get('side')}")
        ok &= verify_payment("slippage", msg.artifact["payment"])
        receipts.append(("slippage_forecast", msg.artifact["payment"]["transaction"]))

    print(f"\nSpent ${client.budget.spent_usdc:.3f} of ${client.budget.limit_usdc:.3f}")
    for name, tx in receipts:
        print(f"  {name:28} https://basescan.org/tx/{tx}")
    print("\nCross-check server side (apex-signals):")
    print("  sqlite3 'file:/root/apex-signals/db/signals.db?mode=ro' \"SELECT id,timestamp,signal_name,price_paid,"
          "substr(tx_hash,1,12) FROM signal_usage WHERE lower(agent_wallet)=lower('" + SEED_ADDRESS + "') "
          "ORDER BY id DESC LIMIT 3;\"")
    print("  grep 'apexrunner-tools/' /var/log/nginx/*access*.log | tail -6")
    client.close()
    print("\nRESULT:", "ALL PASS" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
