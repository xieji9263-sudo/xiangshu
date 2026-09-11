# -*- coding: utf-8 -*-
"""
auction_scan —— 9:25-9:30 竞价扫描（对应通达信 01/02 号指标）
====================================================
输入: 昨日涨停池(output/pool/limitup_pool_latest.csv, 由 build_limitup_pool 生成)
     + 东财全市场实时行情(ak.stock_zh_a_spot_em, 免费)
输出: output/auction/auction_YYYYMMDD_HHMM.csv
      梅(梅花剑)/太(太子剑)两级命中表 + 情绪值降序全表 + 人工复核清单

用法:
    python auction_scan.py                     # 9:25 后运行, 结果更贴近竞价
    python auction_scan.py --force             # 时段外(如9:31后)运行, 结果会混入盘中量, 仅供研究

口径(与 config.AUCTION 及通达信公式一致):
    竞价涨幅%   = (今开/昨收-1)*100       # 今开=当日竞价价, 全天不变
    竞价量(手)  = 东财实时"成交量"        # 仅 9:25-9:30 窗口内它等于竞价量
    竞价量比(倍)= 竞价量 / 昨日全天量     # 昨日量来自涨停池补量列"昨日量_手"
    竞价换手%   = 东财实时"换手率"        # 窗口内即竞价换手
    情绪值      = 竞价换手 × 竞价量比     # 关注 >= config.AUCTION.emotion_threshold

重要局限(免费接口):
    - 东财行情在 9:25-9:30 是否推送"竞价量"取决于其更新节奏; 若"竞价量"列为0/空,
      本脚本会标注"竞价量缺失"并跳过量比类条件 —— 此时请以通达信 .401 排序为准。
    - 竞价涨幅用"今开"计算, 该口径与通达信公式一致且收盘前不变。
仅供学习研究, 不构成投资建议。
"""
import argparse
import datetime as dt
import os

import pandas as pd

import config
import common
from common import need_akshare, fetch_retry, save_csv, col, should_exclude, is_st_or_delist
from notify import send_text, render_table


def _fmt(v, spec="{:.1f}"):
    try:
        f = float(v)
    except Exception:  # noqa: BLE001
        return str(v)
    if f != f:
        return "-"
    return spec.format(f)


def auction_digest(res, meihua, taizi, hot) -> str:
    """生成推送正文: 分节+编号, 一行主信息一行指标, 手机阅读友好。"""
    t = res.copy()
    t["市值亿"] = (t["流通市值"] / 1e8).round(0)
    lines = [f"昨日涨停池 {len(res)} 只 · 今日竞价扫描"]

    def group(title, sub, cap=6, show_emotion=False):
        if sub.empty:
            return
        lines.append("")
        lines.append(f"◆ {title}  ({len(sub)} 只)")
        for i, (_, r) in enumerate(t.loc[sub.index].head(cap).iterrows(), 1):
            tail = [f"现价{_fmt(r['最新价'], '{:.2f}')}",
                    f"竞价{_fmt(r['竞价涨幅%'], '{:.1f}')}%",
                    f"量比{_fmt(r['竞价量比'], '{:.1f}')}",
                    f"换手{_fmt(r['竞价换手%'], '{:.2f}')}%",
                    f"市值{_fmt(r['市值亿'], '{:.0f}')}亿"]
            if show_emotion:
                tail.append(f"情绪{_fmt(r['情绪值'], '{:.0f}')}")
            if bool(r.get("竞价量缺失")) or bool(r.get("昨日量缺失")):
                tail.append("⚠量缺·看通达信")
            star = "★ " if i == 1 else ""
            lines.append(f"{star}**{i}. {r['名称']}({r['代码']})**")
            lines.append("   " + " | ".join(tail))

    group("梅花剑命中(优先)", meihua, show_emotion=True)
    group("太子剑命中(备选)", taizi, show_emotion=True)
    others = hot[~hot.index.isin(set(meihua.index) | set(taizi.index))].head(6)
    group("情绪≥10·其余", others, show_emotion=True)

    if not (len(meihua) or len(taizi) or len(others)):
        lines.append("")
        lines.append("今日无命中: 竞价情绪降温或无合适标的 —— 空仓也是一种操作。")
    lines.append("")
    lines.append("→ 9:30后人工确认买点(放量拉升且均价上行); 跌破竞价价放弃。非买入指令。")
    return "\n".join(lines)


