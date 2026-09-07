#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
토스증권 Open API 클라이언트 (2026-08 전 고객 오픈).
공식 문서(canonical): https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
사람이 읽는 요약: https://openapi.tossinvest.com/openapi-docs/overview.md

이 모듈은 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 환경변수가 설정된 경우에만 동작한다.
설정돼 있지 않으면 compute_rs.py가 자동으로 FinanceDataReader 방식으로 대체(fallback)한다.

주의(중요, 실제 공식 문서로 확인한 두 가지 한계):
1. 이 API는 "허용 IP 목록"에 등록된 IP에서만 호출을 허용하고, 등록 안 된 IP는 403으로
   거부한다(공식 문서 명시). GitHub Actions 러너는 실행마다 IP가 바뀌는 공유 대역이라
   사전 등록이 안 될 가능성이 높다 — 토스 개발자센터에서 IP 제한 설정을 확인할 것.
2. 종목마스터(/api/v1/stocks, /api/v1/stocks/all) 응답에는 업종(Sector/GICS) 필드가
   전혀 없다. 그래서 토스를 쓰더라도 테마(업종) 분류는 여전히 FinanceDataReader/KRX
   데이터에 의존한다 — build_universe()가 이를 어떻게 처리하는지 참고.
"""
import os, time, json
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
    # 공식 문서(openapi.tossinvest.com/openapi-docs) 확인 결과: 이 엔드포인트는 인증이
    # 필요 없고(Authorization 헤더 없음), client_id/client_secret을 Basic 헤더가 아니라
    # grant_type과 함께 폼 바디에 그대로 담아 보내야 한다.
    r = requests.post(
        f"{BASE}/oauth2/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": cid,
            "client_secret": csec,
        },
        timeout=10,
    )
    if r.status_code == 403:
        # 문서에 명시된 403 원인: 허용 IP 목록에 등록되지 않은 IP에서의 요청.
        # GitHub Actions 러너는 매 실행마다 IP가 바뀌는 공유 대역이라 이 방식으로는
        # 사전 등록이 불가능할 수 있다 — 아래 예외 메시지로 원인을 명확히 알린다.
        raise RuntimeError(
            "토큰 발급 403 Forbidden — 토스 Open API는 허용 IP 목록에 없는 요청을 403으로 "
            "막는다. GitHub Actions 러너 IP는 실행마다 바뀌므로 IP 허용목록에 사전 등록이 "
            "안 될 수 있음 (토스 개발자센터에서 IP 제한 설정을 확인할 것): " + r.text[:300]
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


_MARKETS = ["KOSPI", "KOSDAQ", "NYSE", "NASDAQ", "AMEX"]


def fetch_stock_master():
    """전 종목 마스터(코드/이름/시장) 조회. 공식 문서(openapi-docs) 기준 /api/v1/stocks/all은
    market별로 나눠 조회해야 하고(필수 파라미터), symbol/name/securityType/isinCode만 준다 —
    업종(Sector) 필드는 이 API에 없다. 그래서 sector는 항상 "기타"로 채우고, 실제 업종
    분류는 상위(build_universe)에서 FDR 쪽 데이터로 보강해야 한다.
    실패 시 None을 반환해 상위에서 FDR로 폴백하게 한다."""
    out = []
    ok_any = False
    for market in _MARKETS:
        try:
            j = _get("/api/v1/stocks/all", params={"market": market})
        except Exception as e:
            print(f"[toss] 종목마스터({market}) 조회 실패:", e)
            continue
        items = None
        for key in ("items", "data", "list", "stocks"):
            if isinstance(j, dict) and key in j:
                items = j[key]
                break
        if items is None and isinstance(j, list):
            items = j
        if not items:
            continue
        ok_any = True
        for it in items:
            code = it.get("symbol") or it.get("code")
            name = it.get("name") or code
            if not code:
                continue
            out.append({"code": str(code), "name": str(name), "market": market, "sector": "기타"})
    if not ok_any:
        return None
    return out or None


def fetch_price(code):
    """현재가/등락률 조회. {price, prevClose} 형태로 정규화. 실패 시 None.
    공식 문서 기준 쿼리 파라미터는 code가 아니라 symbols(콤마 구분, 최대 200개)."""
    try:
        j = _get("/api/v1/prices", params={"symbols": code})
    except Exception:
        return None
    item = None
    if isinstance(j, dict):
        items = j.get("items") or j.get("data") or j.get("list")
        if isinstance(items, list) and items:
            item = items[0]
        elif "lastPrice" in j:
            item = j
    elif isinstance(j, list) and j:
        item = j[0]
    if not isinstance(item, dict):
        return None
    price = item.get("lastPrice") or item.get("price")
    prev = item.get("prevClose") or item.get("previousClose") or item.get("baseClose")
    if price is None:
        return None
    try:
        return {"price": float(price), "prevClose": float(prev) if prev is not None else None}
    except (TypeError, ValueError):
        return None


def fetch_candles(code, days=420):
    """일봉 캔들 조회 → pandas Series(종가, 오름차순으로 정렬해서 반환).
    공식 문서 기준: 파라미터는 symbol(단수)/interval("1d")/count(최대 200)/before(페이지네이션).
    count가 최대 200이라 420일치가 필요하면 before로 과거 방향으로 페이지를 더 가져온다."""
    import pandas as pd
    all_items = []
    before = None
    try:
        for _ in range(3):  # 200 * 3 = 최대 600개까지 확보 시도
            params = {"symbol": code, "interval": "1d", "count": min(200, days)}
            if before:
                params["before"] = before
            j = _get("/api/v1/candles", params=params)
            items = None
            if isinstance(j, dict):
                items = j.get("items") or j.get("data") or j.get("candles")
            elif isinstance(j, list):
                items = j
            if not items:
                break
            all_items = items + all_items  # 과거 페이지를 앞쪽에 붙임
            if len(all_items) >= days or len(items) < 200:
                break
            oldest = items[0]
            before = oldest.get("timestamp") or oldest.get("time")
            if not before:
                break
    except Exception:
        return None
    if not all_items:
        return None
    closes = []
    for c in all_items:
        v = c.get("closePrice") or c.get("close") or c.get("c")
        if v is not None:
            closes.append(float(v))
    if len(closes) < 25:
        return None
    return pd.Series(closes)  # 과거→최근 순(오름차순)
