# -*- coding: utf-8 -*-
"""
tail_scan —— 14:30 后"一夜持股法"8条件全量扫描（对应通达信 03 号公式的增强版）
====================================================
两阶段:
  [阶段1] 实时行情初筛(框架8条中可在快照层实现的4条 + 剔除规则):
          涨幅3-5% / 官方量比>1 / 换手5-10% / 流通市值50-200亿
  [阶段2] 对初筛候选拉当日1分钟线与基准指数分钟线, 复核其余条件:
          回踩不破分时均价 / 贴近当日新高 / 尾盘创当日新高 /
          阶梯式温和放量(非脉冲) / 全天分时跑赢大盘
输出: output/tail/tail_YYYYMMDD_HHMM.csv (含每项复核列, 命中与否一目了然)

用法:
    python tail_scan.py              # 14:30 后运行
    python tail_scan.py --force      # 非14:30(例如盘后复盘)强制运行
    python tail_scan.py --no-index   # 跳过"跑赢大盘"复核(指数分钟接口偶尔不稳)

注意:
    - 东财分钟接口盘中数据有约几秒~分钟延迟, 14:30 刚过时"今日最高/最高时间/贴新高"
      仍可能变化, 请 14:40 后再跑一次或用 csv 里的 最新价/最高价/最高时间 列复核;
    - 第8条"入场点确认"依赖分时图形态, 机器只给近似指标, 请人工对照分时图。
仅供学习研究, 不构成投资建议。
"""
import argparse
import copy
import datetime as dt
import os
import time

import pandas as pd

import config
import common
from common import need_akshare, fetch_retry, save_csv, minute_metrics, col, should_exclude, is_st_or_delist
from notify import send_text, render_table


def _fmt(v, spec="{:.1f}"):
    try:
        f = float(v)
    except Exception:  # noqa: BLE001
        return str(v)
    if f != f:
        return "-"
    return spec.format(f)


def tail_digest(res, spot) -> str:
    """推送正文: 市场情绪+建议 放在最前, 候选按达标数分条展示。"""
    t = res.head(10)
    mood_lines, verdict = common.market_mood_block(spot)
    lines = []
    lines.append("【市场情绪】")
    lines.append(mood_lines)
    lines.append(f"建议: {verdict}")
    lines.append("")
    if len(t):
        lines.append(f"【一夜持股候选 · 共{len(res)}只, 达标数Top {len(t)}】")
        for i, (_, r) in enumerate(t.iterrows(), 1):
            star = "★ " if i == 1 else ""
            lines.append(f"{star}**{i}. {r['名称']}({r['代码']}) · 达标{_fmt(r['达标数'], '{:.0f}')}/6**")
            lines.append(f"   涨{_fmt(r['涨幅%'], '{:.2f}')}% | 量比{_fmt(r['量比'], '{:.2f}')}"
                         f" | 换手{_fmt(r['换手%'], '{:.2f}')}% | 市值{_fmt(r['流通市值(亿)'], '{:.0f}')}亿")
            marks = []
            for col, lab in (("尾盘创新高", "尾盘新高"), ("贴新高", "贴新高"), ("站上均价", "站均价"),
                             ("阶梯温和", "阶梯量"), ("无脉冲", "无脉冲"), ("跑赢大盘", "跑赢")):
                if str(r.get(col, "")) == "Y":
                    marks.append(lab)
            high = r.get("最高时间")
            if isinstance(high, str) and high and high != "nan":
                marks.append(f"新高{high}")
            if marks:
                lines.append("   " + " · ".join(marks))
    else:
        lines.append("今日无候选: 涨幅/量比/换手/市值不达标 —— 空仓也是一种操作。")
    lines.append("")
    lines.append("→ 达标高≠必买: 人工复核压力位/公告/板块情绪后再下单;")
    lines.append("  次日 9:30-10:00 无条件离场, 条件单 +3%止盈 / -2%止损。")
    return "\n".join(lines)


def spot_snapshot():
    return common.get_spot()


def phase1(spot: pd.DataFrame) -> pd.DataFrame:
    t = config.TAIL
    spot = spot.copy()
    spot["流通市值(亿)"] = spot["流通市值"] / 1e8
    m = ~spot.apply(lambda r: should_exclude(r["代码"], r["名称"], config.MARKET), axis=1)
    m &= ~spot["名称"].apply(is_st_or_delist)
    m &= spot["涨跌幅"].between(*t["pct"])
    m &= spot["量比"] > t["vol_ratio_gt"]
    m &= spot["换手率"].between(*t["turnover"])
    m &= spot["流通市值(亿)"].between(*(x / 1e8 for x in t["float_mktcap"]))
    cand = spot[m].copy()
    cand["竞价涨幅%"] = cand["涨跌幅"]
    return cand


def minute_of(code: str, day_high, day_low):
    import providers
    return providers.minute_frame(code, day_high=day_high, day_low=day_low)


def index_minutes():
    """基准指数分钟线; 依次尝试 config.PROVIDER.index_codes。"""
    import providers
    last = None
    for code in config.PROVIDER.get("index_codes", ("000985", "000001", "399001")):
        try:
            df = providers.index_minute_frame(code)
            if df is not None and len(df):
                print(f"[指数] 使用基准 {code} 共 {len(df)} 条分钟")
                return df
        except Exception as ex:  # noqa: BLE001
            last = ex
    print(f"[警告] 指数分钟线全部失败({last}); 将跳过'跑赢大盘'复核, 可加 --no-index")
    return None