def load_latest_pool() -> pd.DataFrame:
    p = os.path.join(config.POOL["out_dir"], "limitup_pool_latest.csv")
    if not os.path.exists(p):
        print(f"[错误] 找不到 {p}\n       请先在收盘后运行: python build_limitup_pool.py")
        raise SystemExit(1)
    pool = common.load_csv(p)
    pool["代码"] = pool["代码"].astype(str).str.zfill(6)
    return pool


def spot_snapshot():
    return common.get_spot()


def compute(df: pd.DataFrame, open_mode: bool = False) -> pd.DataFrame:
    """
    对 池∩今日实时行情 的合并帧计算指标。
    open_mode=False(9:25-9:30): 竞价口径 —— 情绪值 = 竞价换手% × 竞价量比
    open_mode=True (9:30后):   开盘口径 —— 情绪值 = 换手% × 官方量比, 并给出涨停价/封板状态
    """
    a = config.AUCTION
    out = df.copy()

    if "昨日量_手" in out.columns:
        out["昨日量_手"] = pd.to_numeric(out["昨日量_手"], errors="coerce")
    else:
        out["昨日量_手"] = float("nan")   # 池未补量(接口/配置原因), 竞价量比记为空并标注
    out["昨日量缺失"] = out["昨日量_手"].isna()

    out["竞价涨幅%"] = (out["今开"] / out["昨收"] - 1) * 100
    out["竞价量_手"] = out["成交量"]
    out["竞价量缺失"] = ~(out["竞价量_手"] > 0)      # 接口未推送竞价量时 True
    out["竞价量比"] = out["竞价量_手"] / out["昨日量_手"].replace(0, float("nan"))
    out["竞价换手%"] = out["换手率"]
    out["今日涨幅%"] = pd.to_numeric(out["涨跌幅"], errors="coerce")
    out["情绪值"] = out["竞价换手%"] * out["竞价量比"]

    # 涨停价与封板状态(开盘模式用)
    lims, ztp = [], []
    for _, r in out.iterrows():
        lim = common.classify_board(str(r["代码"]))["limit"]
        lims.append(lim)
        try:
            ztp.append(round(float(r["昨收"]) * (1 + lim / 100), 2))
        except Exception:  # noqa: BLE001
            ztp.append(float("nan"))
    out["_limit_pct"] = lims
    out["涨停价"] = ztp

    def _state(r):
        try:
            px, zt, hi = float(r["最新价"]), float(r["涨停价"]), float(r["最高"])
        except Exception:  # noqa: BLE001
            return ""
        if px != px or zt != zt:
            return ""
        if px >= zt - 0.01:
            return "封板"
        if hi >= zt - 0.01:
            return "炸板"
        return ""

    out["状态"] = out.apply(_state, axis=1) if open_mode else ""

    me = a["meihua"]
    m_cond = pd.Series(True, index=out.index)
    m_cond &= out["竞价涨幅%"].between(*me["gap"])
    # 竞价量/昨日量 缺失(接口限制)时不因缺数据误杀, 但会在表格里标注, 交由通达信复核
    if open_mode:
        # 开盘后"竞价量"口径失效, 用官方量比(>1)替代量能条件
        m_cond &= pd.to_numeric(out.get("量比"), errors="coerce").fillna(0) >= 1
    else:
        m_cond &= (out["竞价量比"] >= me["vol_ratio"]) | out["竞价量缺失"] | out["昨日量缺失"]
    m_cond &= out["流通市值"] < me["float_mktcap_lt"]
    out["梅条件"] = m_cond

    ta = a["taizi"]
    # 昨日涨幅(涨停池给的是涨停日涨幅>=涨停幅, 视为强势); 无法拿昨日涨幅时仅用池内股(涨停本身即强势)
    t_cond = pd.Series(True, index=out.index)
    t_cond &= out["竞价涨幅%"].between(*ta["gap"])
    if open_mode:
        t_cond &= pd.to_numeric(out.get("量比"), errors="coerce").fillna(0) >= 1
    else:
        t_cond &= (out["竞价量比"] >= ta["vol_ratio"]) | out["竞价量缺失"] | out["昨日量缺失"]
    t_cond &= out["流通市值"] < ta["float_mktcap_lt"]
    out["太条件"] = t_cond

    out["命中"] = out["梅条件"] | out["太条件"]
    return out


