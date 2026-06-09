"""高配当株 買い時チェッカー (Streamlit アプリ)

使い方:
    streamlit run app.py

銘柄コードを入力すると、株価チャート・配当推移・利回りバンド・買い時スコアを表示する。
既存の price_fetcher / dividend_fetcher をそのまま利用する。
"""
from __future__ import annotations

import socket

# 通信が無反応でも最大30秒で諦める(無限ハング防止)
socket.setdefaulttimeout(30)

import os
import re

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials

from price_fetcher import get_price_info, fetch_kabutan_dividends, fetch_kabutan_financials
from dividend_fetcher import fetch_irbank_dividend, get_dividends_for_years

st.set_page_config(page_title="高配当株 買い時チェッカー", page_icon="📈", layout="wide")


# ---------- データ取得 (1時間キャッシュ) ----------
@st.cache_data(ttl=3600, show_spinner=False)
def load_info(code: str) -> dict:
    return get_price_info(code)


@st.cache_data(ttl=3600, show_spinner=False)
def load_manual_max() -> dict:
    """ウォッチリストの手入力「5年最大利回り(%)」(J列,予想) を {コード: 最大値} で返す。

    スカウター由来の手入力を優先したい時に使う。読めなければ空dict。
    """
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets",
                  "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
        gc = gspread.authorize(creds)
        rows = gc.open("高配当株管理").worksheet("ウォッチリスト").get("A3:J300")
    except Exception:
        return {}
    out = {}
    for r in rows:
        code = (r[0] if len(r) > 0 else "").strip().upper()
        jval = r[9] if len(r) > 9 else ""
        if code and str(jval).strip():
            try:
                out[code] = float(str(jval).replace("%", ""))
            except ValueError:
                pass
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def load_financials(code: str) -> list:
    return fetch_kabutan_financials(code)


def analyze_financials(fins: list):
    """配当性向・今期増益率・減配リスクの簡易判定を返す。"""
    if not fins:
        return None
    fc = next((r for r in reversed(fins) if r["is_forecast"]), None) or fins[-1]
    actuals = [r for r in fins if not r["is_forecast"]]
    prev = actuals[-1] if actuals else None
    eps, div, net = fc.get("eps"), fc.get("dividend"), fc.get("net_profit")
    payout = (div / eps * 100) if (div and eps and eps > 0) else None
    growth = None
    if prev and prev.get("net_profit") and net is not None and prev["net_profit"] != 0:
        growth = (net - prev["net_profit"]) / abs(prev["net_profit"]) * 100

    score = 0
    reasons = []
    if payout is not None:
        if payout > 100:
            score += 3; reasons.append(f"配当性向{payout:.0f}%＝利益を超えて配当")
        elif payout >= 80:
            score += 2; reasons.append(f"配当性向{payout:.0f}%と高め(余裕小)")
        elif payout >= 60:
            score += 1; reasons.append(f"配当性向{payout:.0f}%とやや高め")
        elif payout < 50:
            score -= 1
    if eps is not None and eps < 0:
        score += 3; reasons.append("今期は赤字予想")
    elif growth is not None:
        if growth <= -30:
            score += 2; reasons.append(f"今期大幅減益予想({growth:+.0f}%)")
        elif growth < 0:
            score += 1; reasons.append(f"今期減益予想({growth:+.0f}%)")
        elif growth >= 5:
            score -= 1; reasons.append(f"今期増益予想({growth:+.0f}%)")

    if score >= 3:
        verdict, vcolor = "⚠️ 減配リスク高め", "#e53935"
    elif score == 2:
        verdict, vcolor = "やや注意", "#fb8c00"
    elif score <= 0:
        verdict, vcolor = "減配リスク低", "#43a047"
    else:
        verdict, vcolor = "中立", "#fbc02d"
    if not reasons:
        reasons.append("配当性向に余裕があり、業績も安定")
    return {"payout": payout, "growth": growth, "verdict": verdict, "vcolor": vcolor,
            "reasons": reasons, "fc_year": fc.get("year"), "fins": fins}


