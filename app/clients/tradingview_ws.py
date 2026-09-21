"""Session-authenticated TradingView websocket client (unofficial protocol).

Pulls the same candles your TradingView chart shows and, experimentally, runs a Pine study by id and returns its
plot values so account-only indicators can feed the scanner. Uses the chart websocket protocol that TradingView's
own web app speaks; it is not a public API and may break without notice.

Manual check:
    python -m app.clients.tradingview_ws BINANCE:BTCUSDT 240
    python -m app.clients.tradingview_ws NASDAQ:AAPL 1D --study "PUB;3a5f2d1c..." --input Length=20
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import string
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

from app.resilience import UpstreamError
from app.schemas import OHLCV, Candle, Timeframe
from app.timeframes import TV_CHART_INTERVAL as _INTERVAL

logger = logging.getLogger(__name__)

WS_URL = "wss://data.tradingview.com/socket.io/websocket?from=chart"
# The per-message timeout alone never fires: TradingView sends a heartbeat roughly every 10s, and each one
# resets it. A study that never completes — no study_completed, no study_error — used to hang forever, and
# with it every /scan that pulls an account indicator. This caps the whole session.
SESSION_TIMEOUT_FACTOR = 4
_ORIGIN = "https://data.tradingview.com"
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
# Only "saved" works. Verified 2026-09-21: pine-facade answers HTTP 400 "Bad 'filter' value" to "favorites" and
# "invite_only", and "all" is built-ins plus TradingView partner add-ons — never a user's invite-only grants.
# Invite-only scripts are reachable only by pine id (PUB;...), which /indicators add accepts.
_LIST_FILTERS = ("saved",)
_FRAME = re.compile(r"~m~\d+~m~")
_AUTH_TOKEN = re.compile(r'"auth_token":"([^"]+)"')


def _rand(prefix: str) -> str:
    return prefix + "".join(random.choices(string.ascii_lowercase, k=12))


def _pack(method: str, params: list[Any]) -> str:
    body = json.dumps({"m": method, "p": params}, separators=(",", ":"))
    return f"~m~{len(body)}~m~{body}"


@dataclass(frozen=True)
class ScriptInfo:
    pine_id: str
    name: str
    version: str
    kind: str
    source: str


@dataclass
class StudyResult:
    pine_id: str
    plots: list[str]
    rows: list[dict[str, float]] = field(default_factory=list)

    @property
    def latest(self) -> dict[str, float]:
        return self.rows[-1] if self.rows else {}


class TradingViewSessionClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        session_id: str | None = None,
        session_sign: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout_seconds: float = 25.0,
    ) -> None:
        self._http = http
        self._session_id = session_id
        self._session_sign = session_sign
        self._username = username
        self._password = password
        self._timeout = timeout_seconds
        self._token: str | None = None
        self._lock = asyncio.Lock()

    @property
    def authenticated(self) -> bool:
        return bool(self._session_id or (self._username and self._password))

    def _cookies(self) -> dict[str, str]:
        c: dict[str, str] = {}
        if self._session_id:
            c["sessionid"] = self._session_id
        if self._session_sign:
            c["sessionid_sign"] = self._session_sign
        return c

    # ------------------------------------------------------------------ auth
    async def auth_token(self) -> str:
        if self._token:
            return self._token
        async with self._lock:
            if self._token:
                return self._token
            token = "unauthorized_user_token"
            headers = {"User-Agent": _UA, "Referer": "https://www.tradingview.com/"}
            try:
                if self._session_id:
                    resp = await self._http.get("https://www.tradingview.com/", headers=headers, cookies=self._cookies())
                    m = _AUTH_TOKEN.search(resp.text)
                    if m:
                        token = m.group(1)
                    else:
                        logger.warning("tradingview session cookie did not yield an auth token; using anonymous access")
                elif self._username and self._password:
                    resp = await self._http.post(
                        "https://www.tradingview.com/accounts/signin/",
                        data={"username": self._username, "password": self._password, "remember": "on"},
                        headers=headers,
                    )
                    data = resp.json()
                    token = (data.get("user") or {}).get("auth_token") or token
                    if token == "unauthorized_user_token":
                        logger.warning("tradingview signin failed (captcha/2FA?); using anonymous access",
                                       extra={"error": str(data.get("error", ""))[:120]})
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("tradingview auth failed; using anonymous access", extra={"error": str(exc)})
            self._token = token
            return token

    # ------------------------------------------------------------------ protocol helpers
    @staticmethod
    def _frames(raw: str) -> list[str]:
        return [p for p in _FRAME.split(raw) if p]

    async def _session(self, symbol: str, timeframe: Timeframe, bars: int, study: tuple[str, dict[str, Any]] | None):
        token = await self.auth_token()
        chart = _rand("cs_")
        candles: dict[int, Candle] = {}
        study_rows: dict[int, list[float]] = {}
        study_id = "st1"
        symbol_spec = json.dumps({"symbol": symbol, "adjustment": "splits", "session": "regular"})

        async with websockets.connect(WS_URL, additional_headers={"Origin": _ORIGIN, "User-Agent": _UA}, max_size=2**24) as ws:
            await ws.send(_pack("set_auth_token", [token]))
            await ws.send(_pack("chart_create_session", [chart, ""]))
            await ws.send(_pack("resolve_symbol", [chart, "sds_sym_1", "=" + symbol_spec]))
            await ws.send(_pack("create_series", [chart, "sds_1", "s1", "sds_sym_1", _INTERVAL[timeframe], bars, ""]))
            await ws.send(_pack("switch_timezone", [chart, "Etc/UTC"]))

            series_done = study_sent = study_done = False
            budget = self._timeout * SESSION_TIMEOUT_FACTOR
            deadline = time.monotonic() + budget
            while not (series_done and (study is None or study_done)):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    waiting = "series" if not series_done else "study"
                    raise UpstreamError(f"tradingview ws: {waiting} did not complete within {budget:.0f}s")
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(self._timeout, remaining))
                except asyncio.TimeoutError:
                    waiting = "series" if not series_done else "study"
                    raise UpstreamError(f"tradingview ws: {waiting} did not complete (no reply in time)") from None
                for frame in self._frames(str(raw)):
                    if frame.startswith("~h~"):
                        await ws.send(f"~m~{len(frame)}~m~{frame}")
                        continue
                    try:
                        msg = json.loads(frame)
                    except json.JSONDecodeError:
                        continue
                    method, params = msg.get("m"), msg.get("p") or []
                    if method in ("timescale_update", "du") and len(params) > 1 and isinstance(params[1], dict):
                        for entry in (params[1].get("sds_1") or {}).get("s") or []:
                            v = entry.get("v") or []
                            if len(v) >= 5:
                                candles[int(v[0]) * 1000] = Candle(ts=int(v[0]) * 1000, open=float(v[1]), high=float(v[2]),
                                                                   low=float(v[3]), close=float(v[4]),
                                                                   volume=float(v[5]) if len(v) > 5 and v[5] is not None else 0.0)
                        for entry in (params[1].get(study_id) or {}).get("st") or []:
                            v = entry.get("v") or []
                            if v:
                                study_rows[int(v[0]) * 1000] = [float(x) if isinstance(x, (int, float)) else float("nan") for x in v[1:]]
                    elif method == "series_completed":
                        series_done = True
                        if study is not None and not study_sent:
                            pine_id, inputs = study
                            await ws.send(_pack("create_study", [chart, study_id, "st1", "sds_1", "Script@tv-scripting-101!", inputs]))
                            study_sent = True
                    elif method == "study_completed":
                        study_done = True
                    elif method in ("symbol_error", "series_error", "study_error", "critical_error", "protocol_error"):
                        raise UpstreamError(f"tradingview ws {method}: {json.dumps(params)[:200]}")
        return candles, study_rows

    # ------------------------------------------------------------------ public API
    async def get_ohlcv(self, symbol: str, timeframe: Timeframe, limit: int) -> OHLCV:
        candles, _ = await self._session(symbol, timeframe, limit, None)
        if not candles:
            raise UpstreamError(f"tradingview ws returned no candles for {symbol}")
        ordered = [candles[k] for k in sorted(candles)]
        return OHLCV(symbol=symbol, timeframe=timeframe, candles=ordered[-limit:], source="tv-session")

    async def list_scripts(self) -> list[ScriptInfo]:
        """The account's own saved scripts. Invite-only scripts cannot be listed — add them by pine id."""
        if not self.authenticated:
            raise UpstreamError("TradingView session not configured (set TV_SESSION_ID)")
        seen: dict[str, ScriptInfo] = {}
        headers = {"User-Agent": _UA, "Referer": "https://www.tradingview.com/"}
        for flt in _LIST_FILTERS:
            try:
                resp = await self._http.get(f"https://pine-facade.tradingview.com/pine-facade/list", params={"filter": flt},
                                            headers=headers, cookies=self._cookies())
            except httpx.HTTPError as exc:
                logger.warning("pine-facade list failed", extra={"filter": flt, "error": str(exc)})
                continue
            if resp.status_code != 200:
                # Warning, not info: a rejected filter silently hid invite-only scripts from /indicators.
                logger.warning("pine-facade list filter rejected",
                               extra={"filter": flt, "status": resp.status_code, "body": resp.text[:160]})
                continue
            try:
                rows = resp.json()
            except ValueError:
                continue
            if isinstance(rows, dict):
                rows = rows.get("results") or rows.get("data") or []
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                pine_id = str(row.get("scriptIdPart") or row.get("id") or "")
                name = str(row.get("scriptName") or row.get("name") or pine_id)
                if not pine_id or pine_id in seen:
                    continue
                kind = str(((row.get("extra") or {}).get("kind")) or row.get("kind") or "study")
                seen[pine_id] = ScriptInfo(pine_id=pine_id, name=name, version=str(row.get("version", "last")), kind=kind, source=flt)
        if not seen:
            raise UpstreamError("no scripts returned — is the session cookie valid?")
        return sorted(seen.values(), key=lambda s: s.name.lower())

    async def translate_pine(self, pine_id: str, version: str = "last") -> dict[str, Any]:
        resp = await self._http.get(
            f"https://pine-facade.tradingview.com/pine-facade/translate/{pine_id}/{version}",
            headers={"User-Agent": _UA, "Referer": "https://www.tradingview.com/"},
            cookies=self._cookies(),
        )
        if resp.status_code != 200:
            raise UpstreamError(f"pine-facade {resp.status_code} for {pine_id} (private script needs a valid session)")
        data = resp.json()
        if not data.get("success", True) or "result" not in data:
            raise UpstreamError(f"pine-facade could not translate {pine_id}: {str(data.get('reason', ''))[:120]}")
        return data["result"]

    @staticmethod
    def _study_inputs(meta: dict[str, Any], overrides: dict[str, Any], pine_id: str) -> tuple[dict[str, Any], list[str]]:
        info = meta.get("metaInfo") or {}
        inputs: dict[str, Any] = {"text": meta.get("ilTemplate", ""), "pineId": pine_id, "pineVersion": info.get("pine", {}).get("version", "1.0")}
        for inp in info.get("inputs") or []:
            iid = inp.get("id")
            if not iid or iid in ("text", "pineId", "pineVersion"):
                continue
            value = overrides.get(inp.get("name", ""), overrides.get(iid, inp.get("defval")))
            inputs[iid] = {"v": value, "f": bool(inp.get("isFake", False)), "t": inp.get("type", "float")}
        styles = info.get("styles") or {}
        plots: list[str] = []
        for plot in info.get("plots") or []:
            pid = plot.get("id", f"plot_{len(plots)}")
            title = (styles.get(pid) or {}).get("title") or pid
            plots.append(re.sub(r"[^A-Za-z0-9_]+", "_", str(title)).strip("_") or pid)
        return inputs, plots

    async def get_study(self, symbol: str, timeframe: Timeframe, pine_id: str, overrides: dict[str, Any] | None = None, bars: int = 300) -> StudyResult:
        meta = await self.translate_pine(pine_id)
        inputs, plots = self._study_inputs(meta, overrides or {}, pine_id)
        _, rows = await self._session(symbol, timeframe, bars, (pine_id, inputs))
        result = StudyResult(pine_id=pine_id, plots=plots)
        for ts in sorted(rows):
            values = rows[ts]
            row: dict[str, float] = {"ts": float(ts)}
            for idx, val in enumerate(values):
                key = plots[idx] if idx < len(plots) else f"plot_{idx}"
                if val == val:  # drop NaN
                    row[key] = val
                    row[f"plot_{idx}"] = val
            result.rows.append(row)
        if not result.rows:
            raise UpstreamError(f"study {pine_id} returned no values")
        return result

    async def health(self) -> str:
        try:
            token = await self.auth_token()
            return "authenticated" if token != "unauthorized_user_token" else "anonymous"
        except Exception as exc:  # noqa: BLE001
            return f"down: {type(exc).__name__}"