def load_today_zt():
    """今日涨停池(东财数据中心): 封板资金/首次封板/最后封板/炸板次数/连板数/所属行业。失败返回 None。"""
    try:
        import akshare as ak
        df = common.fetch_retry(lambda: ak.stock_zt_pool_em(date=common.today_str()),
                                retries=1, desc="今日涨停池")
        if df is None or df.empty:
            return None
        df["代码"] = df["代码"].astype(str).str.zfill(6)
        keep = [c for c in ("代码", "封板资金", "首次封板时间", "最后封板时间",
                            "炸板次数", "连板数", "所属行业", "涨停统计") if c in df.columns]
        return df[keep]
    except Exception as e:  # noqa: BLE001
        print(f"[提示] 今日涨停池获取失败({e}), 用行情自行判断封板状态")
        return None


def open_digest(res, board) -> str:
    """9:35 开盘扫描推送: 可打板候选 + 高开强势 + 全池数据。"""
    lines = []
    n_board = 0 if board is None else len(board)
    sealed = 0 if board is None else int((board["状态"] == "封板").sum())
    broken = 0 if board is None else int((board["状态"] == "炸板").sum())
    lines.append(f"昨日涨停池 {len(res)} 只 | 现涨停 {n_board} 只 (封板 {sealed} / 炸板 {broken})")

    if board is not None and len(board):
        lines.append("")
        lines.append(f"◆ 可打板候选  ({len(board)} 只, 按连板数/封单排序)")
        for i, (_, r) in enumerate(board.head(10).iterrows(), 1):
            star = "★ " if i == 1 else ""
            st = r.get("状态") or "接近涨停"
            fd = r.get("封板资金")
            try:
                fd_s = f" 封单{float(fd)/1e8:.2f}亿" if float(fd) == float(fd) and float(fd) > 0 else ""
            except Exception:  # noqa: BLE001
                fd_s = ""
            cn = r.get("_cn")
            cn_s = f" {int(cn)}板" if cn == cn and cn is not None else ""
            lines.append(f"{star}**{i}. {r['名称']}({r['代码']})** {st}{cn_s}{fd_s}")
            lines.append(f"   现价{_fmt(r['最新价'], '{:.2f}')} | 涨{_fmt(r['今日涨幅%'], '{:.2f}')}%"
                         f" | 量比{_fmt(r.get('量比'), '{:.2f}')} | 换手{_fmt(r['换手率'], '{:.2f}')}%"
                         f" | 市值{_fmt(r['流通市值'] / 1e8, '{:.0f}')}亿")
            ind = r.get("所属行业") or r.get("所属行业_今")
            if isinstance(ind, str) and ind:
                lines.append(f"   {ind}")
    else:
        lines.append("")
        lines.append("◆ 可打板候选: 暂无(池内无封板/接近涨停标的)")

    strong = res[(res["竞价涨幅%"] >= 2) & (~res["状态"].isin(["封板", "炸板"]))]
    if len(strong):
        lines.append("")
        lines.append(f"◆ 高开强势(竞价涨幅≥2%, 未涨停)  {len(strong)} 只")
        for i, (_, r) in enumerate(strong.sort_values("竞价涨幅%", ascending=False).head(8).iterrows(), 1):
            lines.append(f"{i}. {r['名称']}({r['代码']}) 现价{_fmt(r['最新价'], '{:.2f}')}"
                         f" | 竞价{_fmt(r['竞价涨幅%'], '{:.1f}')}% | 量比{_fmt(r.get('量比'), '{:.2f}')}"
                         f" | 换手{_fmt(r['换手率'], '{:.2f}')}% | 市值{_fmt(r['流通市值'] / 1e8, '{:.0f}')}亿")

    lines.append("")
    lines.append(f"◆ 全池数据 ({len(res)} 只)")
    for i, (_, r) in enumerate(res.sort_values("今日涨幅%", ascending=False).iterrows(), 1):
        st = r.get("状态") or ""
        lines.append(f"{i}. {r['名称']}({r['代码']}) 现价{_fmt(r['最新价'], '{:.2f}')}"
                     f" 涨{_fmt(r['今日涨幅%'], '{:+.2f}')}% 量比{_fmt(r.get('量比'), '{:.2f}')}"
                     f" 换手{_fmt(r['换手率'], '{:.2f}')}% 市值{_fmt(r['流通市值'] / 1e8, '{:.0f}')}亿"
                     + (f" [{st}]" if st else ""))
    lines.append("")
    lines.append("→ 打板要点: 封板看封单/首封时间与是否反复开板; 竞价高开但量能不足者易冲高回落。")
    lines.append("  非买入指令, 请人工复核压力位/公告/板块后独立决策。")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="竞价/开盘扫描(池内一进二 + 可打板候选)")
    ap.add_argument("--pool-date", default=None, help="涨停池日期 YYYYMMDD(默认用 latest)")
    ap.add_argument("--force", action="store_true", help="时段外强制运行")
    ap.add_argument("--open", action="store_true", help="开盘模式(9:30后): 可打板候选 + 全池数据")
    ap.add_argument("--auction", action="store_true", help="强制竞价口径(9:25-9:30)")
    args = ap.parse_args()

    need_akshare()
    w0, w1 = config.AUCTION["window"]
    now_hm = common.hhmm_now()
    open_mode = args.open or (now_hm >= "09:30" and not args.auction)
    if not open_mode and not (w0 <= now_hm <= w1) and not args.force:
        print(f"[提示] 当前 {now_hm}, 建议窗口 {w0}-{w1}。之后运行会混入盘中量, 仅供研究。")
        print("       如确认要跑请加 --force")
        raise SystemExit(0)
    if open_mode:
        print(f"[开盘模式] 当前 {now_hm} (9:30后): 按开盘口径计算, 输出可打板候选 + 全池数据")

    print(f"[竞价扫描] {common.now_str()}")
    pool = load_latest_pool()
    spot = spot_snapshot()
    print(f"[数据] 昨日涨停池 {len(pool)} 只; 全市场行情 {len(spot)} 只")

    # 只保留池内(昨涨停)股票做一进二; 太子剑同样以池为基础(宽松覆盖昨强势由池内高标体现)
    keep_cols = set(pool.columns) & {"代码", "名称", "所属行业", "连板数", "昨日量_手", "昨收", "涨停统计", "换手率"}
    pool_sel = pool[list(keep_cols)].rename(columns={"换手率": "昨换手率"})
    merged = spot.merge(pool_sel, on="代码", how="inner", suffixes=("", "_池"))
    # 去掉池侧与实时行情重复的列(名称/昨收等以实时行情为准)
    merged = merged[[c for c in merged.columns if not str(c).endswith("_池")]]
    if merged.empty:
        print("[结果] 涨停池股票今日全部无行情(停牌?) —— 退出")
        return

    res = compute(merged, open_mode=open_mode).sort_values("情绪值", ascending=False)

    # 情绪值≥阈值 且 非竞价量缺失的池内股 → 关注池
    hot = res[res["情绪值"] >= config.AUCTION["emotion_threshold"]]
    meihua = res[res["梅条件"]]
    taizi = res[res["太条件"] & ~res["梅条件"]]

    # 开盘模式(9:30后): 今日涨停池补充"封板资金/首封时间/炸板/连板"
    zt_today = None
    if open_mode:
        zt_today = load_today_zt()
        if zt_today is not None and len(zt_today):
            res = res.merge(zt_today, on="代码", how="left", suffixes=("", "_今"))
            print(f"[开盘模式] 今日涨停池 {len(zt_today)} 只已并入(封板资金/首封时间/连板)")
        # 可打板候选: 封板或炸板, 或今日涨幅已接近涨停
        near = res["今日涨幅%"] >= (res["_limit_pct"] - 1.2)
        board = res[near | res["状态"].isin(["封板", "炸板"])].copy()
        if "连板数_今" in board.columns:
            board["_cn"] = pd.to_numeric(board["连板数_今"], errors="coerce").fillna(
                pd.to_numeric(board.get("连板数"), errors="coerce"))
        else:
            board["_cn"] = pd.to_numeric(board.get("连板数"), errors="coerce").fillna(1)
        if "封板资金" in board.columns:
            board["_fd"] = pd.to_numeric(board["封板资金"], errors="coerce").fillna(0)
        else:
            board["_fd"] = 0
        board = board.sort_values(["_cn", "_fd"], ascending=False)
        print("\n================ 可打板候选(封板/接近涨停) ================")
        _show(board, extra=("状态", "最新价", "今日涨幅%", "量比", "换手率", "_cn", "所属行业"))
    else:
        board = None

    print("\n================ 梅花剑命中(优先) ================")
    _show(meihua)
    print("\n================ 太子剑命中(梅花剑空时看) ================")
    _show(taizi)
    print("\n================ 情绪值>=%.0f 关注池(降序) ================" % config.AUCTION["emotion_threshold"])
    _show(hot, extra=("情绪值", "竞价涨幅%", "竞价量比", "竞价换手%"))

    # 输出文件
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M")
    out_path = os.path.join(config.AUCTION["out_dir"], f"auction_{ts}.csv")
    save_csv(res, out_path)

    # 手机推送(在 config.NOTIFY 启用后自动发出)
    if config.NOTIFY.get("enable"):
        if open_mode:
            title = f"开盘扫描 {dt.datetime.now():%m-%d %H:%M} 池{len(res)}只 可打板{len(board)}只"
            body = open_digest(res, board)
        else:
            title = f"竞价扫描 {dt.datetime.now():%m-%d %H:%M} 梅{len(meihua)}/太{len(taizi)}/热{len(hot)}"
            body = auction_digest(res, meihua, taizi, hot)
        send_text(title, body, attach_paths=[out_path])
    else:
        print("[notify] 未启用推送(设置 config.NOTIFY.enable=True 后可推到手机)")

    print("\n================ 人工复核清单(机器无法替代) ================")
    for i, c in enumerate(common.human_checklist(), 1):
        print(f"  {i}. {c}")
    print("\n[纪律] 竞价命中≠买入。9:30后看5分钟分时: 快速放量拉升且均价上行再考虑; 跌破竞价价放弃。")
    print("       (命中股较多时, 优先 连板数低+市值小+所属行业为当下主线的标的)")


def _show(df: pd.DataFrame, extra=("情绪值", "竞价涨幅%", "竞价量比", "竞价换手%", "流通市值(亿)", "连板数", "所属行业")):
    if df.empty:
        print("  (空)")
        return
    t = df.copy()
    t["流通市值(亿)"] = (t["流通市值"] / 1e8).round(1)
    t["量缺失?"] = (t["竞价量缺失"] | t["昨日量缺失"]).map({True: "Y", False: ""})
    show_cols = ["代码", "名称"] + list(extra) + ["量缺失?"]
    show_cols = [c for c in dict.fromkeys(show_cols) if c in t.columns]
    with pd.option_context("display.max_rows", 200, "display.width", 200, "display.unicode.east_asian_width", True):
        print(t[show_cols].to_string(index=False))


if __name__ == "__main__":
    main()