@st.cache_data(ttl=3600, show_spinner=False)
def load_dividends(code: str) -> dict:
    """年ごとの年間配当 {year: annual} と最新配当を返す。

    過去の推移は「配当金の状況」(多年分)、直近の予想/実績はウォッチリストと
    同じ「一株配当」セクションで上書きして数字を揃える。
    """
    data = fetch_irbank_dividend(code)
    by_year = {p["year"]: p["annual"] for p in data.get("periods", []) if p.get("annual")}
    source = "irbank" if by_year else None

    if by_year:
        # 直近年(2025〜2027)はウォッチリストと同じソースで上書き(整合性)
        fy = get_dividends_for_years(code)
        for p in fy.get("periods_out", []):
            if p.get("annual") is not None:
                m = re.match(r"(\d{4})年", p["label"])
                if m:
                    by_year[int(m.group(1))] = p["annual"]
    else:
        # フォールバック: IRバンクが使えない(海外サーバー等)→株探の業績ページから年間配当
        kb_div = fetch_kabutan_dividends(code)
        if kb_div:
            by_year = kb_div
            source = "kabutan"

    latest_year = max(by_year) if by_year else None
    latest_div = by_year.get(latest_year) if latest_year else None
    return {"by_year": by_year, "latest_year": latest_year, "latest_div": latest_div, "source": source}


@st.cache_data(ttl=3600, show_spinner=False)
def load_history(code: str, period: str = "5y") -> pd.DataFrame | None:
    t = yf.Ticker(f"{code}.T")
    # auto_adjust=False で実際の終値を使う(配当調整後だと過去価格が安く出て利回りが過大になる)
    h = t.history(period=period, auto_adjust=False)
    if h is None or h.empty:
        return None
    df = h[["Close"]].copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


# ---------- 計算 ----------
def yield_series(hist: pd.DataFrame, by_year: dict) -> pd.Series | None:
    """各日の配当利回り(%) = その年の年間配当 ÷ 終値。"""
    if hist is None or hist.empty or not by_year:
        return None
    years_sorted = sorted(by_year)

    def div_for(year: int):
        # その年の配当。無ければ直近の過去年で代用。
        if year in by_year:
            return by_year[year]
        prior = [y for y in years_sorted if y <= year]
        return by_year[prior[-1]] if prior else None

    divs = hist.index.to_series().dt.year.map(div_for)
    ys = (divs.values / hist["Close"].values) * 100
    s = pd.Series(ys, index=hist.index).dropna()
    return s if not s.empty else None


def clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def lerp_score(value, full, zero):
    """value が full なら1.0、zero なら0.0 になる線形スコア(0..1)。"""
    if value is None:
        return None
    if full == zero:
        return 0.5
    return clip01((value - zero) / (full - zero))


