# -*- coding: utf-8 -*-
"""
backtest —— 尾盘"一夜持股法"历史回测（日频近似）
====================================================
为什么要有这个脚本:
    在此之前, 8 条条件的阈值全部来自框架原文, 一次都没有用历史数据验证过。
    本脚本用真实日K回放最近 N 个交易日, 统计"命中后次日到底赚不赚",
    并给出同期全市场基准做对照 —— 没有基准的胜率是没有意义的(大盘普涨时谁都赚)。

能回测的(日频可复现):
    ① 当日涨幅 ∈ (3,5)%         —— 用收盘涨幅近似 14:30 时点涨幅
    ② 量能放大                   —— 当日量 / 前5日均量 > 1（官方量比的日频代理）
    ③ 换手率 ∈ (5,10)%          —— 成交股数 / 流通股本
    ④ 流通市值 ∈ (50,200)亿      —— 用当前市值近似(见下方局限)
    ⑤ 次日卖出: +3%止盈 / -2%止损 / 否则收盘价离场

无法回测的(需要当日分钟线, 免费接口只给当天):
    阶梯式温和放量、5分钟脉冲、全天跑赢大盘、14:30后创新高、回踩不破均价
    → 本回测只覆盖"阶段1 + 量能", 真实候选会更少, 结论偏乐观, 请据此打折。

已知近似与偏差:
    1) 涨幅/换手用"当日收盘"值, 而实际决策在 14:30, 中间可能变化;
    2) 流通市值与流通股本用当前值回看历史, 历史上不满足市值区间的票会被误纳/误排;
    3) 一字板/停牌次日无法卖出, 脚本按"次日有行情即按价格成交"处理, 偏乐观;
    4) 未计交易成本(佣金+印花税约 0.13%), 请自行从期望收益中扣除。

用法:
    python backtest.py                      # 默认 60 个交易日、300 只样本
    python backtest.py --days 120 --limit 600
    python backtest.py --no-net             # 离线自检(合成数据, 验证统计逻辑)
输出: output/backtest/backtest_YYYYMMDD.csv (逐条命中) + 终端汇总
仅供学习研究, 不构成投资建议。
"""
import argparse
import datetime as dt
import os
import random
import statistics as st
import sys

import pandas as pd

import config
import common
import providers
from common import should_exclude, is_st_or_delist, save_csv

OUT_DIR = os.path.join(config.BASE_DIR, "output", "backtest")


# ------------------------------------------------------------------
# 核心统计逻辑(纯函数, 便于离线单测)
# ------------------------------------------------------------------
def next_day_pnl(day: dict, nxt: dict, take_profit: float = 3.0, stop_loss: float = -2.0) -> dict:
    """
    给定当日与次日日K, 计算次日各种口径的收益(%)与按纪律执行的收益。
    保守假设: 若次日最低已触及止损, 先按止损计(不假设先涨后跌)。
    """
    c = float(day["收盘"])
    if c <= 0:
        return None
    open_ret = (float(nxt["开盘"]) / c - 1) * 100
    high_ret = (float(nxt["最高"]) / c - 1) * 100
    low_ret = (float(nxt["最低"]) / c - 1) * 100
    close_ret = (float(nxt["收盘"]) / c - 1) * 100
    if low_ret <= stop_loss:
        pnl, how = stop_loss, "止损"
    elif high_ret >= take_profit:
        pnl, how = take_profit, "止盈"
    else:
        pnl, how = close_ret, "收盘离场"
    return {"开盘%": open_ret, "最高%": high_ret, "最低%": low_ret,
            "收盘%": close_ret, "纪律收益%": pnl, "离场方式": how}


