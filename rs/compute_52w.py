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

신고가/신저가 종목이 많은 날엔 "돌파/붕괴 강도"(오늘을 뺀 52주 구간의 이전 최고/최저 대비
오늘 종가가 몇 % 위/아래인지) 순으로 하루 최대 --top-n(기본 20)개까지만 남기고, 각 종목마다
'사유' 칸을 자동으로 채운다 — 한국 종목은 네이버 금융 뉴스, 미국 종목은 Google 뉴스 RSS에서
그 종목 이름으로 검색한 최신 헤드라인 1건을 그대로 붙인다. 사람이 실제로 원인을 검증한 게
아니라 '이 종목 이름으로 최근 뜬 기사'일 뿐이므로 추정치다(공시/DART 연동은 종목코드→
corp_code 매핑이 추가로 필요해서 아직 안 함 — 필요하면 나중에 추가 가능).
"""
import json, time, sys, argparse, os, warnings, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

warnings.simplefilter("ignore")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compute_rs as cr  # 임포트만으로 requests 헤더 우회 몽키패치가 적용됨 (compute_rs.py 자체는 수정 안 함)
import FinanceDataReader as fdr

MAX_WORKERS = int(os.environ.get("W52_MAX_WORKERS", 24))  # 유니버스가 훨씬 커서 RS보다 동시성을 높임
TOP_N = int(os.environ.get("W52_TOP_N", 20))  # 신고가/신저가 각각 하루에 몇 종목까지 남길지


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
    # 돌파/붕괴 강도 — 오늘을 뺀 52주 구간의 이전 최고/최저 대비 오늘 종가가 몇 % 위/아래인지.
    # 신고가/신저가 종목이 많을 때 "얼마나 강하게 갱신했는지" 기준으로 상위 N개를 추리는 데 쓴다
    # (신고가 종목은 전부 pctFromHigh=0이라 그 값만으로는 순위를 못 매김).
    window = close.iloc[-252:] if len(close) > 252 else close
    prev_window = window.iloc[:-1]
    breakout_pct = breakdown_pct = None
    if len(prev_window) >= 5:
        last = float(close.iloc[-1])
        prev_hi, prev_lo = float(prev_window.max()), float(prev_window.min())
        if prev_hi > 0:
            breakout_pct = round((last / prev_hi - 1.0) * 100, 2)   # 신고가일 때만 의미 있음(양수)
        if prev_lo > 0:
            breakdown_pct = round((last / prev_lo - 1.0) * 100, 2)  # 신저가일 때만 의미 있음(음수)
    return {
        "code": code, "name": name, "market": region, "sector": sector, "exchange": exchange,
        "last": round(float(close.iloc[-1]), 4),
        "high52": round(hi, 4), "low52": round(lo, 4),
        "pctFromHigh": pct_from_high, "pctFromLow": pct_from_low,
        "isHigh52": is_high, "isLow52": is_low,
        "breakoutPct": breakout_pct, "breakdownPct": breakdown_pct,
    }


def _kr_news_headline(code):
    """네이버 금융 종목 뉴스 목록에서 가장 최근 헤드라인 1건을 가져온다(공식 API가 아니라
    화면 HTML을 파싱 — compute_rs 임포트로 이미 적용된 브라우저 헤더 우회를 그대로 씀).
    실패하거나 뉴스가 없으면 None — 호출부에서 '사유 없음'으로 처리한다."""
    url = f"https://finance.naver.com/item/news_news.naver?code={code}&page=1"
    try:
        r = requests.get(url, timeout=8)
        r.encoding = r.apparent_encoding or "euc-kr"  # 페이지 인코딩이 바뀌어도 최대한 안 깨지게
        soup = BeautifulSoup(r.text, "html.parser")
        a = (soup.select_one("table.type5 td.title a")
             or soup.select_one("td.title a")
             or soup.select_one(".tb_cont a"))
        if not a:
            return None
        title = a.get_text(strip=True)
        if not title:
            return None
        href = a.get("href", "")
        link = ("https://finance.naver.com" + href) if href.startswith("/") else href
        return {"title": title, "url": link, "source": "네이버 금융 뉴스"}
    except Exception:
        return None


def _us_news_headline(code, name):
    """Google 뉴스 RSS에서 회사명으로 검색한 최신 헤드라인 1건. 실패/무결과 시 None."""
    q = urllib.parse.quote(f"{name} stock")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    try:
        r = requests.get(url, timeout=8)
        root = ET.fromstring(r.content)
        item = root.find("./channel/item")
        if item is None:
            return None
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title:
            return None
        return {"title": title, "url": link, "source": "Google 뉴스"}
    except Exception:
        return None


def attach_reason(rec):
    """신고가/신저가 '사유' 칸에 쓸 관련 기사를 자동 검색해서 붙인다. 공시(DART)까지는 아직
    연동 안 함 — 종목코드→DART corp_code 매핑(별도 마스터 파일 다운로드/파싱)이 필요해서
    비용 대비 지금은 뉴스 헤드라인만으로 충분하다고 판단(추정 사유로도 괜찮다고 하셨음).
    나중에 원하면 공시 연동을 추가할 수 있다. 찾은 기사는 실제 원인이 맞다고 검증된 게
    아니라 '이 종목 이름으로 최근 뜬 기사'일 뿐이라 어디까지나 추정이다."""
    try:
        news = _kr_news_headline(rec["code"]) if rec["market"] == "한국" else _us_news_headline(rec["code"], rec["name"])
    except Exception:
        news = None
    rec["reason"] = news
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="테스트용 종목 수 제한")
    ap.add_argument("--out", default="data/w52.json")
    ap.add_argument("--workers", type=int, default=MAX_WORKERS)
    ap.add_argument("--top-n", type=int, default=TOP_N, help="신고가/신저가 각각 하루 최대 종목 수")
    ap.add_argument("--news-workers", type=int, default=8, help="사유(관련 기사) 조회 동시 실행 수")
    ap.add_argument("--no-reason", action="store_true", help="사유(관련 기사 자동 검색) 생략(속도 우선)")
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

    highs_all = [r for r in recs if r["isHigh52"]]
    lows_all = [r for r in recs if r["isLow52"]]
    # 신고가/신저가 종목이 많은 날엔 "얼마나 강하게 갱신했는지"(breakoutPct/breakdownPct) 순으로
    # 하루 --top-n(기본 20)개까지만 남긴다 — 사유 조회(뉴스 검색)도 이 개수만큼만 수행해서
    # 요청 수를 억제한다.
    highs_all.sort(key=lambda r: (r["breakoutPct"] if r["breakoutPct"] is not None else -1e9), reverse=True)
    lows_all.sort(key=lambda r: (r["breakdownPct"] if r["breakdownPct"] is not None else 1e9))
    highs = highs_all[:args.top_n]
    lows = lows_all[:args.top_n]

    if not args.no_reason:
        picked = highs + lows
        with ThreadPoolExecutor(max_workers=args.news_workers) as ex:
            list(ex.map(attach_reason, picked))
        found = sum(1 for r in picked if r.get("reason"))
        print(f"[w52] 관련 기사(사유) 자동 검색: {found}/{len(picked)}건 발견")

    updated = datetime.now().isoformat(timespec="minutes")
    payload = {
        "updated": updated,
        "scanned": len(recs),
        "totalHigh52": len(highs_all), "totalLow52": len(lows_all),  # 상위 N으로 자르기 전 전체 건수
        "countHigh52": len(highs), "countLow52": len(lows),          # 실제로 담긴 건수 (<= top-n)
        "topN": args.top_n,
        "items": highs + lows,
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[done] 스캔 {len(recs)}종목(성공 {ok}/실패 {fail}) → {args.out} "
          f"(신고가 {len(highs)}/{len(highs_all)} · 신저가 {len(lows)}/{len(lows_all)}, 상위 {args.top_n})")


if __name__ == "__main__":
    main()