def compute_score(info: dict, yld: pd.Series | None, current_yield, by_year: dict,
                  hist: pd.DataFrame | None, manual_max=None):
    """買い時スコア(0..100)と内訳を返す。manual_max があれば最大値に優先採用。"""
    factors = []  # (ラベル, 重み, 0..1スコア or None, 補足)

    # 1. 利回り割安度 (40%): 過去5年レンジの中で今の利回りが高いほど割安
    s_yield = None
    band = None
    max_src = "自動計算"
    if yld is not None and current_yield is not None:
        ymin, ymean, ymax = float(yld.min()), float(yld.mean()), float(yld.max())
        if manual_max is not None:
            ymax = manual_max  # 手入力(スカウター)の最大値を優先
            max_src = "手入力(スカウター)"
            ymin = min(ymin, ymax)  # 念のため最小≦最大を保つ
        band = (ymin, ymean, ymax)
        if ymax > ymin:
            s_yield = clip01((current_yield - ymin) / (ymax - ymin))
        else:
            s_yield = 0.5
    note = f"過去5年レンジ内の位置 / 最大値:{max_src}"
    factors.append(("利回りの割安度", 0.35, s_yield, note))

    # 2. 株主還元 (15%): 連続増配・非減配の年数。還元姿勢の代用指標
    inc = nondec = 0
    yrs_all = sorted(by_year)
    vals = [by_year[y] for y in yrs_all]
    for i in range(len(vals) - 1, 0, -1):
        if vals[i] > vals[i - 1]:
            inc += 1
        else:
            break
    for i in range(len(vals) - 1, 0, -1):
        if vals[i] >= vals[i - 1]:
            nondec += 1
        else:
            break
    # 非減配(減配なし)を土台70%、連続増配を上乗せ30%。増配が止まっても急落しない。
    s_return = None
    if vals:
        s_return = clip01(0.7 * min(nondec, 12) / 12 + 0.3 * min(inc, 10) / 10)
    factors.append(("株主還元(連続増配)", 0.15, s_return, f"連続増配{inc}年 / 非減配{nondec}年"))

    # 3. PER (18%): 8以下で満点, 25以上で0
    per = info.get("per")
    s_per = None
    if per is not None and per > 0:
        s_per = lerp_score(per, full=8, zero=25)
    factors.append(("PERの低さ", 0.18, s_per, "8以下=満点 / 25以上=0"))

    # 4. PBR (12%): 0.8以下で満点, 2.5以上で0
    pbr = info.get("pbr")
    s_pbr = None
    if pbr is not None and pbr > 0:
        s_pbr = lerp_score(pbr, full=0.8, zero=2.5)
    factors.append(("PBRの低さ", 0.12, s_pbr, "0.8以下=満点 / 2.5以上=0"))

    # 5. 増配傾向 (10%): 直近の平均増配率。+5%以上で満点, -2%以下で0
    avg_growth = None
    yrs = sorted(by_year)
    gs = []
    for y in yrs:
        if (y - 1) in by_year and by_year[y - 1] > 0:
            gs.append((by_year[y] - by_year[y - 1]) / by_year[y - 1] * 100)
    if gs:
        avg_growth = sum(gs[-5:]) / len(gs[-5:])
    s_growth = lerp_score(avg_growth, full=5, zero=-2) if avg_growth is not None else None
    factors.append(("増配傾向", 0.10, s_growth, "平均増配率 +5%以上=満点"))

    # 6. 株価位置 (10%): 直近1年レンジで安いほど良い(押し目)
    s_pos = None
    if hist is not None and not hist.empty:
        last1y = hist[hist.index >= (hist.index.max() - pd.Timedelta(days=365))]["Close"]
        if not last1y.empty:
            lo, hi = float(last1y.min()), float(last1y.max())
            cur = float(last1y.iloc[-1])
            if hi > lo:
                s_pos = 1 - (cur - lo) / (hi - lo)  # 安いほど高得点
    factors.append(("株価位置(押し目度)", 0.10, s_pos, "52週レンジで下=高得点"))

    # 合計: 取得できた要素だけで重みを再正規化
    avail = [(w, s) for _, w, s, _ in factors if s is not None]
    if avail:
        total_w = sum(w for w, _ in avail)
        score = sum(w * s for w, s in avail) / total_w * 100
    else:
        score = None
    return score, factors, band, avg_growth


def score_label(score):
    if score is None:
        return "判定不可", "gray"
    if score >= 75:
        return "強い買い時", "#2e7d32"
    if score >= 60:
        return "買い時", "#43a047"
    if score >= 45:
        return "中立", "#fbc02d"
    if score >= 30:
        return "やや割高", "#fb8c00"
    return "割高ぎみ", "#e53935"


# ---------- UI ----------
st.title("📈 高配当株 買い時チェッカー")

code = st.text_input("銘柄コードを入力", value="", placeholder="例: 8306").strip().upper()

if not code:
    st.info("4桁の銘柄コードを入力してください（例: 8306 = 三菱UFJ FG）。")
    st.stop()

with st.spinner(f"{code} のデータを取得中..."):
    try:
        info = load_info(code)
        div = load_dividends(code)
        hist = load_history(code)
    except Exception as e:
        st.error(f"取得に失敗しました: {e}")
        st.stop()

name = info.get("name") or "(銘柄名取得失敗)"
sector = info.get("sector") or ""
close = info.get("close")
by_year = div["by_year"]
latest_div = div["latest_div"]
# IRバンク由来ならその配当で利回り算出(ウォッチリストと一致)。
# 海外フォールバック(yfinance配当は今期が不完全)の場合は株探の予想利回りを使う。
if div.get("source") == "irbank" and latest_div and close:
    current_yield = latest_div / close * 100
