#!/usr/bin/env python3
"""
새내기주 트래커 - 현재가 자동 갱신 스크립트

data/companies.json 에 정의된 종목코드로 네이버 증권의 비공식(미문서화) 시세 API를
호출해 data/prices.json 을 갱신한다. 네이버 쪽 API는 언제든 형식이 바뀌거나 막힐 수
있으므로, 실패한 종목은 건너뛰고 이전 값을 유지한 채 진행한다(전체 실패로 워크플로가
죽지 않도록).

실행: python scripts/fetch_prices.py   (표준 라이브러리만 사용, 별도 설치 불필요)
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

COMPANIES_PATH = "data/companies.json"
PRICES_PATH = "data/prices.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://m.stock.naver.com/",
}
TIMEOUT = 10
BATCH_SIZE = 40
NUMERIC_CODE = re.compile(r"^\d{6}$")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fetch_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def to_number(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "N/A", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def pick(d, *keys):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def fetch_polling_batch(codes):
    """여러 종목을 한 번에 조회 (쉼표로 구분). 실패 시 빈 dict 반환."""
    url = "https://m.stock.naver.com/api/polling/domestic/stock?itemCodes=" + ",".join(codes)
    out = {}
    try:
        data = fetch_json(url)
        items = data.get("datas") or data.get("result") or []
        if isinstance(items, dict):
            items = list(items.values())
        for it in items:
            code = pick(it, "itemCode", "itemcode", "code", "cd")
            if not code:
                continue
            price = to_number(pick(it, "closePrice", "nowPrice", "now_val", "nv"))
            rate = to_number(pick(it, "fluctuationsRatio", "prevChangeRate", "changeRate", "rf"))
            if price is not None:
                out[str(code)] = {"price": price, "dayChangePct": rate}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        log(f"[warn] 배치 조회 실패 ({len(codes)}종목): {e}")
    return out


def fetch_detail_single(code):
    """배치 조회에서 빠졌거나 실패한 종목을 개별 상세 API로 재시도."""
    for url in (
        f"https://m.stock.naver.com/api/stock/{code}/basic",
        f"https://m.stock.naver.com/api/stock/{code}/integration",
    ):
        try:
            data = fetch_json(url)
            price = to_number(pick(data, "closePrice", "nowPrice"))
            rate = to_number(pick(data, "fluctuationsRatio", "prevChangeRate"))
            if price is not None:
                return {"price": price, "dayChangePct": rate}
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
            continue
    return None


def main():
    with open(COMPANIES_PATH, encoding="utf-8") as f:
        companies = json.load(f)

    tickers = sorted({c["ticker"] for c in companies if c.get("ticker")})
    numeric = [t for t in tickers if NUMERIC_CODE.match(t)]
    other = [t for t in tickers if not NUMERIC_CODE.match(t)]

    log(f"대상 종목: 숫자코드 {len(numeric)}건, 신형(영숫자)코드 {len(other)}건")

    prices = {}

    # 1) 숫자 코드는 배치(polling) API로 한 번에 조회
    for i in range(0, len(numeric), BATCH_SIZE):
        batch = numeric[i:i + BATCH_SIZE]
        prices.update(fetch_polling_batch(batch))
        time.sleep(0.4)

    # 2) 배치에서 못 가져온 숫자 코드는 개별 재시도
    missing = [t for t in numeric if t not in prices]
    for code in missing:
        r = fetch_detail_single(code)
        if r:
            prices[code] = r
        time.sleep(0.25)

    # 3) 영숫자(신형) 코드는 개별 상세 API만 시도 (배치 API 미지원 가능성 있음)
    for code in other:
        r = fetch_detail_single(code)
        if r:
            prices[code] = r
        time.sleep(0.25)

    result = {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fetched": len(prices),
        "total": len(tickers),
        "prices": prices,
    }

    with open(PRICES_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write("\n")

    log(f"완료: {len(prices)} / {len(tickers)}개 종목 시세 갱신")
    if len(prices) == 0 and len(tickers) > 0:
        log("[warn] 한 건도 가져오지 못했습니다 — 네이버 API 형식이 바뀌었을 수 있습니다.")


if __name__ == "__main__":
    main()
