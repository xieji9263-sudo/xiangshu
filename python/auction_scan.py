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


def compute(df: pd.DataFrame) -> pd.DataFrame:
    """对 池∩今日实时行情 的合并帧计算各竞价指标与两级命中, 返回带条件列的表。"""
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
    out["情绪值"] = out["竞价换手%"] * out["竞价量比"]

    me = a["meihua"]
    m_cond = pd.Series(True, index=out.index)
    m_cond &= out["竞价涨幅%"].between(*me["gap"])
    # 竞价量/昨日量 缺失(接口限制)时不因缺数据误杀, 但会在表格里标注, 交由通达信复核
    m_cond &= (out["竞价量比"] >= me["vol_ratio"]) | out["竞价量缺失"] | out["昨日量缺失"]
    m_cond &= out["流通市值"] < me["float_mktcap_lt"]
    out["梅条件"] = m_cond

    ta = a["taizi"]
    # 昨日涨幅(涨停池给的是涨停日涨幅>=涨停幅, 视为强势); 无法拿昨日涨幅时仅用池内股(涨停本身即强势)
    t_cond = pd.Series(True, index=out.index)
    t_cond &= out["竞价涨幅%"].between(*ta["gap"])
    t_cond &= (out["竞价量比"] >= ta["vol_ratio"]) | out["竞价量缺失"] | out["昨日量缺失"]
    t_cond &= out["流通市值"] < ta["float_mktcap_lt"]
    out["太条件"] = t_cond

    out["命中"] = out["梅条件"] | out["太条件"]
    return out


def main():
    ap = argparse.ArgumentParser(description="9:25-9:30 竞价扫描(一进二)")
    ap.add_argument("--pool-date", default=None, help="涨停池日期 YYYYMMDD(默认用 latest)")
    ap.add_argument("--force", action="store_true", help="时段外强制运行")
    args = ap.parse_args()

    need_akshare()
    w0, w1 = config.AUCTION["window"]
    now_hm = common.hhmm_now()
    if not (w0 <= now_hm <= w1) and not args.force:
        print(f"[提示] 当前 {now_hm}, 建议窗口 {w0}-{w1}。之后运行会混入盘中量, 仅供研究。")
        print("       如确认要跑请加 --force")
        raise SystemExit(0)

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

    res = compute(merged).sort_values("情绪值", ascending=False)

    # 情绪值≥阈值 且 非竞价量缺失的池内股 → 关注池
    hot = res[res["情绪值"] >= config.AUCTION["emotion_threshold"]]
    meihua = res[res["梅条件"]]
    taizi = res[res["太条件"] & ~res["梅条件"]]

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
        title = f"竞价扫描 {dt.datetime.now():%m-%d %H:%M} 梅{len(meihua)}/太{len(taizi)}/热{len(hot)}"
        send_text(title, auction_digest(res, meihua, taizi, hot), attach_paths=[out_path])
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