def eval_stock(df: pd.DataFrame, float_shares: float, tail_cfg: dict,
               take_profit: float = 3.0, stop_loss: float = -2.0) -> tuple:
    """
    对单只股票的日K逐日评估。返回 (命中列表, 基准列表)。
    基准列表 = 同一股票所有交易日的次日收盘涨幅(不施加条件), 用于计算超额。
    """
    hits, base = [], []
    if df is None or len(df) < 8:
        return hits, base
    pct_lo, pct_hi = tail_cfg["pct"]
    tn_lo, tn_hi = tail_cfg["turnover"]
    vr_min = tail_cfg["vol_ratio_gt"]
    vol = df["成交量"].astype(float)
    close = df["收盘"].astype(float)

    for i in range(6, len(df) - 1):
        prev_c = float(close.iloc[i - 1])
        c = float(close.iloc[i])
        if prev_c <= 0 or c <= 0:
            continue
        nxt = df.iloc[i + 1]
        pnl = next_day_pnl(df.iloc[i], nxt, take_profit, stop_loss)
        if pnl is None:
            continue
        # 基准: 无条件样本
        base.append(pnl["收盘%"])

        pct = (c / prev_c - 1) * 100
        v5 = float(vol.iloc[i - 5:i].mean())
        vr = (float(vol.iloc[i]) / v5) if v5 > 0 else float("nan")
        # 成交量(手) -> 股: ×100; 换手% = 成交股数 / 流通股本 × 100
        turn = (float(vol.iloc[i]) * 100 / float_shares * 100) if float_shares > 0 else float("nan")
        if not (pct_lo <= pct <= pct_hi):
            continue
        if not (vr == vr and vr > vr_min):
            continue
        if not (tn_lo <= turn <= tn_hi):
            continue
        row = {"日期": df.iloc[i]["日期"], "涨幅%": round(pct, 2),
               "量能比": round(vr, 2), "换手%": round(turn, 2)}
        row.update({k: (round(v, 2) if isinstance(v, float) else v) for k, v in pnl.items()})
        hits.append(row)
    return hits, base


def sweep_pairs(hits: list, tps=(2.0, 3.0, 5.0, 8.0, 10.0),
                sls=(-1.0, -2.0, -3.0, -5.0, -8.0)) -> pd.DataFrame:
    """
    止盈/止损参数网格扫描: 同一批命中样本, 换不同 (止盈, 止损) 组合重算纪律收益。
    用来回答"到底是策略不行, 还是止盈止损设得太紧"。
    注意: 网格扫出来的最优值有过拟合风险, 必须换区间/换样本复核后再用。
    """
    rows = []
    for tp in tps:
        for sl in sls:
            pnls, n_tp, n_sl = [], 0, 0
            for h in hits:
                if h["最低%"] <= sl:
                    p, n_sl = sl, n_sl + 1
                elif h["最高%"] >= tp:
                    p, n_tp = tp, n_tp + 1
                else:
                    p = h["收盘%"]
                pnls.append(p)
            if not pnls:
                continue
            rows.append({
                "止盈%": tp, "止损%": sl,
                "均值%": round(sum(pnls) / len(pnls), 3),
                "中位%": round(st.median(pnls), 3),
                "胜率%": round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1),
                "止盈触发%": round(n_tp / len(pnls) * 100, 1),
                "止损触发%": round(n_sl / len(pnls) * 100, 1),
            })
    return pd.DataFrame(rows)


def summarize(hits: list, base: list) -> dict:
    """汇总命中样本与基准样本。"""
    def stat(xs):
        if not xs:
            return {}
        xs = sorted(xs)
        return {"n": len(xs), "均值": sum(xs) / len(xs), "中位": st.median(xs),
                "胜率": sum(1 for x in xs if x > 0) / len(xs)}
    h_close = [h["收盘%"] for h in hits]
    h_disc = [h["纪律收益%"] for h in hits]
    h_open = [h["开盘%"] for h in hits]
    s = {"命中样本": stat(h_close), "按纪律执行": stat(h_disc),
         "次日开盘": stat(h_open), "基准(全样本次日收盘)": stat(base)}
    if s["命中样本"] and s["基准(全样本次日收盘)"]:
        s["超额%"] = s["命中样本"]["均值"] - s["基准(全样本次日收盘)"]["均值"]
        s["纪律超额%"] = s["按纪律执行"]["均值"] - s["基准(全样本次日收盘)"]["均值"]
    if hits:
        tp = sum(1 for h in hits if h["离场方式"] == "止盈")
        sl = sum(1 for h in hits if h["离场方式"] == "止损")
        s["止盈触发率"] = tp / len(hits)
        s["止损触发率"] = sl / len(hits)
    return s


