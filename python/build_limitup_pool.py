# -*- coding: utf-8 -*-
"""
build_limitup_pool —— 盘后构建"昨日涨停池"
====================================================
用途: 为次日 9:25 竞价扫描(auction_scan)与盘前情绪报告(mood_report)提供输入：
      - 昨日(最近交易日)涨停股及其所属行业/连板数
      - 每只涨停股的"昨日全天成交量(手)"（竞价量比的分母）
数据源: 东方财富"涨停板行情"池接口（akshare: stock_zt_pool_em, 免费单次全市场）。

用法:
    python build_limitup_pool.py              # 自动取最近一个交易日
    python build_limitup_pool.py --date 20250228
说明:
    - 收盘后运行即可（东财涨停池数据当日收盘后完整）。
    - 若 zt_pool 接口不可用会报错并提示回退方案（该接口偶尔改版）。
输出: output/pool/limitup_pool_YYYYMMDD.csv
仅供学习研究, 不构成投资建议。
"""
import argparse
import datetime as dt
import os
import time

import pandas as pd

import config
import common
from common import need_akshare, fetch_retry, save_csv, should_exclude, is_st_or_delist
from notify import send_text, render_table


def pool_digest(pool: pd.DataFrame) -> str:
    """推送正文: 涨停家数 + 板块分布(编号, 含最高板)。"""
    lines = [f"昨日涨停池: {len(pool)} 只 (已剔除ST/退市/北交)"]
    if "所属行业" in pool.columns and len(pool):
        g = pool.groupby("所属行业").agg(只数=("代码", "count"), 最高连板=("连板数", "max"))
        g = g.sort_values("只数", ascending=False).head(6)
        lines.append("")
        lines.append("板块分布(只数 / 最高板):")
        for i, (ind, r) in enumerate(g.iterrows(), 1):
            star = "★ " if i == 1 else ""
            lines.append(f"{star}**{i}. {ind}**: {int(r['只数'])}只 · 最高{int(r['最高连板'])}板")
    lines.append("")
    lines.append("→ 明日 09:26 竞价扫描将基于本池自动推送候选。")
    return "\n".join(lines)


def get_zt_pool(date_str: str) -> pd.DataFrame:
    """东财涨停池。列(视版本): 代码 名称 涨跌幅 最新价 成交额 流通市值 总市值 换手率
    封板资金 首次封板时间 最后封板时间 炸板次数 涨停统计 连板数 所属行业"""
    import akshare as ak
    df = fetch_retry(lambda: ak.stock_zt_pool_em(date=date_str), desc=f"东财涨停池 {date_str}")
    if df is None or df.empty:
        raise RuntimeError(f"{date_str} 涨停池为空(非交易日或接口无数据)")
    return df


def enrich_prev_volume(pool: pd.DataFrame, date_str: str) -> pd.DataFrame:
    """
    涨停池不直接给"成交量(手)"。对每只涨停股用 腾讯日K 取 date_str 那天的
    成交量(手) 与 收盘价, 作为次日竞价量比的分母。涨停股通常几十只, 约1-3分钟。
    """
    import providers
    n_ok, n_fail = 0, 0
    vols, closes = [], []
    for _, r in pool.iterrows():
        code = str(r["代码"]).zfill(6)
        try:
            vol, close = providers.prev_day_volume(code, date_str)
            if vol is None:
                raise ValueError(f"{date_str} 无日K数据")
            vols.append(float(vol))
            closes.append(float(close) if close is not None else float("nan"))
            n_ok += 1
        except Exception as e:  # noqa: BLE001
            vols.append(float("nan"))
            closes.append(float("nan"))
            n_fail += 1
            print(f"  [警告] {code} 昨量补取失败: {e}")
        time.sleep(0.12)
    pool["昨日量_手"] = vols
    pool["昨收"] = closes
    print(f"[补量] 成功 {n_ok} 只, 失败/缺失 {n_fail} 只(缺失的竞价量比记为空)")
    return pool


def main():
    ap = argparse.ArgumentParser(description="盘后构建昨日涨停池")
    ap.add_argument("--date", default=config.POOL.get("date"),
                    help="涨停日 YYYYMMDD; 默认最近交易日")
    args = ap.parse_args()

    need_akshare()
    date_str = args.date or common.last_trading_date_str()
    # 用户习惯"昨日池"= 最近交易日; 若今天为交易日且现在已收盘, 最近交易日=今天。
    # 这里语义明确为: 传入 date 即"涨停发生日", 供次日竞价扫描用, 由使用者决定。
    print(f"[涨停池] 目标日期: {date_str}  {common.now_str()}")

    pool = get_zt_pool(date_str)
    # 清洗: 代码规整 + 剔除 ST/退市
    n_raw = len(pool)
    pool["代码"] = pool["代码"].astype(str).str.zfill(6)
    pool = pool[~pool.apply(lambda r: should_exclude(r["代码"], r.get("名称", ""), config.MARKET), axis=1)]
    pool = pool[~pool["名称"].apply(is_st_or_delist)].reset_index(drop=True)
    print(f"[涨停池] 剔除ST/退市/北交后 {len(pool)} 只(原始 {n_raw} 只)")

    # 列规整: 保证下游脚本依赖的列存在(不同 akshare 版本 zt_pool 列名可能缺)
    for _c, _default in (("连板数", 1), ("所属行业", "未知"), ("涨停统计", ""), ("换手率", float("nan"))):
        if _c not in pool.columns:
            pool[_c] = _default
    if "代码" in pool.columns and "名称" not in pool.columns:
        pool["名称"] = ""

    if config.POOL.get("enrich_prev_volume", True):
        pool = enrich_prev_volume(pool, date_str)

    out_path = os.path.join(config.POOL["out_dir"], f"limitup_pool_{date_str}.csv")
    save_csv(pool, out_path)
    # 同时生成"最新"软链接式副本, 供其它脚本免传参读取
    latest = os.path.join(config.POOL["out_dir"], "limitup_pool_latest.csv")
    pool.to_csv(latest, index=False, encoding="utf-8-sig")
    print(f"[最新副本] {latest}")

    if config.NOTIFY.get("enable"):
        send_text(f"涨停池 {date_str} {len(pool)}只", pool_digest(pool), attach_paths=[out_path])
    else:
        print("[notify] 未启用推送(设置 config.NOTIFY.enable=True 后可推到手机)")

    print("\n[提示] 明天 9:25-9:30 运行: python auction_scan.py (或交给定时任务自动推送)")


if __name__ == "__main__":
    main()
