"""株価・銘柄名取得モジュール。

yfinance をメインに使い、銘柄名はIRバンクの dividend ページから取得する
（yfinance の日本株名は英語表記になりがちなため）。
"""
from __future__ import annotations

import re
import time
from datetime import datetime

import requests
import yfinance as yf
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    )
}


def fetch_name_from_irbank(code: str) -> str | None:
    """IRバンクの dividend ページの <title> から日本語銘柄名を取得する。

    title 例: 「三菱UFJ FG（8306）の配当金推移 - IRBANK」
    """
    try:
        r = requests.get(f"https://irbank.net/{code}/dividend", headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    title = soup.find("title")
    if not title:
        return None
    text = title.get_text(strip=True)
    # 「名前（コード）の...」のパターン
    m = re.match(r"^(.+?)[（\(]" + re.escape(code), text)
    if m:
        return m.group(1).strip()
    return None


def fetch_price_yfinance(code: str) -> dict:
    """yfinance から株価情報を取得する。

    返り値: {"close": 終値, "prev_close": 前日終値, "as_of": 取得日(YYYY-MM-DD)}
    """
    ticker = yf.Ticker(f"{code}.T")
    result = {"close": None, "prev_close": None, "as_of": None}

    try:
        hist = ticker.history(period="7d")
        if hist is not None and not hist.empty:
            # 最新行が NaN(未確定/データ欠損)のことがあるので、有効な終値だけを使う
            closes = hist["Close"].dropna()
            if not closes.empty:
                result["close"] = float(closes.iloc[-1])
                result["as_of"] = closes.index[-1].strftime("%Y-%m-%d")
                if len(closes) >= 2:
                    result["prev_close"] = float(closes.iloc[-2])
    except Exception:
        pass

    return result


def fetch_per_pbr_yfinance(code: str) -> dict:
    """yfinance.info から PER/PBR を取得する。

    PER は予想PER(forwardPE)を優先。取得できない銘柄のみ実績PER(trailingPE)で補完する。
    """
    ticker = yf.Ticker(f"{code}.T")
    per = None
    pbr = None
    try:
        info = ticker.info or {}
        per = info.get("forwardPE")
        if per is None:
            per = info.get("trailingPE")  # 予想が無い銘柄は実績で代用
        pbr = info.get("priceToBook")
    except Exception:
        pass
    return {"per": per, "pbr": pbr}


def fetch_kabutan(code: str) -> dict:
    """株探(kabutan.jp)の銘柄ページから 予想PER・PBR・33業種 を一度に取得する。

    会社予想ベースなので SBI証券など日本の証券会社の表示と一致しやすい。
    PER/PBR: 指標行の並びは [PER, PBR, 利回り(％), 信用倍率] なので、倍/％付き値を
    出現順に拾い先頭2つ(PER, PBR)を採用する。「－」は None。
    業種: industry= を含むアンカーのテキスト(33業種)。
    1リクエストで3つ取れるので Yahoo日本へのアクセスを減らせる(レート制限回避)。
    """
    url = f"https://kabutan.jp/stock/?code={code}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception:
        return {"per": None, "pbr": None, "sector": None}
    html = r.text

    per = pbr = yield_pct = None
    i = html.find("信用倍率")
    if i >= 0:
        chunk = re.sub(r"<[^>]+>", " ", html[i:i + 400])
        # 指標行の並び: [PER, PBR, 利回り(％), 信用倍率]
        pairs = re.findall(r"(－|-|[\d,]+(?:\.\d+)?)\s*(倍|％|%)", chunk)

        def val(idx):
            if idx >= len(pairs):
                return None
            v = pairs[idx][0]
            if v in ("－", "-"):
                return None
            try:
                return float(v.replace(",", ""))
            except ValueError:
                return None

        per, pbr, yield_pct = val(0), val(1), val(2)

    sector = None
    m = re.search(r'<a[^>]*href="[^"]*industry[^"]*"[^>]*>([^<]+)</a>', html)
    if m:
        sector = m.group(1).strip()

    # 銘柄名(IRバンクが使えない環境のフォールバック用)。title例:「全国保証【7164】株の…」
    name = None
    mt = re.search(r"<title>([^<【(（]+)", html)
    if mt:
        name = mt.group(1).strip()

    return {"per": per, "pbr": pbr, "sector": sector, "yield_pct": yield_pct, "name": name}


def fetch_kabutan_dividends(code: str) -> dict:
    """株探(kabutan.jp)の業績ページから 年間配当(決算期ベース) を {年: 配当} で返す。

    IRバンクが使えない環境(海外サーバー等)のフォールバック用。
    暦年合算でない決算期ベースなので、特別配当の混入や今期途中の誤判定が起きにくい。
    予想(予)行も含む。取得失敗時は空dict。
    """
    try:
        html = requests.get(f"https://kabutan.jp/stock/finance?code={code}",
                            headers=HEADERS, timeout=15).text
    except Exception:
        return {}
    by_year = {}
    for tbl in re.findall(r"<table[^>]*>(.*?)</table>", html, re.DOTALL):
        if "決算期" not in tbl or "1株配" not in tbl:
            continue
        div_idx = None
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.DOTALL):
            cells = [re.sub(r"<[^>]+>", "", c).strip()
                     for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.DOTALL)]
            cells = [c for c in cells if c != ""]
            if not cells:
                continue
            if div_idx is None:  # ヘッダ行で「1株配」列の位置を特定
                if any("1株配" in c for c in cells):
                    for j, c in enumerate(cells):
                        if "1株配" in c:
                            div_idx = j
                continue
            ym = re.match(r".*?(\d{4})\.(\d{1,2})", cells[0])  # 決算期 例: 2026.03 / 予 2027.03
            if not ym or div_idx >= len(cells):
                continue
            try:
                by_year[int(ym.group(1))] = float(cells[div_idx].replace(",", ""))
            except ValueError:
                pass
        if by_year:
            break
    return by_year