# ------------------------------------------------------------------
# 离线自检(不联网, 验证统计逻辑)
# ------------------------------------------------------------------
def _self_test() -> str:
    df = pd.DataFrame({
        "日期": [f"2025-01-{d:02d}" for d in range(2, 20)],
        "开盘": [10.0] * 18, "收盘": [10.0] * 18,
        "最高": [10.0] * 18, "最低": [10.0] * 18, "成交量": [1000.0] * 18,
    })
    # 构造: 第7天(索引6) 涨4%, 量放大1.5倍, 换手7%(流通股本=成交量*100/0.07)
    df.loc[6, "收盘"] = 10.4
    df.loc[6, "成交量"] = 1500.0
    # 次日(索引7)横盘在 10.4, 收益应为 0 -> 走"收盘离场"
    df.loc[7, ["开盘", "收盘", "最高", "最低"]] = 10.4
    float_shares = 1500.0 * 100 / 0.07
    cfg = {"pct": (3.0, 5.0), "turnover": (5.0, 10.0), "vol_ratio_gt": 1.0}
    hits, base = eval_stock(df, float_shares, cfg)
    assert len(hits) == 1, f"应命中 1 条, 实际 {len(hits)}"
    assert abs(hits[0]["涨幅%"] - 4.0) < 0.01, hits[0]
    assert hits[0]["离场方式"] == "收盘离场" and abs(hits[0]["收盘%"]) < 1e-6, hits[0]
    # 验证止盈: 次日最高到 +4%
    df2 = df.copy()
    df2.loc[7, "最高"] = 10.4 * 1.04
    hits2, _ = eval_stock(df2, float_shares, cfg)
    assert hits2[0]["离场方式"] == "止盈" and hits2[0]["纪律收益%"] == 3.0, hits2[0]
    # 验证止损优先: 次日最低 -3.85% 且最高 +5.8%, 应先算止损
    df3 = df.copy()
    df3.loc[7, "最低"] = 10.0
    df3.loc[7, "最高"] = 11.0
    hits3, _ = eval_stock(df3, float_shares, cfg)
    assert hits3[0]["离场方式"] == "止损" and hits3[0]["纪律收益%"] == -2.0, hits3[0]
    return f"离线逻辑通过(命中{len(hits)}条, 基准{len(base)}条, 止盈/止损判定正确)"