else:
    current_yield = info.get("yield_pct")
    if current_yield is None and latest_div and close:
        current_yield = latest_div / close * 100

yld = yield_series(hist, by_year)
manual_max = load_manual_max().get(code)  # ウォッチリスト手入力(スカウター)の最大値があれば優先
score, factors, band, avg_growth = compute_score(info, yld, current_yield, by_year, hist, manual_max)
label, color = score_label(score)

# --- ヘッダー ---
st.header(f"{name}　{f'({sector})' if sector else ''}")
c1, c2, c3, c4 = st.columns(4)
change = info.get("change")
change_pct = info.get("change_pct")
delta = f"{change:+,.1f}円 ({change_pct:+.2f}%)" if (change is not None and change_pct is not None) else None
c1.metric("終値", f"{close:,.0f}円" if close else "—", delta)
c2.metric("PER", f"{info['per']:.1f}倍" if info.get("per") else "—")
c3.metric("PBR", f"{info['pbr']:.2f}倍" if info.get("pbr") else "—")
c4.metric("予想利回り", f"{current_yield:.2f}%" if current_yield else "—")

# --- 買い時スコア ---
st.subheader("買い時スコア")
sc1, sc2 = st.columns([1, 1])
with sc1:
    gauge = go.Figure(go.Indicator(
        mode="gauge+number",
        value=score if score is not None else 0,
        number={"suffix": " / 100"},
        gauge={
            "axis": {"range": [0, 100]},
            "bar": {"color": color},
            "steps": [
                {"range": [0, 30], "color": "#ffebee"},
                {"range": [30, 45], "color": "#fff3e0"},
                {"range": [45, 60], "color": "#fffde7"},
                {"range": [60, 75], "color": "#f1f8e9"},
                {"range": [75, 100], "color": "#e8f5e9"},
            ],
        },
    ))
    gauge.update_layout(height=250, margin=dict(l=20, r=20, t=30, b=10))
    st.plotly_chart(gauge, use_container_width=True)
    st.markdown(f"### <span style='color:{color}'>{label}</span>", unsafe_allow_html=True)

with sc2:
    st.caption("スコアの内訳（取得できた項目だけで計算）")
    for lbl, w, s, note in factors:
        pct = f"{s*100:.0f}点" if s is not None else "データなし"
        st.write(f"**{lbl}** (重み{int(w*100)}%): {pct}　_{note}_")
        st.progress(s if s is not None else 0.0)

# --- チャート ---
g1, g2 = st.columns(2)
with g1:
    st.subheader("株価チャート (5年)")
    if hist is not None:
        fig = go.Figure(go.Scatter(x=hist.index, y=hist["Close"], mode="lines", line=dict(color="#1565c0")))
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="終値(円)")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.warning("株価履歴を取得できませんでした。")

with g2:
    st.subheader("配当推移 (1株あたり年間)")
    if by_year:
        yrs = sorted(by_year)
        vals = [by_year[y] for y in yrs]
        latest_year = div["latest_year"]
        colors = ["#90a4ae" if y != latest_year else "#43a047" for y in yrs]
        fig = go.Figure(go.Bar(x=[f"{y}年" for y in yrs], y=vals, marker_color=colors,
                               text=[f"{v:.0f}" for v in vals], textposition="outside"))
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="配当(円)")
        st.plotly_chart(fig, use_container_width=True)
        if avg_growth is not None:
            st.caption(f"直近の平均増配率: {avg_growth:+.1f}% / 年")
    else:
        st.warning("配当データを取得できませんでした。")

