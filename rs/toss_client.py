#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
토스증권 Open API 클라이언트 (2026-08 전 고객 오픈).
공식 문서: https://developers.tossinvest.com (OAuth2 client_credentials)

이 모듈은 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 환경변수가 설정된 경우에만 동작한다.
설정돼 있지 않으면 compute_rs.py가 자동으로 FinanceDataReader 방식으로 대체(fallback)한다.

주의(중요): 이 코드는 실제 API에 대해 네트워크로 검증하지 못한 상태로 작성됐다
(작성 환경의 네트워크 정책상 openapi.tossinvest.com 접근이 막혀 있었음).
공개된 2차 자료(가이드 블로그)를 근거로 엔드포인트/필드명을 최대한 방어적으로
작성했으니, 최초 실행 시 반드시 GitHub Actions에서 workflow_dispatch로
한 번 직접 돌려보고 로그의 [toss] 경고를 확인할 것.
"""
import os, time, base64, json
import requests

BASE = "https://openapi.tossinvest.com"
_TOKEN = {"value": None, "exp": 0}


def enabled():
    return bool(os.environ.get("TOSS_CLIENT_ID") and os.environ.get("TOSS_CLIENT_SECRET"))


def _get_token():
    now = time.time()
    if _TOKEN["value"] and now < _TOKEN["exp"] - 60:
        return _TOKEN["value"]
    cid = os.environ["TOSS_CLIENT_ID"]
    csec = os.environ["TOSS_CLIENT_SECRET"]
    basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
    r = requests.post(
        f"{BASE}/oauth2/token",
        headers={"Authorization": f"Basic {basic}",
                  "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials"},
        timeout=10,
    )
    r.raise_for_status()
    j = r.json()
    tok = j.get("access_token") or j.get("accessToken")
    if not tok:
        raise RuntimeError(f"[toss] 토큰 응답에 access_token이 없음: {j}")
    _TOKEN["value"] = tok
    _TOKEN["exp"] = now + float(j.get("expires_in", j.get("expiresIn", 3600)))
    return tok


def _get(path, params=None, retries=2):
    for attempt in range(retries + 1):
        try:
            tok = _get_token()
            r = requests.get(f"{BASE}{path}", params=params or {},
                              headers={"Authorization": f"Bearer {tok}"}, timeout=10)
            if r.status_code == 401:
                _TOKEN["value"] = None  # 토큰 만료 추정 → 1회 재발급 후 재시도
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt >= retries:
                raise
            time.sleep(0.5 * (attempt + 1))
    return None


def fetch_stock_master():
    """전 종목 마스터(코드/이름/시장/업종) 조회. 실패 시 None을 반환해 상위에서 폴백하게 한다.
    응답 스키마가 다를 수 있어 여러 후보 키로 방어적으로 파싱한다."""
    try:
        j = _get("/v1/stocks")
    except Exception as e:
        print("[toss] 종목마스터 조회 실패:", e)
        return None
    items = None
    for key in ("stocks", "items", "data", "list"):
        if isinstance(j, dict) and key in j:
            items = j[key]
            break
    if items is None and isinstance(j, list):
        items = j
    if not items:
        print("[toss] 종목마스터 응답 형식을 해석하지 못함:", str(j)[:200])
        return None

    def pick(d, *keys, default=None):
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return d[k]
        return default

    out = []
    for it in items:
        code = pick(it, "code", "symbol", "ticker", "productCode")
        name = pick(it, "name", "korName", "stockName", default=code)
        market = pick(it, "market", "exchange", "marketType", default="")
        sector = pick(it, "sector", "sectorName", "industry", "industryName", default="기타")
        if not code:
            continue
        out.append({"code": str(code), "name": str(name), "market": str(market), "sector": str(sector)})
    return out or None


def fetch_price(code):
    """현재가/등락률 조회. {price, prevClose} 형태로 정규화. 실패 시 None."""
    try:
        j = _get("/v1/market/price", params={"code": code})
    except Exception:
        return None
    if not isinstance(j, dict):
        return None
    price = j.get("price") or j.get("currentPrice") or j.get("last")
    prev = j.get("prevClose") or j.get("previousClose") or j.get("baseClose")
    if price is None:
        return None
    try:
        return {"price": float(price), "prevClose": float(prev) if prev is not None else None}
    except (TypeError, ValueError):
        return None


def fetch_candles(code, days=420):
    """일봉 캔들 조회 → pandas Series(종가, 최신순 아님/오름차순 정렬해서 반환)."""
    import pandas as pd
    try:
        j = _get("/v1/market/candles", params={"code": code, "period": "day", "count": days})
    except Exception:
        return None
    items = None
    for key in ("candles", "items", "data"):
        if isinstance(j, dict) and key in j:
            items = j[key]
            break
    if items is None and isinstance(j, list):
        items = j
    if not items:
        return None
    closes = []
    for c in items:
        v = c.get("close") or c.get("closePrice") or c.get("c")
        if v is not None:
            closes.append(float(v))
    if len(closes) < 25:
        return None
    return pd.Series(closes)  # 과거→최근 순으로 온다고 가정(문서 미확인 — 실제 실행 시 검증 필요)
