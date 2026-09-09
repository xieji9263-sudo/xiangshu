# -*- coding: utf-8 -*-
"""
mood_report —— 盘前/盘后市场情绪与板块温度报告（对应框架"市场情绪与板块分析"）
====================================================
免费接口能稳定给到的量化情绪信号(全部为近似统计, 供"只做情绪好的时候"做粗判断):
  1) 全市场: 上涨/下跌/平盘家数, 上涨家数占比
  2) 涨停/跌停家数(近似: 按各板涨跌停价口径+收盘/现价贴近涨停价判定; 收盘后以东财涨停池接口更准)
  3) 昨日涨停股今日表现(打板情绪): 红盘率/平均涨幅/高标(高连板)今日涨跌 —— 来自 build_limitup_pool 的池
  4) 主线板块: 昨日涨停池 行业分布与最高连板; 收盘后可再取当日涨停池行业分布
  5) 综合温度文本(简化规则, 仅供参考, 非"股市温度计"同类加权估值工具)

用法:
    python mood_report.py                    # 任意时点(盘前/盘中/盘后)
    python mood_report.py --no-pool          # 未构建涨停池时跳过第3/4项
输出: output/mood/mood_YYYYMMDD_HHMM.csv
仅供学习研究, 不构成投资建议。
"""
import argparse
import datetime as dt
import os

import pandas as pd

import config
import common
from common import need_akshare, fetch_retry, save_csv, classify_board, is_st_or_delist, should_exclude
from notify import send_text


def approx_limit_stats(spot: pd.DataFrame) -> dict:
    """按各板涨跌停价口径近似统计涨停/跌停家数(盘中会随封板/炸板变化)。"""
    s = spot.copy()
    s = s[~s.apply(lambda r: should_exclude(r["代码"], r["名称"], config.MARKET), axis=1)]
    s = s[~s["名称"].apply(is_st_or_delist)]
    up = down = 0
    for _, r in s.iterrows():
        lim = classify_board(r["代码"])["limit"]
        px, pc = float(r["最新价"]), float(r["昨收"])
        if pc > 0 and px > 0:
            lz = round(pc * (1 + lim / 100), 2)
            dz = round(pc * (1 - lim / 100), 2)
            if px >= lz - 0.01 and px >= float(r["最高"]) - 1e-9:
                up += 1
            if px <= dz + 0.01 and px <= float(r["最低"]) + 1e-9:
                down += 1
    return {"涨停(近似)": up, "跌停(近似)": down}


def load_pool() -> pd.DataFrame:
    p = os.path.join(config.POOL["out_dir"], "limitup_pool_latest.csv")
    return common.load_csv(p) if os.path.exists(p) else None


def board_today_pool(today: str):
    """收盘后取当日东财涨停池的行业分布; 失败/盘中返回 None。"""
    try:
        import akshare as ak
        df = fetch_retry(lambda: ak.stock_zt_pool_em(date=today), desc=f"当日涨停池 {today}")
        if df is None or df.empty:
            return None
        if "所属行业" in df.columns and "代码" in df.columns:
            return df[["代码", "名称", "连板数", "所属行业"]] if "连板数" in df.columns else df
        return df
    except Exception:  # noqa: BLE001
        return None