# ---------------------------------------------------------------------- CLI
async def _cli(args: argparse.Namespace) -> int:
    from app.timeframes import parse_timeframe

    tf = parse_timeframe(args.interval)
    if tf is None:
        print("interval must be one of 5m,15m,30m,1h,4h,1d,1w (or 5,15,30,60,240,1D,1W)", file=sys.stderr)
        return 2
    overrides: dict[str, Any] = {}
    for item in args.input or []:
        k, _, v = item.partition("=")
        try:
            overrides[k] = json.loads(v)
        except json.JSONDecodeError:
            overrides[k] = v
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as http:
        client = TradingViewSessionClient(http, os.getenv("TV_SESSION_ID"), os.getenv("TV_SESSION_ID_SIGN"),
                                          os.getenv("TV_USERNAME"), os.getenv("TV_PASSWORD"))
        print("auth:", await client.health())
        if args.list:
            for i, s in enumerate(await client.list_scripts(), 1):
                print(f"{i:>3}. {s.name}  [{s.kind}/{s.source}]  {s.pine_id}")
            return 0
        ohlcv = await client.get_ohlcv(args.symbol, tf, args.bars)
        last = ohlcv.candles[-1]
        print(f"candles: {len(ohlcv.candles)}  last close={last.close} ts={last.ts}")
        if args.study:
            study = await client.get_study(args.symbol, tf, args.study, overrides, args.bars)
            print("plots:", study.plots)
            for row in study.rows[-3:]:
                print(row)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Probe the TradingView session feed")
    p.add_argument("symbol")
    p.add_argument("interval", nargs="?", default="4h")
    p.add_argument("--bars", type=int, default=300)
    p.add_argument("--list", action="store_true", help="List scripts available to the account and exit")
    p.add_argument("--study", help='Pine id, e.g. "PUB;abc123" or "USER;abc123"')
    p.add_argument("--input", action="append", help="Study input override Name=value (repeatable)")
    return asyncio.run(_cli(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
