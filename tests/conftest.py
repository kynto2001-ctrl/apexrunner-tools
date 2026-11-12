"""A local mock of APEX Runner's x402 endpoints. Nothing here touches the network.

The 402 challenge is the real one captured from production
(fixtures/challenge_slippage_forecast.json); tests mutate copies of it.
Signed requests are decoded server-side so tests can assert exactly what the
client authorised: who pays, to whom, how much.
"""

from __future__ import annotations

import base64
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import pytest
from eth_account import Account

from apexrunner_tools import ApexClient

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "challenge_slippage_forecast.json").read_text())
MOCK_TX = "0x" + "ab" * 32


def b64json(obj: Any) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def real_challenge() -> dict:
    return copy.deepcopy(FIXTURE)


def default_body(path: str, query: dict) -> dict:
    return {"signal": path.rsplit("/", 1)[-1], "query": query, "valid_for_seconds": 120, "data_age_seconds": 0}


class MockApex:
    def __init__(self) -> None:
        self.challenge: dict = real_challenge()
        # (path, query) -> (status, body dict, extra headers) for a SIGNED request
        self.paid_response: Callable[[str, dict], tuple[int, dict, dict]] = lambda p, q: (200, default_body(p, q), {})
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # silence
                pass

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                sig = self.headers.get("PAYMENT-SIGNATURE") or self.headers.get("X-PAYMENT")
                payload = json.loads(base64.b64decode(sig)) if sig else None
                with mock._lock:
                    mock.requests.append(
                        {"path": parsed.path, "query": query, "ua": self.headers.get("User-Agent"), "payload": payload}
                    )
                if payload is None:
                    self._send(402, {"error": "Payment required"}, {"PAYMENT-REQUIRED": b64json(mock.challenge)})
                    return
                status, body, headers = mock.paid_response(parsed.path, query)
                if status == 200 and "PAYMENT-RESPONSE" not in headers:
                    auth = payload["payload"]["authorization"]
                    headers = {
                        **headers,
                        "PAYMENT-RESPONSE": b64json(
                            {"success": True, "transaction": MOCK_TX, "network": "eip155:8453", "payer": auth["from"]}
                        ),
                    }
                self._send(status, body, headers)

            def _send(self, status: int, body: dict, headers: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/signals"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def signed(self) -> list[dict]:
        with self._lock:
            return [r for r in self.requests if r["payload"] is not None]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def mock_apex():
    m = MockApex()
    yield m
    m.close()


@pytest.fixture
def account():
    return Account.create()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_client(mock_apex, account, clock):
    clients: list[ApexClient] = []

    def _make(**kw: Any) -> ApexClient:
        kw.setdefault("base_url", mock_apex.base_url)
        kw.setdefault("_clock", clock)
        if not any(k in kw for k in ("private_key", "account", "signer")):
            kw["account"] = account
        c = ApexClient(**kw)
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.close()