def fetch_kabutan_financials(code: str) -> list:
    """株探の業績ページから 決算期ごとの EPS・配当・最終益 を返す。

    返り値: [{year, eps, dividend, net_profit, is_forecast}, ...] 古い順。予想行(予)含む。
    配当性向・減配リスク判定・成長判定に使う。取得失敗時は空list。
    """
    try:
        html = requests.get(f"https://kabutan.jp/stock/finance?code={code}",
                            headers=HEADERS, timeout=15).text
    except Exception:
        return []
    for tbl in re.findall(r"<table[^>]*>(.*?)</table>", html, re.DOTALL):
        if not ("決算期" in tbl and "1株配" in tbl and "1株益" in tbl):
            continue
        idx = {}
        out = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.DOTALL):
            cells = [re.sub(r"<[^>]+>", "", c).strip()
                     for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.DOTALL)]
            cells = [c for c in cells if c != ""]
            if not cells:
                continue
            if not idx:  # ヘッダ行で列位置を特定
                if any("1株配" in c for c in cells) and any("1株益" in c for c in cells):
                    for j, c in enumerate(cells):
                        if "最終益" in c:
                            idx["net"] = j
                        elif "1株益" in c:
                            idx["eps"] = j
                        elif "1株配" in c:
                            idx["div"] = j
                continue
            ym = re.match(r".*?(\d{4})\.(\d{1,2})", cells[0])
            if not ym:
                continue

            def num(j):
                if j is None or j >= len(cells):
                    return None
                v = cells[j].replace(",", "").replace("－", "").strip()
                try:
                    return float(v)
                except ValueError:
                    return None

            out.append({
                "year": int(ym.group(1)),
                "eps": num(idx.get("eps")),
                "dividend": num(idx.get("div")),
                "net_profit": num(idx.get("net")),
                "is_forecast": "予" in cells[0],
            })
        if out:
            return out
    return []


def fetch_per_pbr_yahoo(code: str) -> dict:
    """finance.yahoo.co.jp の参考指標から 予想PER・実績PBR を取得する。

    ページ内の二重エスケープJSON (\\"per\\":{...}) から per(予想)/pbr(実績) を抜き出す。
    値が「---」等で取れない場合は None を返す(呼び出し側で yfinance に補完させる)。
    """
    url = f"https://finance.yahoo.co.jp/quote/{code}.T"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception:
        return {"per": None, "pbr": None}
    html = r.text.replace('\\"', '"')  # 二重エスケープを解除

    def grab(key: str, name: str, sub: str):
        m = re.search(
            r'"%s":\{"name":"%s","subText":"[^"]*%s[^"]*","value":"([\d.,]+)"' % (key, name, sub),
            html,
        )
        if not m:
            return None
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None

    return {"per": grab("per", "PER", "予想"), "pbr": grab("pbr", "PBR", "実績")}


def fetch_sector_yahoo(code: str) -> str | None:
    """Yahoo!ファイナンス のプロフィールページから33業種を取得する。"""
    import re as _re
    url = f"https://finance.yahoo.co.jp/quote/{code}.T/profile"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    for dt in soup.find_all(["dt", "th"]):
        if "業種" in dt.get_text(strip=True):
            sib = dt.find_next(["dd", "td"])
            if sib:
                return sib.get_text(strip=True)
    m = _re.search(r"業種[\s　:：]*([^\s\n<]{2,30})", soup.get_text())
    return m.group(1).strip() if m else None


def get_price_info(code: str) -> dict:
    """銘柄名+株価+PER+PBR+業種 をまとめて取得する。"""
    name = fetch_name_from_irbank(code)
    time.sleep(0.3)
    price = fetch_price_yfinance(code)
    time.sleep(0.3)
    # 株探(会社予想ベース=SBI等と一致)で PER・PBR・業種 を1リクエストで取得。
    kb = fetch_kabutan(code)
    per, pbr, sector = kb.get("per"), kb.get("pbr"), kb.get("sector")
    # PER/PBR が取れない分だけ Yahoo日本 → yfinance の順で補完する。
    if per is None or pbr is None:
        time.sleep(0.3)
        y = fetch_per_pbr_yahoo(code)
        if per is None:
            per = y.get("per")
        if pbr is None:
            pbr = y.get("pbr")
    if per is None or pbr is None:
        time.sleep(0.3)
        fb = fetch_per_pbr_yfinance(code)
        if per is None:
            per = fb.get("per")
        if pbr is None:
            pbr = fb.get("pbr")
    # 業種が取れなければ Yahoo日本で補完。
    if not sector:
        time.sleep(0.3)
        sector = fetch_sector_yahoo(code)
    # 銘柄名がIRバンクで取れない(海外サーバー等)場合は株探の名前で補完。
    if not name:
        name = kb.get("name")

    change = None
    change_pct = None
    if price.get("close") is not None and price.get("prev_close"):
        change = price["close"] - price["prev_close"]
        if price["prev_close"] > 0:
            change_pct = change / price["prev_close"] * 100

    return {
        "code": code,
        "name": name,
        "sector": sector,
        "close": price.get("close"),
        "prev_close": price.get("prev_close"),
        "change": change,
        "change_pct": change_pct,
        "per": per,
        "pbr": pbr,
        "yield_pct": kb.get("yield_pct"),  # 株探の予想配当利回り(海外フォールバック用)
        "as_of": price.get("as_of") or datetime.now().strftime("%Y-%m-%d"),
    }
