"""Local risk limits: config loading, the 24 h ledger, the stop latch, and RiskShield's ordering.

Every block is asserted three ways: the error says "Blocked by local risk limits",
the wrapped order function never ran, and NO paid check_trade_safety call was made.
"""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
import threading
import time

import pytest

from apexrunner_tools import (
    LedgerError,
    LocalLimitBlocked,
    RiskLimits,
    RiskShield,
    TradeBlocked,
    load_limits,
)
from apexrunner_tools.ledger import WINDOW_SECONDS, StopLatch, VolumeExceeded, VolumeLedger


def safety(score, verdict):
    body = {"safety_score": score, "verdict": verdict, "summary": verdict, "reasoning": [],
            "valid_for_seconds": 120, "data_age_seconds": 0}
    return lambda path, query: (200, body, {})


@pytest.fixture
def state(tmp_path):
    return tmp_path / "risk.sqlite"


@pytest.fixture
def low_risk(mock_apex):
    mock_apex.paid_response = safety(80, "LOW_RISK")
    return mock_apex


class Equity:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def make_order(shield, trades):
    @shield.protect
    def place_order(coin, side, size_usd):
        trades.append((coin, side, size_usd))
        return "filled"
    return place_order


def assert_local_block(exc_info, limit, mock_apex, trades, before_trades=0):
    e = exc_info.value
    assert isinstance(e, LocalLimitBlocked) and isinstance(e, TradeBlocked)
    assert str(e).startswith("Blocked by local risk limits"), str(e)
    assert e.decision.stage == "local_limits" and e.decision.limit == limit
    assert e.decision.allowed is False and e.decision.checked is False
    assert len(trades) == before_trades, "wrapped order function ran"
    assert mock_apex.signed == [], "a paid check was made for a locally blocked trade"


# ── config ────────────────────────────────────────────────────────────────────

