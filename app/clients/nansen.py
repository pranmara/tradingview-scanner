from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import httpx
from redis.asyncio import Redis

from app.asset_classifier import AssetInfo
from app.resilience import raise_for_status, with_retry
from app.schemas import OnChainSnapshot

logger = logging.getLogger(__name__)

_SM_KEYS = (
    "smart_trader_net_flow_usd", "smart_money_net_flow_usd", "smart_money_netflow_usd", "smartTraderNetFlowUsd",
    "sm_netflow_usd", "smart_trader_netflow_usd", "netflow_24h_usd", "net_flow_usd",
)
_EXCHANGE_KEYS = ("exchange_net_flow_usd", "exchange_netflow_usd", "exchangeNetFlowUsd", "cex_net_flow_usd", "exchanges_net_flow_usd")
_NESTED_FLOW_KEYS = ("net_flow_usd", "netflow_usd", "net_flow", "netflow", "value_usd")
_SM_NESTED = ("smart_trader", "smart_traders", "smart_money", "smartMoney")
_EXCHANGE_NESTED = ("exchange", "exchanges", "cex")
_SHARE_KEYS = ("ownership_percentage", "share_pct", "percentage", "holding_pct", "balance_pct", "token_share", "share")
_CHANGE_KEYS = ("balance_change_24h_pct", "change_24h_pct", "balance_change_pct_24h", "ownership_change_24h", "pct_change_24h")


def _num(d: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v.replace(",", ""))
            except ValueError:
                continue
    return None


def _flow(d: dict[str, Any], direct: tuple[str, ...], nested: tuple[str, ...]) -> float | None:
    val = _num(d, direct)
    if val is not None:
        return val
    for k in nested:
        inner = d.get(k)
        if isinstance(inner, dict):
            val = _num(inner, _NESTED_FLOW_KEYS)
            if val is not None:
                return val
    return None


def _records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("data", "result", "results", "items", "records"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
            if isinstance(inner, dict):
                return [inner]
        return [data]
    return []


class NansenClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: str | None,
        base_url: str,
        token_map: dict[str, dict[str, str]],
        cache: Redis | None = None,
        cache_ttl_seconds: int = 300,
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._token_map = token_map
        self._cache = cache
        self._ttl = cache_ttl_seconds

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    @staticmethod
    def load_token_map(path: str) -> dict[str, dict[str, str]]:
        p = Path(path)
        if not p.exists():
            logger.warning("nansen token map not found", extra={"path": path})
            return {}
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {k.upper(): v for k, v in raw.items() if isinstance(v, dict) and "address" in v and "chain" in v}

    def resolve(self, asset: AssetInfo) -> tuple[str, str] | None:
        entry = self._token_map.get(asset.base.upper())
        return (entry["chain"], entry["address"]) if entry else None

    @with_retry(attempts=4, initial=1.0, maximum=10.0)
    async def _post_uncached(self, path: str, body: dict[str, Any]) -> Any:
        resp = await self._http.post(
            f"{self._base}{path}",
            json=body,
            headers={"apiKey": self._api_key or "", "Content-Type": "application/json", "Accept": "application/json"},
        )
        raise_for_status(resp)
        return resp.json()

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        key = "nansen:" + hashlib.sha1((path + json.dumps(body, sort_keys=True)).encode()).hexdigest()
        if self._cache is not None:
            try:
                hit = await self._cache.get(key)
                if hit:
                    return json.loads(hit)
            except Exception as exc:  # noqa: BLE001
                logger.warning("nansen cache read failed", extra={"error": str(exc)})
        data = await self._post_uncached(path, body)
        if self._cache is not None:
            try:
                await self._cache.set(key, json.dumps(data), ex=self._ttl)
            except Exception as exc:  # noqa: BLE001
                logger.warning("nansen cache write failed", extra={"error": str(exc)})
        return data

    async def _flow_intelligence(self, chain: str, address: str) -> dict[str, Any]:
        data = await self._post(
            "/tgm/flow-intelligence",
            {"parameters": {"chain": chain, "tokenAddress": address, "timeframe": "1d"}},
        )
        rows = _records(data)
        return rows[0] if rows else {}

    async def _holders(self, chain: str, address: str) -> dict[str, float | None]:
        data = await self._post(
            "/tgm/holders",
            {"parameters": {"chain": chain, "tokenAddress": address}, "pagination": {"page": 1, "recordsPerPage": 10}},
        )
        rows = _records(data)
        shares = [s for s in (_num(r, _SHARE_KEYS) for r in rows) if s is not None]
        changes = [c for c in (_num(r, _CHANGE_KEYS) for r in rows) if c is not None]
        return {
            "concentration_pct": sum(shares) if shares else None,
            "concentration_change_pct": sum(changes) / len(changes) if changes else None,
        }

    async def get_onchain_snapshot(self, asset: AssetInfo) -> OnChainSnapshot | None:
        if not self.enabled:
            return None
        resolved = self.resolve(asset)
        if resolved is None:
            logger.info("no nansen token mapping", extra={"symbol": asset.symbol})
            return None
        chain, address = resolved
        flows, holders = await asyncio.gather(
            self._flow_intelligence(chain, address), self._holders(chain, address), return_exceptions=True
        )
        snap = OnChainSnapshot(chain=chain, token_address=address, source="nansen")
        if isinstance(flows, dict):
            snap.sm_netflow_24h_usd = _flow(flows, _SM_KEYS, _SM_NESTED)
            snap.exchange_netflow_24h_usd = _flow(flows, _EXCHANGE_KEYS, _EXCHANGE_NESTED)
        else:
            logger.warning("nansen flow-intelligence failed", extra={"symbol": asset.symbol, "error": str(flows)})
        if isinstance(holders, dict):
            snap.top_holder_concentration_pct = holders.get("concentration_pct")
            snap.top_holder_concentration_change_pct = holders.get("concentration_change_pct")
        else:
            logger.warning("nansen holders failed", extra={"symbol": asset.symbol, "error": str(holders)})
        return snap