def report(hits: list, base: list, save_path: str = None):
    """打印汇总 + 参数扫描。base 为空时跳过基准对照。"""
    s = summarize(hits, base)
    print("\n================ 回测结果(日频近似) ================")
    for k in ("命中样本", "按纪律执行", "次日开盘", "基准(全样本次日收盘)"):
        v = s.get(k)
        if not v:
            continue
        print(f"{k:<20} 样本{v['n']:>5}  均值{v['均值']:>+7.3f}%  "
              f"中位{v['中位']:>+7.3f}%  胜率{v['胜率']:>6.1%}")
    print(f"\n止盈触发率 {s.get('止盈触发率', 0):.1%}   止损触发率 {s.get('止损触发率', 0):.1%}")
    if base:
        print(f"超额收益(策略-基准) {s.get('超额%', float('nan')):+.3f}%   "
              f"按纪律执行超额 {s.get('纪律超额%', float('nan')):+.3f}%")

    print("\n================ 怎么读这个结果 ================")
    ex = s.get("纪律超额%", 0)
    if not base:
        print("(本次未计算基准样本, 无法判断超额; 完整跑一次才有对照)")
    elif ex > 0.3:
        print("纪律超额为正且幅度可观: 阈值方向可能有效, 建议扩大样本/换区间再验一次。")
    elif ex > 0:
        print("纪律超额略为正: 幅度小于交易成本(约0.13%)或接近噪声, 不足以支撑实盘。")
    else:
        print("纪律超额为负: 这套阈值在回看区间内没有正期望, 继续用它做决策要慎重。")
    print("注意: 本回测未覆盖分钟线条件(阶梯/脉冲/跑赢大盘/创新高), 真实候选更少;")
    print("      也未计交易成本。结论请据此打折, 不构成投资建议。")

    sw = sweep_pairs(hits)
    if sw.empty:
        return
    print("\n================ 止盈/止损敏感度扫描(同批命中样本) ================")
    with pd.option_context("display.width", 200, "display.unicode.east_asian_width", True):
        print(sw.pivot(index="止损%", columns="止盈%", values="均值%").to_string())
    best = sw.sort_values("均值%", ascending=False).iloc[0]
    print(f"\n本区间内最优组合: 止盈{best['止盈%']:+.0f}% / 止损{best['止损%']:+.0f}% "
          f"→ 均值{best['均值%']:+.3f}% 胜率{best['胜率%']:.1f}%")
    if save_path:
        sw_path = save_path.replace("backtest_", "backtest_sweep_")
        save_csv(sw, sw_path)
    print("提醒: 网格最优值有过拟合风险, 换一个时间区间/换一批样本复核后再改 config。")


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="尾盘一夜持股法历史回测")
    ap.add_argument("--days", type=int, default=60, help="回看交易日数(默认60)")
    ap.add_argument("--limit", type=int, default=300, help="最多抽样股票数(默认300)")
    ap.add_argument("--seed", type=int, default=7, help="抽样随机种子")
    ap.add_argument("--no-net", action="store_true", help="离线自检(不联网)")
    ap.add_argument("--from-csv", default=None,
                    help="用已保存的命中明细重算统计与参数扫描(不联网)")
    args = ap.parse_args()

    if args.no_net:
        print("[离线自检] " + _self_test())
        return

    if args.from_csv:
        df = pd.read_csv(args.from_csv)
        need_cols = {"开盘%", "最高%", "最低%", "收盘%"}
        if not need_cols <= set(df.columns):
            print(f"[错误] 该文件缺少列 {need_cols - set(df.columns)}")
            raise SystemExit(1)
        hits = df.to_dict("records")
        print(f"[离线] 读取命中明细 {len(hits)} 条: {args.from_csv}")
        print("[提示] 该模式无全市场基准样本, 超额收益不显示; 完整对照请重跑一次。")
        report(hits, [], save_path=args.from_csv)
        return

    common.need_akshare()
    os.makedirs(OUT_DIR, exist_ok=True)
    t = config.TAIL
    print(f"[回测] 最近 {args.days} 个交易日 · 样本上限 {args.limit} 只 · {common.now_str()}")

    spot = common.get_spot()
    spot = spot.copy()
    spot["流通市值(亿)"] = pd.to_numeric(spot["流通市值"], errors="coerce") / 1e8
    m = ~spot.apply(lambda r: should_exclude(r["代码"], r["名称"], config.MARKET), axis=1)
    m &= ~spot["名称"].apply(is_st_or_delist)
    m &= spot["流通市值(亿)"].between(*(x / 1e8 for x in t["float_mktcap"]))
    cand = spot[m]
    print(f"[筛选] 符合市值与板块口径(主板/50-200亿) {len(cand)} 只")
    if cand.empty:
        print("[结果] 无候选")
        return

    codes = cand["代码"].astype(str).str.zfill(6).tolist()
    if len(codes) > args.limit:
        random.Random(args.seed).shuffle(codes)
        codes = codes[:args.limit]
        print(f"[抽样] 随机取 {len(codes)} 只(种子 {args.seed})")

    name_map = dict(zip(cand["代码"].astype(str).str.zfill(6), cand["名称"]))
    px_map = dict(zip(cand["代码"].astype(str).str.zfill(6),
                      pd.to_numeric(cand["最新价"], errors="coerce")))
    mc_map = dict(zip(cand["代码"].astype(str).str.zfill(6),
                      pd.to_numeric(cand["流通市值"], errors="coerce")))

    all_hits, all_base = [], []
    need = args.days + 10
    for i, code in enumerate(codes, 1):
        try:
            df = providers.daily_frame(code, n=max(need, 30))
        except Exception as e:  # noqa: BLE001
            print(f"  [跳过] {code}: {e}")
            continue
        if df is None or len(df) < 10:
            continue
        df = df.tail(need).reset_index(drop=True)
        px, mc = px_map.get(code), mc_map.get(code)
        float_shares = (float(mc) / float(px)) if (px and mc and px > 0) else 0.0
        hits, base = eval_stock(df, float_shares, t)
        for h in hits:
            h["代码"] = code
            h["名称"] = name_map.get(code, "")
        all_hits.extend(hits)
        all_base.extend(base)
        if i % 50 == 0:
            print(f"  ... 已完成 {i}/{len(codes)}  命中 {len(all_hits)}")

    print(f"\n[样本] 股票 {len(codes)} 只 · 命中 {len(all_hits)} 条 · 基准样本 {len(all_base)} 条")

    if not all_hits:
        print("[结果] 区间内无命中。可加大 --days 或 --limit; 也可说明该阈值在当前市场偏严。")
        return

    df_hits = pd.DataFrame(all_hits)
    cols = ["日期", "代码", "名称", "涨幅%", "量能比", "换手%",
            "开盘%", "最高%", "最低%", "收盘%", "纪律收益%", "离场方式"]
    df_hits = df_hits[[c for c in cols if c in df_hits.columns]]
    out_path = os.path.join(OUT_DIR, f"backtest_{dt.date.today():%Y%m%d}.csv")
    save_csv(df_hits.sort_values("日期"), out_path)

    report(all_hits, all_base, save_path=out_path)


if __name__ == "__main__":
    main()
