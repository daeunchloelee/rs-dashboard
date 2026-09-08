#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
52주 신고가·신저가 스크리너 — 기존 rs/compute_rs.py(RS 대시보드)와는 완전히 독립적으로
동작하는 별도 스크립트다. compute_rs.py는 이 파일이 전혀 건드리지 않는다(import만 해서
KRX 캐시 폴백 로직 등 이미 검증된 코드를 재사용할 뿐).

RS(장기/단기 상대강도)는 계산하지 않는다 — 오직 종가 기준 52주(252거래일) 최고가/최저가만
본다. 그래서 장 마감 후 하루 한 번만 갱신해도 충분하고(실시간 필요 없음), 그 대신
RS 대시보드보다 훨씬 넓은 유니버스를 스캔할 수 있다:

  - 코스피 전체 + 코스닥 전체 (RS 대시보드처럼 시가총액 상위로 자르지 않음)
  - 한국 ETF 전체 (fdr.StockListing('ETF/KR') 전체 — RS 대시보드가 쓰는 18개 curated
    리스트가 아니라 상장된 모든 ETF)
  - 미국 S&P500 (RS 대시보드와 동일한 소스)
  - 미국 ETF (RS 대시보드와 같은 39개 주요 ETF 리스트를 재사용 — FinanceDataReader가 해외
    ETF '전체 목록' 조회는 지원하지 않아서(공식적으로 지원 중단), 전체 목록을 얻을 방법이
    없다. 필요하면 rs/compute_rs.py의 ETF_US 리스트에 종목을 추가하면 여기도 같이 늘어난다)

결과는 data/w52.json 하나만 만든다 (data/rs.json, data/portfolio.json 등 RS 대시보드가
쓰는 파일은 손대지 않는다).
"""
import json, time, sys, argparse, os, warnings
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.simplefilter("ignore")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compute_rs as cr  # 임포트만으로 requests 헤더 우회 몽키패치가 적용됨 (compute_rs.py 자체는 수정 안 함)
import FinanceDataReader as fdr

MAX_WORKERS = int(os.environ.get("W52_MAX_WORKERS", 24))  # 유니버스가 훨씬 커서 RS보다 동시성을 높임


def _kr_etf_listing_all():
    """한국 상장 ETF '전체' 목록. compute_rs.ETF_KR(18개 curated)과 달리 필터 없이 상장된
    모든 ETF를 담는다 — 이 스크리너는 테마 투자용이 아니라 스크리닝용이라 넓게 보는 게 맞다."""
    for attempt in range(2):
        try:
            df = fdr.StockListing("ETF/KR")
            code_col = "Symbol" if "Symbol" in df.columns else df.columns[0]
            name_col = "Name" if "Name" in df.columns else df.columns[1]
            out = [(str(r[code_col]).zfill(6), str(r[name_col]), "한국", "ETF", None)
                   for _, r in df.iterrows()]
            print(f"[w52] 한국 ETF 전체: {len(out)}")
            return out
        except Exception as e:
            if attempt == 0:
                time.sleep(2)
            else:
                print("[w52] 한국 ETF 목록 조회 실패:", e)
    return []


def build_universe(limit=None):
    """[(code, name, region, sector, marcap_or_None, exchange), ...]. RS 대시보드의
    build_universe()와 달리 한국 종목은 top_n 없이 _kr_listing()을 호출해서(top_n=None →
    시총 제한 없음) 코스피·코스닥 전체를 담는다. exchange는 프런트에서 코스피/코스닥/한국ETF/
    S&P500/미국ETF 5단 필터를 만드는 데 쓴다(market="한국"/"미국"만으로는 이 구분이 안 됨)."""
    kospi = [(*row, "코스피") for row in cr._kr_listing("KOSPI")]
    kosdaq = [(*row, "코스닥") for row in cr._kr_listing("KOSDAQ")]
    kr_all = kospi + kosdaq
    kr_etf = [(*row, "한국ETF") for row in _kr_etf_listing_all()]
    sp500 = cr._sp500_constituents()
    sp500_rows = [(sym, name, "미국", sector, None, "S&P500") for sym, name, sector in sp500]
    us_etf = [(t, cr.ETF_US_NAMES.get(t, t), "미국", "ETF", None, "미국ETF") for t in cr.ETF_US]

    uni = kr_all + kr_etf + sp500_rows + us_etf
    seen, out = set(), []
    for row in uni:
        key = (row[0], row[2])  # 코드+지역을 키로 (한국/미국 코드 체계가 달라 코드만으로는 부족)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    if limit:
        out = out[:limit]
    print(f"[w52] 총 {len(out)}종목 (코스피 {len(kospi)} · 코스닥 {len(kosdaq)} · 한국 ETF 전체 {len(kr_etf)} · "
          f"S&P500 {len(sp500_rows)} · 미국 ETF {len(us_etf)})")
    return out


def _process_one(args):
    code, name, region, sector, exchange = args
    start = (datetime.now() - timedelta(days=420)).strftime("%Y-%m-%d")
    close = cr.fetch_close(code, start)
    if close is None or len(close) < 30:
        return None
    hi, lo, pct_from_high, pct_from_low, is_high, is_low = cr.week52(close)
    if hi is None:
        return None
    return {
        "code": code, "name": name, "market": region, "sector": sector, "exchange": exchange,
        "last": round(float(close.iloc[-1]), 4),
        "high52": round(hi, 4), "low52": round(lo, 4),
        "pctFromHigh": pct_from_high, "pctFromLow": pct_from_low,
        "isHigh52": is_high, "isLow52": is_low,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="테스트용 종목 수 제한")
    ap.add_argument("--out", default="data/w52.json")
    ap.add_argument("--workers", type=int, default=MAX_WORKERS)
    ap.add_argument("--keep-all", action="store_true",
                     help="신고가/신저가 종목만 남기지 않고 스캔한 전 종목을 결과에 포함 (파일이 커짐)")
    args = ap.parse_args()

    uni = build_universe(args.limit)
    jobs = [(code, name, region, sector, exchange) for code, name, region, sector, _marcap, exchange in uni]

    recs = []
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_process_one, j): j for j in jobs}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            if r is None:
                fail += 1
            else:
                ok += 1
                recs.append(r)
            if i % 300 == 0:
                print(f"  ...{i}/{len(jobs)}  (ok {ok} / fail {fail})")

    if not recs:
        print("[error] 가격을 가져온 종목이 없습니다.")
        sys.exit(1)

    highs = [r for r in recs if r["isHigh52"]]
    lows = [r for r in recs if r["isLow52"]]
    # 스크리너 용도라 신고가/신저가 종목만 저장한다(전체 스캔 결과를 다 담으면 파일이 너무
    # 커진다 — --keep-all 을 주면 전체를 담아서 나중에 "근접" 기능을 붙이고 싶을 때 쓸 수 있다).
    items = recs if args.keep_all else (highs + lows)

    updated = datetime.now().isoformat(timespec="minutes")
    payload = {
        "updated": updated,
        "scanned": len(recs),
        "countHigh52": len(highs),
        "countLow52": len(lows),
        "items": items,
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[done] 스캔 {len(recs)}종목(성공 {ok}/실패 {fail}) → {args.out} "
          f"(신고가 {len(highs)} · 신저가 {len(lows)})")


if __name__ == "__main__":
    main()
