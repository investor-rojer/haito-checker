from __future__ import annotations

import re
import time

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    )
}


def _to_float(s: str) -> float | None:
    if not s:
        return None
    s = s.strip().replace(",", "")
    if s in ("-", "ー", "－", "", "–"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


_KIND_PRIORITY = {"実績": 3, "修正": 2, "予想": 1}


def _detect_value_columns(table_html: str) -> dict[str, int]:
    """thead を解析して、各列名が値セル(rt/ct class)の何番目になるかを返す。

    返り値の例:
      エディオン: {"interim": 0, "period_end": 1, "total": 2}
      イチケン  : {"interim": 0, "period_end": 1, "total": 2, "split_adj": 3}
      256A 飛島: {"period_end": 0, "total": 1}
    """
    thead_match = re.search(r"<thead>(.*?)</thead>", table_html, re.DOTALL)
    if not thead_match:
        return {}
    thead_html = thead_match.group(1)

    th_html_list = re.findall(r"<th[^>]*>(.*?)</th>", thead_html, re.DOTALL)
    cleaned = []
    for t in th_html_list:
        t = re.sub(r"<[^>]+>", "", t)
        t = re.sub(r"\s+", "", t)
        cleaned.append(t)

    try:
        start = cleaned.index("区分") + 1
    except ValueError:
        return {}

    key_map = {"中間": "interim", "期末": "period_end", "合計": "total", "分割調整": "split_adj"}
    result: dict[str, int] = {}
    value_idx = 0
    for name in cleaned[start:]:
        if name in ("備考",):
            continue
        if name in key_map:
            result[key_map[name]] = value_idx
        value_idx += 1
    return result


def fetch_irbank_dividend(code: str) -> dict:
    """IRバンクの「配当金の状況」テーブルから各決算期の配当データを返す。

    HTMLが <tr> 不整合で、各年は rowspan で 予想/修正/実績 の最大3行構造。
    `</tr>` で分割して、優先度: 実績 > 修正 > 予想 の最新値を年ごとに採用する。
    """
    url = f"https://irbank.net/{code}/dividend"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        return {"error": f"irbank fetch failed: {e}", "periods": []}

    html = r.text
    cap_idx = html.find("配当金の状況")
    if cap_idx < 0:
        return {"error": "配当テーブルが見つかりません", "periods": []}
    end_idx = html.find("</table>", cap_idx)
    table_html = html[cap_idx:end_idx if end_idx > 0 else len(html)]

    # theadを解析して「中間/期末/合計/分割調整」がそれぞれ値セル(rt/ct)の何番目かを判定する。
    # 銘柄により列構成が異なる: 例えば256A 飛島HDは「期末/合計/利回り」だけで「中間」が無い。
    col_idx = _detect_value_columns(table_html)
    has_split_col = "split_adj" in col_idx

    fragments = re.split(r"</tr>", table_html)

    by_year: dict[int, dict] = {}
    fiscal_month: int | None = None
    cur_year: int | None = None
    cur_month: int | None = None

    year_re = re.compile(r"rowspan=\"\d+\">(\d{4})年<br/?>(\d{1,2})月")
    kind_re = re.compile(r"<span class=\"co_\w+\">(予想|修正|実績)</span>")
    # 値セル: 「</td>」 か 「<br」 のどちらか早い方までを値とみなす。
    # IRバンクは利回りセル等で「5.67%<br><span>5/22</span></td>」のように発表日メモを付ける場合がある。
    td_value_re = re.compile(r"<td[^>]*class=\"(?:rt|ct)[^\"]*\"[^>]*>([^<]*)(?:</td>|<br)")

    for frag in fragments:
        ym = year_re.search(frag)
        if ym:
            cur_year = int(ym.group(1))
            cur_month = int(ym.group(2))
            if fiscal_month is None:
                fiscal_month = cur_month

        if cur_year is None:
            continue

        km = kind_re.search(frag)
        if not km:
            continue
        kind = km.group(1)

        values = td_value_re.findall(frag)
        if not values:
            continue

        def pick(key):
            i = col_idx.get(key)
            if i is None or i >= len(values):
                return None
            return _to_float(values[i])

        interim = pick("interim")
        period_end = pick("period_end")
        total = pick("total")
        split_adj = pick("split_adj")

        kind_suffix = ""
        if has_split_col and split_adj is not None:
            annual = split_adj
            if total is not None and split_adj != total:
                kind_suffix = "(分割調整)"
        else:
            annual = total

        # 中+期から合計を補完するのは「両方とも有効」な場合のみ。
        # 実績行で中間だけ確定して期末・合計が空("-")のケースでは補完せず、
        # 予想/修正にフォールバックさせる。
        if annual is None and interim is not None and period_end is not None:
            annual = interim + period_end
            kind_suffix = "(中+期計算)"

        if annual is None:
            continue

        prev = by_year.get(cur_year)
        new_priority = _KIND_PRIORITY.get(kind, 0)
        prev_priority = _KIND_PRIORITY.get(prev["kind_raw"], 0) if prev else -1
        if new_priority >= prev_priority:
            by_year[cur_year] = {
                "year": cur_year,
                "month": cur_month,
                "kind": kind + kind_suffix,
                "kind_raw": kind,
                "annual": annual,
                "interim": interim,
                "period_end": period_end,
            }

    periods = [by_year[y] for y in sorted(by_year)]
    return {
        "fiscal_month": fiscal_month,
        "periods": periods,
        "source_url": url,
    }


def fetch_per_share_dividend(code: str) -> dict:
    """IRバンクの「一株配当」セクションから年ごとの年間配当を取得する。

    `/{code}/dividend` 経由でEDINETコードを取得し、`/{edinet}/results` の
    「一株配当」dl から各年の値を抽出する。
    - 分割調整済みの統一値
    - 予想は dd 値の頭に "*" が付き、dt に「予」が含まれる
    - h2 タグは「一株配当」または注釈付き「一株配当#16」など
    """
    try:
        r1 = requests.get(f"https://irbank.net/{code}/dividend", headers=HEADERS, timeout=15)
        r1.raise_for_status()
    except Exception as e:
        return {"error": f"dividend page fetch failed: {e}", "periods": []}

    m = re.search(
        r'<meta\s+property="og:url"\s+content="https://irbank\.net/(E\d+)/',
        r1.text,
    )
    if not m:
        return {"error": "EDINETコード抽出失敗", "periods": []}
    edinet = m.group(1)

    time.sleep(0.5)
    url = f"https://irbank.net/{edinet}/results"
    try:
        r2 = requests.get(url, headers=HEADERS, timeout=15)
        r2.raise_for_status()
    except Exception as e:
        return {"error": f"results page fetch failed: {e}", "periods": []}

    soup = BeautifulSoup(r2.text, "html.parser")
    target_dl = None
    for h2 in soup.find_all("h2"):
        if "一株配当" in h2.get_text(strip=True):
            target_dl = h2.find_next("dl")
            break
    if target_dl is None:
        return {"error": "一株配当 セクションが見つかりません", "periods": []}

    periods: list[dict] = []
    fiscal_month: int | None = None

    for dt in target_dl.find_all("dt"):
        dt_text = dt.get_text(" ", strip=True)
        ym = re.match(r"(\d{4})/(\d{1,2})", dt_text)
        if not ym:
            continue
        year = int(ym.group(1))
        month = int(ym.group(2))
        if fiscal_month is None:
            fiscal_month = month

        kind = "予想" if "予" in dt_text else "実績"

        dd = dt.find_next("dd")
        if not dd:
            continue
        dd_text = dd.get_text(" ", strip=True)
        # 例: "* 96円" / "70円" / "* 125円"
        val_match = re.search(r"([\d,]+\.?\d*)\s*円", dd_text)
        if not val_match:
            continue
        try:
            annual = float(val_match.group(1).replace(",", ""))
        except ValueError:
            continue

        periods.append({
            "year": year,
            "month": month,
            "kind": kind,
            "annual": annual,
        })

    return {
        "fiscal_month": fiscal_month,
        "periods": periods,
        "source_url": url,
        "edinet": edinet,
    }


def get_dividends_for_years(code: str, target_years=(2025, 2026, 2027)) -> dict:
    """指定した「決算年」の配当データを返す。

    IRバンクの「一株配当」セクション(分割調整済み・最新予想含む)から取得する。
    """
    time.sleep(0.5)
    data = fetch_per_share_dividend(code)
    periods = data.get("periods", [])
    fiscal_month = data.get("fiscal_month")

    note_parts = []
    if data.get("error"):
        note_parts.append(data["error"])

    by_year = {p["year"]: p for p in periods}
    out = []
    for y in target_years:
        p = by_year.get(y)
        if p:
            out.append({
                "label": f"{p['year']}年{p['month']}月期",
                "annual": p["annual"],
                "kind": p["kind"],
            })
            if "計算" in p["kind"]:
                note_parts.append(f"{y}年は中間+期末から計算")
        else:
            label = f"{y}年{fiscal_month}月期" if fiscal_month else f"{y}年"
            out.append({"label": label, "annual": None, "kind": ""})
            note_parts.append(f"{y}年データなし")

    def growth(a, b):
        if a and b and a > 0:
            return (b - a) / a * 100
        return None

    g12 = growth(out[0]["annual"], out[1]["annual"])
    g23 = growth(out[1]["annual"], out[2]["annual"])

    return {
        "fiscal_month": fiscal_month,
        "periods_out": out,
        "growth_1_2": g12,
        "growth_2_3": g23,
        "source": "IRバンク" if not data.get("error") else "",
        "source_url": data.get("source_url", ""),
        "note": " / ".join(note_parts),
    }


def get_recent_dividends(code: str, today=None) -> dict:
    """後方互換: 旧名称。決算年 2025/2026/2027 を固定で返す。"""
    return get_dividends_for_years(code, target_years=(2025, 2026, 2027))


def get_growth_table(code: str, years=(2023, 2024, 2025, 2026, 2027)) -> dict:
    """各年の前年比増配率を返す。平均増配率は「データがある年の単純平均」。

    取得利回り用に最新の予想/実績配当(latest_dividend)も返す。
    優先度: years の最後の年に有効値があればそれ、なければ一つ手前、…
    """
    time.sleep(1)
    data = fetch_irbank_dividend(code)
    periods = data.get("periods", [])
    by_year = {p["year"]: p["annual"] for p in periods if p.get("annual") is not None}

    growths: dict[int, float | None] = {}
    for y in years:
        cur = by_year.get(y)
        prev = by_year.get(y - 1)
        if cur is not None and prev is not None and prev > 0:
            growths[y] = (cur - prev) / prev * 100
        else:
            growths[y] = None

    valid_growths = [g for g in growths.values() if g is not None]
    avg = sum(valid_growths) / len(valid_growths) if valid_growths else None

    latest_div = None
    latest_year = None
    for y in reversed(years):
        v = by_year.get(y)
        if v is not None and v > 0:
            latest_div = v
            latest_year = y
            break

    return {
        "growths": growths,
        "average": avg,
        "latest_dividend": latest_div,
        "latest_year": latest_year,
        "fiscal_month": data.get("fiscal_month"),
        "source_url": data.get("source_url", ""),
        "error": data.get("error"),
    }
