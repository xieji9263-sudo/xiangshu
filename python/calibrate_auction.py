# -*- coding: utf-8 -*-
"""
calibrate_auction —— 竞价量占比分布统计与阈值校准
====================================================
用途: 实证"集合竞价成交量(9:25-9:30 撮合) 占 当日全天成交量"的分布,
      用来校准 config.AUCTION 里 梅花剑/太子剑 的 vol_ratio 阈值。

背景(2026-09-11):
    原阈值 meihua.vol_ratio=0.5 / taizi.vol_ratio=0.3 是按"竞价量 >= 昨日全天量 50%/30%"
    直译的, 但实测竞价量只占全天量的 1% 上下, 该条件几乎永不命中, 整条一进二主线空转。
    本脚本用真实数据给出分位数, 阈值应设在分位点上而不是拍脑袋。

数据源: 腾讯分钟接口(首根 09:30 的累计量即竞价撮合量; 末根累计量即全天量)

用法:
    python calibrate_auction.py                 # 用最新涨停池
    python calibrate_auction.py --limit 100     # 最多取前 N 只(按池顺序)
    python calibrate_auction.py --csv 路径.csv  # 指定池文件
输出: 终端分位数表 + output/pool/auction_ratio_YYYYMMDD.csv
仅供学习研究, 不构成投资建议。
"""
import argparse
import datetime as dt
import os
import statistics as st
import sys

import pandas as pd

import config
import common
import providers


def auction_ratio(code: str):
    """返回 (竞价量手, 全天量手, 占比)。失败返回 None。

    腾讯分钟数据: 每根为 "HHMM 价格 累计量(手) 累计额(元)",
    首根 09:30 的累计量 = 集合竞价成交量(9:15-9:25 撮合, 9:30 一并显示)。
    """
    try:
        node_raw = providers._retry(
            lambda: providers._get(
                f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={providers.to_tencent_symbol(code)}"
            ).decode("utf-8", "ignore")
        )
        import json
        j = json.loads(node_raw)
        sym = providers.to_tencent_symbol(code)
        node = j["data"][sym]["data"]["data"]
        if not node:
            return None
        first = node[0].split()
        last = node[-1].split()
        a_vol = float(first[2])
        d_vol = float(last[2])
        if d_vol <= 0:
            return None
        return a_vol, d_vol, a_vol / d_vol
    except Exception as e:  # noqa: BLE001
        print(f"  [跳过] {code}: {e}")
        return None


def quantile(sorted_vals, q):
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def main():
    ap = argparse.ArgumentParser(description="竞价量占比分布与阈值校准")
    ap.add_argument("--csv", default=None, help="池文件路径(默认 output/pool/limitup_pool_latest.csv)")
    ap.add_argument("--limit", type=int, default=120, help="最多取前 N 只")
    args = ap.parse_args()

    path = args.csv or os.path.join(config.POOL["out_dir"], "limitup_pool_latest.csv")
    if not os.path.exists(path):
        print(f"[错误] 找不到池文件 {path}\n       请先运行: python build_limitup_pool.py")
        raise SystemExit(1)

    pool = common.load_csv(path)
    pool["代码"] = pool["代码"].astype(str).str.zfill(6)
    codes = pool["代码"].tolist()[:args.limit]
    print(f"[校准] 池文件 {os.path.basename(path)}  取 {len(codes)} 只")

    rows = []
    for i, c in enumerate(codes, 1):
        r = auction_ratio(c)
        if r:
            rows.append({"代码": c, "竞价量_手": r[0], "全天量_手": r[1], "竞价占比": r[2]})
        if i % 20 == 0:
            print(f"  ... 已完成 {i}/{len(codes)}")

    if not rows:
        print("[错误] 无有效样本(网络受限或接口变更)")
        raise SystemExit(1)

    df = pd.DataFrame(rows)
    if "名称" in pool.columns:
        df = df.merge(pool[["代码", "名称"]], on="代码", how="left")
    df = df.sort_values("竞价占比", ascending=False)

    out_path = os.path.join(config.POOL["out_dir"],
                            f"auction_ratio_{dt.date.today():%Y%m%d}.csv")
    common.save_csv(df, out_path)

    vals = sorted(df["竞价占比"].tolist())
    print("\n============ 竞价量 / 当日全天量 分布 ============")
    print(f"样本 {len(vals)} 只   均值 {st.mean(vals):.2%}   中位数 {st.median(vals):.2%}")
    print(f"最大 {max(vals):.2%}   最小 {min(vals):.2%}")
    print("\n分位数:")
    for q in (0.25, 0.5, 0.6, 0.75, 0.8, 0.9):
        print(f"  P{int(q*100):<3} = {quantile(vals, q):.3%}")

    print("\n============ Top10 竞价占比 ============")
    show = df.head(10).copy()
    show["竞价占比"] = (show["竞价占比"] * 100).round(2)
    with pd.option_context("display.width", 200, "display.unicode.east_asian_width", True):
        print(show.to_string(index=False))

    print("\n============ 阈值建议 ============")
    p80, p60 = quantile(vals, 0.80), quantile(vals, 0.60)
    print(f"梅花剑 vol_ratio(严格, 建议 P80) ≈ {p80:.4f}")
    print(f"太子剑 vol_ratio(宽松, 建议 P60) ≈ {p60:.4f}")
    print("把这两个数填进 config.AUCTION.meihua.vol_ratio / taizi.vol_ratio 即可。")
    print("注: 一字板/秒板样本会显著拉高分位数, 若想剔除可用 --limit 后人工核对 Top 样本。")
    print("\n另: 情绪值已改用'官方量比'口径(换手% × 行情量比列), 与本口径解耦, 见 auction_scan.compute。")


if __name__ == "__main__":
    main()