# --- 利回りバンド ---
st.subheader("利回りバンド (過去5年)")
if band and current_yield is not None:
    ymin, ymean, ymax = band
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=[ymin, ymax], y=[0, 0], mode="lines",
                             line=dict(color="#cfd8dc", width=14), showlegend=False))
    for val, txt, col in [(ymin, f"最小 {ymin:.1f}%", "#90a4ae"),
                          (ymean, f"平均 {ymean:.1f}%", "#546e7a"),
                          (ymax, f"最大 {ymax:.1f}%", "#90a4ae")]:
        fig.add_trace(go.Scatter(x=[val], y=[0], mode="markers+text", text=[txt],
                                 textposition="top center", marker=dict(size=10, color=col), showlegend=False))
    fig.add_trace(go.Scatter(x=[current_yield], y=[0], mode="markers+text", text=[f"現在 {current_yield:.1f}%"],
                             textposition="bottom center", marker=dict(size=18, color="#e53935", symbol="diamond"),
                             showlegend=False))
    fig.update_layout(height=180, margin=dict(l=10, r=10, t=30, b=10),
                      yaxis=dict(visible=False, range=[-1, 1]), xaxis_title="配当利回り(%)")
    st.plotly_chart(fig, use_container_width=True)
    if manual_max is not None:
        st.caption(f"最大値はウォッチリスト手入力(スカウター)の {manual_max:.2f}% を使用。平均・最小は自動計算。")
    else:
        st.caption("最大・平均・最小すべて自動計算(手入力が無いため)。")
    if current_yield >= ymax * 0.98:
        st.success("過去5年で見て利回りが高い水準＝歴史的な割安ゾーンです。")
    elif current_yield <= ymin * 1.02:
        st.warning("過去5年で見て利回りが低い水準＝割高ぎみです。")
else:
    st.info("利回りバンドの計算に必要なデータが揃いませんでした。")

# --- 配当の余裕・減配リスク ---
st.subheader("配当の余裕・減配リスク")
fin = analyze_financials(load_financials(code))
if fin:
    rc1, rc2 = st.columns(2)
    rc1.metric("配当性向(予想)", f"{fin['payout']:.0f}%" if fin["payout"] is not None else "—")
    rc2.markdown(f"### <span style='color:{fin['vcolor']}'>{fin['verdict']}</span>", unsafe_allow_html=True)
    if fin["payout"] is not None:
        p = fin["payout"]
        health = ("余裕大" if p < 30 else "健全" if p < 50 else "やや高め" if p < 70
                  else "高い(余裕小)" if p <= 100 else "利益超え(危険)")
        st.caption(f"配当性向 {p:.0f}% ＝ {health}（利益のうち配当に回す割合。低いほど増配余地・減配耐性あり）")
    st.write("判定理由: " + " / ".join(fin["reasons"]))
else:
    st.info("業績データ(株探)が取得できませんでした。")

# --- AIコメント(成長期待・減配リスク) ---
st.subheader("AIコメント（成長期待・減配リスク）")
try:
    api_key = st.secrets["ANTHROPIC_API_KEY"]
except Exception:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
if not api_key:
    st.info("AIコメントを使うには Anthropic APIキーの設定が必要です（未設定）。")
elif st.button("🤖 AIコメントを生成"):
    with st.spinner("AIが分析中..."):
        try:
            import anthropic
            fins = (fin or {}).get("fins", [])
            rows = "\n".join(
                f"  {r['year']}{'(予想)' if r['is_forecast'] else ''}: "
                f"EPS={r['eps']} 配当={r['dividend']} 最終益={r['net_profit']}"
                for r in fins)
            summary = (
                f"銘柄: {name} ({sector})\n"
                f"株価: {close}円 / PER: {info.get('per')} / PBR: {info.get('pbr')} / "
                f"予想利回り: {current_yield}%\n"
                f"配当性向(予想): {fin.get('payout') if fin else None}%\n"
                f"業績推移(売上ではなくEPS・配当・最終益、百万円):\n{rows}")
            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=450,
                system=[{
                    "type": "text",
                    "text": ("あなたは日本株の配当分析アシスタント。与えられた業績データだけを根拠に、"
                             "①事業・利益の成長期待、②どうなると減配しそうか(減配リスク)、を"
                             "日本語で簡潔に(各2〜3文)述べる。データにない事実は推測せず断定しない。"
                             "最後に必ず『※これはデータに基づく機械的な分析で、投資助言ではありません』と添える。"),
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": summary}],
            )
            st.write(msg.content[0].text)
        except Exception as e:
            st.error(f"AIコメント生成に失敗しました: {e}")
st.caption("※AIコメントはデータに基づく推測で、投資助言ではありません。")

st.caption(f"取得日: {info.get('as_of')}　データソース: yfinance / 株探 / IRバンク　※投資判断は自己責任で。")