def main():
    ap = argparse.ArgumentParser(description="14:30后尾盘一夜持股法扫描")
    ap.add_argument("--force", action="store_true", help="时段外强制运行(盘后复盘用)")
    ap.add_argument("--no-index", action="store_true", help="跳过跑赢大盘复核")
    ap.add_argument("--max-cand", type=int, default=60, help="阶段2分钟线复核候选上限")
    args = ap.parse_args()

    need_akshare()
    t0 = config.TAIL["window_from"]
    now_hm = common.hhmm_now()
    if now_hm < t0 and not args.force:
        print(f"[提示] 当前 {now_hm}, 该策略仅建议 {t0} 之后运行(--force 可强制, 仅适合盘后复盘)")
        raise SystemExit(0)

    print(f"[尾盘扫描] {common.now_str()}")
    spot = spot_snapshot()
    print(f"[阶段1] 实时行情 {len(spot)} 只")

    cand = phase1(spot)
    print(f"[阶段1] 4条硬条件初筛命中 {len(cand)} 只")
    if cand.empty:
        print("[结果] 无候选 —— 今日可能不适合该策略, 结合 mood_report 判断情绪后再决定是否空仓")
        return

    trade_date = dt.date.today().strftime("%Y%m%d")
    cand = cand.sort_values("换手率", ascending=False).head(args.max_cand)

    # ---- 阶段2: 分钟线复核 ----
    index_df = None if args.no_index else index_minutes()

    # 早盘模式: 提前运行(<14:30, 例如云端 13:58 触发)时, 用已有时段做近似复核
    cfg_tail = copy.deepcopy(config.TAIL)
    if now_hm < t0:
        cfg_tail["early"] = True
        cfg_tail["confirm"]["high_after"] = "13:30"
        print(f"[早盘模式] 当前 {now_hm} 早于 {t0}, 按已有时段近似复核(数据截至 {now_hm}); "
              f"新高判定放宽到 13:30 后")

    rows = []
    for i, (_, r) in enumerate(cand.iterrows(), 1):
        code, name = r["代码"], r["名称"]
        try:
            mdf = minute_of(code, r["最高"], r["最低"])
            met = minute_metrics(mdf, cfg_tail, index_df)
        except Exception as e:  # noqa: BLE001
            print(f"  [警告] {code} {name} 分钟复核失败: {e}")
            continue
        row = {
            "代码": code, "名称": name,
            "涨幅%": round(float(r["涨跌幅"]), 2),
            "量比": round(float(r["量比"]), 2),
            "换手%": round(float(r["换手率"]), 2),
            "流通市值(亿)": round(float(r["流通市值(亿)"]), 1),
            "最新价": float(r["最新价"]), "最高价": round(met.get("high") or 0, 2),
            "最高时间": met.get("high_time"),
            "尾盘创新高": "Y" if met.get("high_after") else "",
            "贴新高": "Y" if met.get("near_high") else "",
            "站上均价": "Y" if met.get("above_vwap") else "",
            "阶梯温和": "Y" if met.get("ladder_ok") else ("?" if met.get("ladder_ok") is None else ""),
            "无脉冲": "Y" if met.get("spike_ok") else "",
            "跑赢大盘%": met.get("beat_index_frac"),
            "跑赢大盘": "Y" if met.get("beat_index_ok") else "",
            "收盘价": met.get("last"),
        }
        rows.append(row)
        time.sleep(0.15)
        if i % 10 == 0:
            print(f"  ... 已复核 {i}/{len(cand)}")

    res = pd.DataFrame(rows)
    if res.empty:
        print("[结果] 阶段2全部失败(接口问题?), 请稍后重试")
        return

    flag_cols = ["尾盘创新高", "贴新高", "站上均价", "阶梯温和", "无脉冲", "跑赢大盘"]
    score = res[flag_cols].apply(lambda s: s.astype(str).str.upper().eq("Y")).sum(axis=1)
    res.insert(0, "达标数", score)

    # 排序: 达标数降序 -> 跑赢大盘 -> 涨幅
    res = res.sort_values(["达标数", "跑赢大盘%"], ascending=[False, False], na_position="last")
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M")
    out_path = os.path.join(config.TAIL["out_dir"], f"tail_{ts}.csv")
    save_csv(res, out_path)

    # 手机推送(在 config.NOTIFY 启用后自动发出)
    if config.NOTIFY.get("enable"):
        send_text(f"尾盘一夜持股 {dt.datetime.now():%m-%d %H:%M} 候选{len(res)}", tail_digest(res, spot),
                  attach_paths=[out_path])
    else:
        print("[notify] 未启用推送(设置 config.NOTIFY.enable=True 后可推到手机)")

    print("\n================ 尾盘复核结果(达标数降序, 前20) ================")
    with pd.option_context("display.max_rows", 30, "display.width", 220,
                           "display.unicode.east_asian_width", True):
        print(res.head(20).to_string(index=False))

    print("\n================ 人工复核清单(机器无法替代) ================")
    for i, c in enumerate(common.human_checklist(), 1):
        print(f"  {i}. {c}")
    print("\n[卖出纪律] 次日 9:30-10:00 无条件离场; 条件单 +3%止盈 / -2%止损 强制执行。")


if __name__ == "__main__":
    main()