def write(tmp_path, text, name="apex_risk.toml"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_toml_loads_and_percent_string(tmp_path):
    p = write(tmp_path, '[limits]\nmax_trade_usd = 100\nmax_24h_volume_usd = 1_000\nportfolio_stop_pct = "5%"\n'
                        '[state]\npath = "sub/ledger.sqlite"\n')
    lim = load_limits(p, env={})
    assert (lim.max_trade_usd, lim.max_24h_volume_usd, lim.portfolio_stop_pct) == (100, 1000, 5)
    assert lim.state_path == tmp_path / "sub" / "ledger.sqlite"  # relative to the toml, not the cwd


def test_precedence_kwargs_over_env_over_toml(tmp_path):
    p = write(tmp_path, "[limits]\nmax_trade_usd = 100\nmax_24h_volume_usd = 1000\n")
    env = {"APEX_MAX_TRADE_USD": "250", "APEX_PORTFOLIO_STOP_PCT": "7%"}
    lim = load_limits(p, env=env)
    assert (lim.max_trade_usd, lim.max_24h_volume_usd, lim.portfolio_stop_pct) == (250, 1000, 7)
    assert load_limits(p, env=env, max_trade_usd=50).max_trade_usd == 50


def test_env_only_and_config_env_var(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no apex_risk.toml here
    assert load_limits(env={"APEX_MAX_TRADE_USD": "$1,500"}).max_trade_usd == 1500
    p = write(tmp_path, "[limits]\nmax_trade_usd = 9\n", name="other.toml")
    assert load_limits(env={"APEX_RISK_CONFIG": str(p)}).max_trade_usd == 9


def test_default_file_in_cwd(tmp_path, monkeypatch):
    write(tmp_path, "[limits]\nmax_trade_usd = 42\n")
    monkeypatch.chdir(tmp_path)
    assert load_limits(env={}).max_trade_usd == 42


@pytest.mark.parametrize("text, match", [
    ("[limits]\nmax_trade = 100\n", "unknown limit"),
    ("[limit]\nmax_trade_usd = 100\n", "unknown section"),
    ("[limits]\nmax_trade_usd = -5\n", "positive"),
    ("[limits]\nmax_trade_usd = 0\n", "positive"),
    ("[limits]\nmax_trade_usd = \"lots\"\n", "must be a number"),
    ("[limits]\nmax_trade_usd = true\n", "must be a number"),
    ("[limits]\nmax_trade_usd = \"5%\"\n", "only valid for portfolio_stop_pct"),
    ("[limits]\nportfolio_stop_pct = 100\n", "under 100"),
    ("[limits]\nmax_trade_usd = nan\n", "finite"),
    ("[state]\npath = \"x.sqlite\"\n", "no risk limits configured"),
    ("[limits\n", "invalid TOML"),
    ("[state]\nfile = \"x\"\n[limits]\nmax_trade_usd = 1\n", r"unknown \[state\]"),
])
def test_bad_config_fails_loudly(tmp_path, text, match):
    with pytest.raises(ValueError, match=match):
        load_limits(write(tmp_path, text), env={})


def test_no_config_anywhere_is_an_error_not_silence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="no risk limits configured"):
        load_limits(env={})


def test_explicit_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_limits(tmp_path / "nope.toml", env={})


def test_unknown_override_raises():
    with pytest.raises(ValueError, match="unknown limit"):
        load_limits(env={}, max_trades=5)


# ── constructor contract ──────────────────────────────────────────────────────

def test_missing_equity_fn_refused_at_construction(make_client, state):
    # A configured stop with no way to read equity would block every trade; refuse to start.
    with pytest.raises(ValueError, match="equity_fn is missing"):
        RiskShield(make_client(), limits=RiskLimits(portfolio_stop_pct=5, state_path=state))


def test_equity_fn_without_stop_and_async_equity_fn_refused(make_client, state):
    with pytest.raises(ValueError, match="no portfolio_stop_pct"):
        RiskShield(make_client(), limits=RiskLimits(max_trade_usd=5, state_path=state), equity_fn=lambda: 1.0)

    async def eq():
        return 1.0
    with pytest.raises(TypeError, match="non-async"):
        RiskShield(make_client(), limits=RiskLimits(portfolio_stop_pct=5, state_path=state), equity_fn=eq)


def test_empty_limits_refused(make_client, state):
    with pytest.raises(ValueError, match="sets no limit"):
        RiskShield(make_client(), limits=RiskLimits(state_path=state))


def test_limits_accepts_toml_path(make_client, tmp_path, low_risk):
    p = write(tmp_path, '[limits]\nmax_trade_usd = 100\n')
    shield = RiskShield(make_client(), limits=str(p))
    assert shield.limits.max_trade_usd == 100


# ── max_trade_usd ─────────────────────────────────────────────────────────────

def test_max_trade_exact_boundary_passes_one_cent_over_blocks(make_client, state, low_risk):
    trades = []
    place = make_order(RiskShield(make_client(), limits=RiskLimits(max_trade_usd=100, state_path=state)), trades)
    with pytest.raises(TradeBlocked) as ei:
        place("ETH", "long", 100.01)
    assert_local_block(ei, "max_trade_usd", low_risk, trades)
    assert "$100.01 exceeds the $100.00 max trade size" in str(ei.value)
    assert place("ETH", "long", 100) == "filled"
    assert trades == [("ETH", "long", 100)] and len(low_risk.signed) == 1


def test_local_pass_still_runs_paid_check_and_its_block(make_client, mock_apex, state):
    mock_apex.paid_response = safety(20, "DANGER")
    trades = []
    place = make_order(RiskShield(make_client(), limits=RiskLimits(max_trade_usd=100, state_path=state)), trades)
    with pytest.raises(TradeBlocked) as ei:
        place("ETH", "long", 50)
    assert not isinstance(ei.value, LocalLimitBlocked) and ei.value.decision.stage == "safety_check"
    assert len(mock_apex.signed) == 1 and trades == []


def test_invalid_size_blocks_locally(make_client, state, low_risk):
    trades = []
    place = make_order(RiskShield(make_client(), limits=RiskLimits(max_trade_usd=100, state_path=state)), trades)
    for bad in ("50", float("nan"), -1, True):
        with pytest.raises(LocalLimitBlocked) as ei:
            place("ETH", "long", bad)
        assert_local_block(ei, "invalid_trade", low_risk, trades)


# ── max_24h_volume_usd ────────────────────────────────────────────────────────

def vol_shield(make_client, state, clock, limit=1000, **kw):
    return RiskShield(make_client(), limits=RiskLimits(max_24h_volume_usd=limit, state_path=state),
                      _clock=clock, **kw)


def test_volume_exact_boundary_passes_then_blocks(make_client, state, clock, low_risk):
    trades = []
    place = make_order(vol_shield(make_client, state, clock), trades)
    place("ETH", "long", 600)
    place("BTC", "short", 400)          # total exactly 1000: passes
    paid = len(low_risk.signed)
    with pytest.raises(LocalLimitBlocked) as ei:
        place("SOL", "long", 10)
    assert ei.value.decision.limit == "max_24h_volume_usd" and len(trades) == 2
    assert len(low_risk.signed) == paid, "paid check made for a locally blocked trade"
    assert "$1,000.00 already traded" in str(ei.value)


def test_volume_boundary_is_exact_with_float_sizes(state, clock):
    ledger = VolumeLedger(state, clock=clock)
    for usd in (0.1, 0.2, 0.7):          # 0.1 + 0.2 + 0.7 != 1.0 in floats
        ledger.reserve(usd, 1.0)
    with pytest.raises(VolumeExceeded):
        ledger.reserve(0.000001, 1.0)


def test_volume_window_is_rolling_24h(make_client, state, clock, low_risk):
    trades = []
    place = make_order(vol_shield(make_client, state, clock), trades)
    place("ETH", "long", 1000)
    clock.now += WINDOW_SECONDS - 1
    with pytest.raises(LocalLimitBlocked):
        place("ETH", "long", 10)
    clock.now += 1                      # exactly 24 h later the first trade has left the window
    place("ETH", "long", 1000)
    assert len(trades) == 2


def test_paid_block_releases_reservation(make_client, mock_apex, state, clock):
    mock_apex.paid_response = safety(20, "DANGER")
    shield = vol_shield(make_client, state, clock)
    with pytest.raises(TradeBlocked):
        make_order(shield, [])("ETH", "long", 900)
    assert shield.local_status()["volume_24h_used_usd"] == 0


def test_order_exception_keeps_volume_counted(make_client, state, clock, low_risk):
    shield = vol_shield(make_client, state, clock)

    @shield.protect
    def place_order(coin, side, size_usd):
        raise TimeoutError("venue timed out; order may be live")
    with pytest.raises(TimeoutError):
        place_order("ETH", "long", 700)
    assert shield.local_status()["volume_24h_used_usd"] == 700
    states = sqlite3.connect(state).execute("SELECT state FROM volume").fetchall()
    assert states == [("unknown",)]


def test_check_is_read_only_and_record_volume_counts(make_client, state, clock, low_risk):
    shield = vol_shield(make_client, state, clock)
    assert shield.check("ETH", "long", 800).allowed
    assert shield.check("ETH", "long", 800).allowed     # check() never reserved anything
    shield.record_volume(800, coin="ETH", side="long")
    d = shield.check("ETH", "long", 300)
    assert not d.allowed and d.limit == "max_24h_volume_usd"
    with pytest.raises(LocalLimitBlocked):
        shield.guard("ETH", "long", 300)


def test_volume_shared_across_shields_on_same_state(make_client, state, clock, low_risk):
    """Two venues, two shields, one state file: one 24 h total."""
    kraken = make_order(vol_shield(make_client, state, clock), [])
    hyperliquid = make_order(vol_shield(make_client, state, clock), [])
    kraken("ETH", "long", 700)
    with pytest.raises(LocalLimitBlocked):
        hyperliquid("ETH", "long", 400)
    hyperliquid("ETH", "long", 300)


# ── concurrency: no double-spend ──────────────────────────────────────────────

def test_concurrent_threads_cannot_double_spend(state):
    """8 threads each try $300 against $1,000. Every one reads the total before any writes
    (the hook stalls each inside its transaction), yet exactly 3 may succeed."""
    ok, refused, errors = [], [], []
    barrier = threading.Barrier(8)

    def worker():
        ledger = VolumeLedger(state)
        ledger._race_hook = lambda: time.sleep(0.05)
        barrier.wait()
        try:
            ok.append(ledger.reserve(300, 1000))
        except VolumeExceeded:
            refused.append(1)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(ok) == 3 and len(refused) == 5
    assert VolumeLedger(state).used_usd() == 900


def _proc_reserve(path, barrier, results):
    ledger = VolumeLedger(path)
    ledger._race_hook = lambda: time.sleep(0.1)
    barrier.wait()
    try:
        ledger.reserve(300, 1000)
        results.put("ok")
    except VolumeExceeded:
        results.put("refused")
    except Exception as exc:  # noqa: BLE001
        results.put(f"error: {exc!r}")


def test_concurrent_processes_cannot_double_spend(state):
    """Same as above across separate processes (separate agents on one machine)."""
    ctx = mp.get_context("spawn")
    barrier, results = ctx.Barrier(6), ctx.Queue()
    procs = [ctx.Process(target=_proc_reserve, args=(str(state), barrier, results)) for _ in range(6)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    out = sorted(results.get(timeout=5) for _ in procs)
    assert out == ["ok"] * 3 + ["refused"] * 3, out
    assert VolumeLedger(state).used_usd() == 900


def test_concurrent_protected_orders_cannot_double_spend(make_client, state, low_risk):
    shield = RiskShield(make_client(), limits=RiskLimits(max_24h_volume_usd=1000, state_path=state))
    shield._ledger._race_hook = lambda: time.sleep(0.05)
    trades, blocked = [], []
    place = make_order(shield, trades)
    barrier = threading.Barrier(5)

    def worker():
        barrier.wait()
        try:
            place("ETH", "long", 400)
        except LocalLimitBlocked:
            blocked.append(1)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(trades) == 2 and len(blocked) == 3


# ── portfolio_stop_pct ────────────────────────────────────────────────────────

def stop_shield(make_client, state, equity, pct=5):
    return RiskShield(make_client(), limits=RiskLimits(portfolio_stop_pct=pct, state_path=state), equity_fn=equity)


def test_stop_arms_at_first_trade_trips_past_threshold_and_latches(make_client, state, low_risk):
    eq, trades = Equity(1000.0), []
    shield = stop_shield(make_client, state, eq)
    place = make_order(shield, trades)
    place("ETH", "long", 50)                       # arms: baseline $1,000
    assert shield.local_status()["stop"].baseline_usd == 1000
    eq.value = 950.0                               # exactly -5%: does not exceed the stop
    place("ETH", "long", 50)
    eq.value = 949.99                              # -5.001%: trips
    paid = len(low_risk.signed)
    with pytest.raises(LocalLimitBlocked) as ei:
        place("ETH", "long", 50)
    assert ei.value.decision.limit == "portfolio_stop_pct" and "reset_stop()" in str(ei.value)
    assert len(low_risk.signed) == paid and len(trades) == 2
    eq.value = 2000.0                              # recovery does not un-trip it
    with pytest.raises(LocalLimitBlocked):
        place("ETH", "long", 50)
    # ...nor does a restart
    with pytest.raises(LocalLimitBlocked):
        make_order(stop_shield(make_client, state, eq), trades)("ETH", "long", 50)
    st = shield.reset_stop()                       # re-arm at current equity
    assert st.baseline_usd == 2000 and not st.tripped
    place("ETH", "long", 50)
    assert len(trades) == 3


def test_baseline_survives_restart(make_client, state, low_risk):
    eq = Equity(1000.0)
    make_order(stop_shield(make_client, state, eq), [])("ETH", "long", 50)
    eq.value = 900.0                               # restarting must not re-baseline at the low
    with pytest.raises(LocalLimitBlocked):
        make_order(stop_shield(make_client, state, eq), [])("ETH", "long", 50)


@pytest.mark.parametrize("bad", [RuntimeError("exchange down"), None, float("nan"), -1.0, "1000", True])
def test_unreadable_equity_blocks(make_client, state, low_risk, bad):
    trades = []
    place = make_order(stop_shield(make_client, state, Equity(bad)), trades)
    with pytest.raises(LocalLimitBlocked) as ei:
        place("ETH", "long", 50)
    assert_local_block(ei, "portfolio_stop_pct", low_risk, trades)
    assert ei.value.decision.error


def test_zero_equity_cannot_arm(make_client, state, low_risk):
    with pytest.raises(LocalLimitBlocked):
        make_order(stop_shield(make_client, state, Equity(0.0)), [])("ETH", "long", 50)


# ── ledger corruption: always blocks, even with fail_open ─────────────────────

def all_limits(state):
    return RiskLimits(max_trade_usd=500, max_24h_volume_usd=1000, portfolio_stop_pct=5, state_path=state)


@pytest.mark.parametrize("fail_open", [False, True])
def test_corrupt_ledger_blocks(make_client, state, low_risk, fail_open):
    state.write_bytes(b"this is not a sqlite database" * 200)
    trades = []
    shield = RiskShield(make_client(), limits=all_limits(state), equity_fn=Equity(), fail_open=fail_open)
    with pytest.raises(LocalLimitBlocked) as ei:
        make_order(shield, trades)("ETH", "long", 50)
    assert_local_block(ei, "risk_state", low_risk, trades)
    assert "unusable" in str(ei.value)


def test_foreign_sqlite_file_blocks(make_client, state, low_risk):
    conn = sqlite3.connect(state)
    conn.execute("CREATE TABLE volume (x)")
    conn.commit()
    conn.close()
    trades = []
    with pytest.raises(LocalLimitBlocked) as ei:
        make_order(RiskShield(make_client(), limits=all_limits(state), equity_fn=Equity()), trades)("ETH", "long", 50)
    assert_local_block(ei, "risk_state", low_risk, trades)
    assert "not an apexrunner-tools ledger" in str(ei.value)


def test_schema_version_mismatch_blocks(state):
    VolumeLedger(state).used_usd()
    conn = sqlite3.connect(state)
    conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(LedgerError, match="schema 999"):
        VolumeLedger(state).used_usd()


def test_state_deleted_while_running_blocks(make_client, state, low_risk):
    trades = []
    place = make_order(RiskShield(make_client(), limits=all_limits(state), equity_fn=Equity()), trades)
    place("ETH", "long", 50)
    state.unlink()
    with pytest.raises(LocalLimitBlocked) as ei:
        place("ETH", "long", 50)
    assert ei.value.decision.limit == "risk_state" and "deleted or emptied" in str(ei.value)
    assert len(trades) == 1


def test_state_locked_past_timeout_blocks(make_client, state, low_risk):
    shield = RiskShield(make_client(), limits=RiskLimits(max_24h_volume_usd=1000, state_path=state))
    shield._ledger.timeout = 0.2
    shield._ledger.used_usd()                      # create it
    holder = sqlite3.connect(state, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")              # another process holds the write lock
    try:
        trades = []
        with pytest.raises(LocalLimitBlocked) as ei:
            make_order(shield, trades)("ETH", "long", 50)
        assert_local_block(ei, "risk_state", low_risk, trades)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_negative_control_healthy_ledger_passes(make_client, state, low_risk):
    """The corruption tests above must fail for the corruption, not for the setup."""
    trades = []
    make_order(RiskShield(make_client(), limits=all_limits(state), equity_fn=Equity()), trades)("ETH", "long", 50)
    assert trades == [("ETH", "long", 50)]


# ── async, callbacks, compatibility ───────────────────────────────────────────

async def test_async_protect_enforces_limits(make_client, state, clock, low_risk):
    shield = vol_shield(make_client, state, clock, limit=100)
    trades = []

    @shield.protect
    async def place_order(coin, side, size_usd):
        trades.append(size_usd)
        return "filled"

    assert await place_order("ETH", "long", 100) == "filled"
    with pytest.raises(LocalLimitBlocked):
        await place_order("ETH", "long", 1)
    assert trades == [100] and len(low_risk.signed) == 1


def test_on_decision_sees_local_blocks(make_client, state, low_risk):
    seen = []
    shield = RiskShield(make_client(), limits=RiskLimits(max_trade_usd=10, state_path=state), on_decision=seen.append)
    with pytest.raises(LocalLimitBlocked):
        shield.guard("ETH", "long", 50)
    assert [(d.stage, d.limit) for d in seen] == [("local_limits", "max_trade_usd")]


def test_except_trade_blocked_still_catches_local_blocks(make_client, state, low_risk):
    shield = RiskShield(make_client(), limits=RiskLimits(max_trade_usd=10, state_path=state))
    try:
        shield.guard("ETH", "long", 50)
    except TradeBlocked as e:   # 0.2.0-era handler
        assert str(e).startswith("Blocked by local risk limits")
    else:
        pytest.fail("not blocked")


def test_no_limits_touches_no_state(make_client, tmp_path, low_risk, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    trades = []
    make_order(RiskShield(make_client()), trades)("ETH", "long", 5_000_000)
    assert trades and not (tmp_path / ".apexrunner").exists()