def main():
    ap = argparse.ArgumentParser(description="盘前/盘后市场情绪与板块报告")
    ap.add_argument("--no-pool", action="store_true", help="跳过涨停池相关统计")
    args = ap.parse_args()

    need_akshare()
    print(f"[情绪报告] {common.now_str()}")

    spot = common.get_spot()
    spot["代码"] = spot["代码"].astype(str).str.zfill(6)
    # 盘前(未开盘)时东财快照多为昨收价+0涨幅, 给出提示
    now_hm = common.hhmm_now()
    live = spot["成交量"].fillna(0).astype(float).sum() > 0
    if now_hm < "09:15" and not live:
        print("[提示] 当前为盘前且行情无成交量, 以下「今日家数」实为最近收盘快照, 仅可参考方向。")

    up_n = int((spot["涨跌幅"] > 0).sum())
    dn_n = int((spot["涨跌幅"] < 0).sum())
    flat_n = int((spot["涨跌幅"] == 0).sum())
    total = up_n + dn_n + flat_n
    red_rate = up_n / total if total else 0.0
    lim = approx_limit_stats(spot)

    print("\n================ 1) 全市场温度 ================")
    print(f"上涨 {up_n} / 下跌 {dn_n} / 平盘 {flat_n}   上涨家数占比 {red_rate:.1%}")

    print("\n================ 2) 涨停/跌停家数(近似) ================")
    print(f"涨停(近似) {lim['涨停(近似)']}    跌停(近似) {lim['跌停(近似)']}")

    pool_metrics = {}
    pool = None if args.no_pool else load_pool()
    if pool is not None and len(pool):
        pool["代码"] = pool["代码"].astype(str).str.zfill(6)
        j = spot.merge(pool[["代码", "名称", "连板数", "所属行业"]], on="代码", how="inner", suffixes=("", "_池"))
        j["连板数"] = pd.to_numeric(j.get("连板数"), errors="coerce").fillna(1)
        if len(j):
            red = int((j["涨跌幅"] > 0).sum())
            pool_metrics = {
                "昨涨停数": len(j),
                "今日红盘率": red / len(j),
                "今日平均涨幅": float(j["涨跌幅"].mean()),
                "最高连板": int(j["连板数"].max()),
                "高标(连板>=3)今日平均": (float(j.loc[j["连板数"] >= 3, "涨跌幅"].mean())
                                       if (j["连板数"] >= 3).any() else None),
            }
            print("\n================ 3) 昨日涨停股今日表现(打板情绪) ================")
            for k, v in pool_metrics.items():
                if v is None:
                    continue
                if k == "今日红盘率":
                    print(f"{k}: {v:.1%}")
                elif isinstance(v, float):
                    print(f"{k}: {v:.2f}%")
                else:
                    print(f"{k}: {v}")
            print("\n================ 4) 主线方向(昨日涨停池行业分布, 按只数Top8) ================")
            if "所属行业" in j.columns:
                grp = j.groupby("所属行业").agg(只数=("代码", "count"), 最高连板=("连板数", "max"),
                                                平均涨幅=("涨跌幅", "mean")).sort_values("只数", ascending=False)
                print(grp.head(8).round(2).to_string())
            else:
                print("(接口未返回 所属行业 列, 跳过)")
    else:
        print("\n[跳过3/4] 未找到涨停池(运行 python build_limitup_pool.py 后该两项才可用)")

    # 收盘后: 当日涨停池行业分布
    today_df = board_today_pool(common.today_str())
    if today_df is not None and "所属行业" in today_df.columns:
        print("\n================ 4b) 当日涨停池行业分布(Top8, 收盘后数据) ================")
        g2 = today_df.groupby("所属行业").agg(只数=("代码", "count"))
        print(g2.sort_values("只数", ascending=False).head(8).to_string())
    elif today_df is None:
        print("\n[提示] 当日涨停池接口暂不可用(盘中或接口调整), 4b 项跳过; 收盘后重试即可。")

    # ---- 5) 综合温度文本(简化规则) ----
    m = config.MOOD
    verdict = []
    verdict.append("普涨" if red_rate >= m["red_rate_ok"] else ("普跌" if red_rate <= 0.35 else "分化"))
    verdict.append(f"涨停{lim['涨停(近似)']}家" + ("(有热度)" if lim["涨停(近似)"] >= m["limit_count_ok"] else "(热度不足)"))
    if pool_metrics:
        verdict.append("打板情绪好" if pool_metrics["今日红盘率"] >= m["prev_pool_red_ok"] else "打板情绪差")
        verdict.append(f"高度{pool_metrics['最高连板']}板" +
                       ("(有高度)" if pool_metrics["最高连板"] >= m["top_height_ok"] else "(无高度/退潮)"))
    n_bad = sum(("差" in v) or ("不足" in v) or ("无高度" in v) or ("退潮" in v) or ("普跌" in v) for v in verdict)
    if n_bad >= 2 and ("普涨" not in verdict[0]):
        advice = "情绪偏弱: 按你的纪律应降低仓位或空仓等待, 谨慎打板/接力。"
    elif "分化" in verdict[0] and ("打板情绪好" in verdict):
        advice = "结构性行情: 只做主线板块内情绪好的个股, 控制单票仓位。"
    else:
        advice = "情绪尚可: 可正常执行 9:25 竞价扫描与 14:30 尾盘扫描, 但务必守止损纪律。"

    print("\n================ 5) 综合(简化规则, 仅供参考) ================")
    print(" / ".join(verdict))
    print(">> " + advice)

    out_path = os.path.join(config.MOOD["out_dir"], f"mood_{dt.datetime.now():%Y%m%d_%H%M}.csv")
    row = {"时间": common.now_str(), "上涨家数": up_n, "下跌家数": dn_n, "上涨占比": round(red_rate, 4)}
    row.update(lim)
    row.update(pool_metrics)
    save_csv(pd.DataFrame([row]), out_path)

    # 手机推送(在 config.NOTIFY 启用后自动发出)
    if config.NOTIFY.get("enable"):
        body = [
            f"上涨{up_n}/下跌{dn_n} 平盘{flat_n}, 上涨占比{red_rate:.1%}",
            f"涨停≈{lim['涨停(近似)']}家 / 跌停≈{lim['跌停(近似)']}家",
        ]
        if pool_metrics:
            body.append(f"昨涨停{pool_metrics['昨涨停数']}只 今日红盘率{pool_metrics['今日红盘率']:.1%} "
                        f"平均{pool_metrics['今日平均涨幅']:.2f}% 最高{pool_metrics['最高连板']}板")
        body.append(" / ".join(verdict))
        body.append(">> " + advice)
        send_text(f"情绪报告 {common.now_str()}", "\n".join(body), attach_paths=[out_path])
    else:
        print("[notify] 未启用推送(设置 config.NOTIFY.enable=True 后可推到手机)")


if __name__ == "__main__":
    main()
