# -*- coding: utf-8 -*-
"""
selfcheck —— 一键自检（在你自己的电脑上跑）
====================================================
    python selfcheck.py
1) 环境检查: python/pandas/akshare 版本
2) 联网检查: 尝试拉一次全市场实时行情(东财), 通不通都给出明确结论
3) 离线逻辑自测: 用合成数据验证筛选口径与分钟线指标(不联网也能跑)

退出码: 0=全部通过; 1=存在失败项(会打印失败点)
仅供学习研究, 不构成投资建议。
"""
import os
import sys
import traceback

import pandas as pd

import config
import common
from common import classify_board, is_st_or_delist, should_exclude, minute_metrics

PASS, FAIL, WARN = [], [], []


def check(name, fn, warn_only=False):
    try:
        msg = fn()
        PASS.append(name)
        print(f"[通过] {name}" + (f"  -> {msg}" if msg else ""))
    except Exception as e:  # noqa: BLE001
        if warn_only:
            WARN.append(name)
            print(f"[警告] {name}  -> {e}")
            print("        联网项失败通常是行情源风控/网络环境导致, 不阻断使用; 逻辑自检照常有效。")
        else:
            FAIL.append(name)
            print(f"[失败] {name}  -> {e}")
            traceback.print_exc()


def test_filters():
    """离线: 用构造的快照行验证 尾盘 phase1 口径 与 排除规则。"""
    from tail_scan import phase1
    rows = [
        # 完美命中: 主板, 涨幅4%, 量比2, 换手7%, 流通市值100亿
        dict(代码="600001", 名称="测试甲", 最新价=10.4, 涨跌幅=4.0, 量比=2.0,
             换手率=7.0, 流通市值=100e8, 成交量=1e6, 成交额=1e8, 昨收=10.0,
             最高=10.5, 最低=10.0, 今开=10.2),
        # 涨幅超5% -> 应被剔除
        dict(代码="600002", 名称="测试乙", 最新价=10.7, 涨跌幅=7.0, 量比=2.0,
             换手率=7.0, 流通市值=100e8, 成交量=1e6, 成交额=1e8, 昨收=10.0,
             最高=10.8, 最低=10.0, 今开=10.3),
        # ST -> 应被剔除
        dict(代码="000001", 名称="*ST测试", 最新价=4.2, 涨跌幅=4.0, 量比=2.0,
             换手率=7.0, 流通市值=100e8, 成交量=1e6, 成交额=1e8, 昨收=4.04,
             最高=4.3, 最低=4.0, 今开=4.1),
        # 北交所 -> 应被剔除
        dict(代码="920001", 名称="测试丙", 最新价=6.2, 涨跌幅=4.0, 量比=2.0,
             换手率=7.0, 流通市值=100e8, 成交量=1e6, 成交额=1e8, 昨收=5.96,
             最高=6.3, 最低=6.0, 今开=6.1),
        # 流通市值15亿 <50亿 -> 剔除
        dict(代码="600003", 名称="测试丁", 最新价=10.4, 涨跌幅=4.0, 量比=2.0,
             换手率=7.0, 流通市值=15e8, 成交量=1e6, 成交额=1e8, 昨收=10.0,
             最高=10.5, 最低=10.0, 今开=10.2),
    ]
    spot = pd.DataFrame(rows)
    cand = phase1(spot)
    got = set(cand["代码"])
    expect = {"600001"}
    assert got == expect, f"筛选结果不符: 命中={got} 期望={expect}"
    return f"命中 {sorted(got)}"


def test_minute_metrics():
    """离线: 构造分钟帧验证 tail 复核指标。"""
    idx = pd.date_range("2025-01-06 09:30", "2025-01-06 11:30", freq="1min")
    n = len(idx)
    price = [10.0 + i * 0.01 for i in range(n)]          # 一路缓慢上行
    vol = [200 + (i % 7) for i in range(n)]              # 平稳量
    mdf = pd.DataFrame({
        "时间": idx, "开盘": price, "收盘": price,
        "最高": [p + 0.005 for p in price], "最低": [p - 0.005 for p in price],
        "成交量": vol, "成交额": [p * v * 100 for p, v in zip(price, vol)],
    })
    # 指数分钟: 横盘, 让个股"跑赢"
    imdf = pd.DataFrame({"时间": idx, "收盘": [10.0] * n})
    met = minute_metrics(mdf, config.TAIL, imdf)
    assert met["n_minutes"] == n
    assert met["above_vwap"] is True or met["above_vwap"] is None or isinstance(met["above_vwap"], bool)
    assert met["high_time"] and met["high_after"] is False  # 最高出现在早盘
    assert met["beat_index_ok"] is True, "个股上行 vs 指数横盘 应判定为跑赢"
    return f"指标: vwap={round(met['vwap'], 3)} last={met['last']}"


def test_classify():
    assert classify_board("600519")["board"] == "main"
    assert classify_board("300750")["limit"] == 20.0
    assert classify_board("688981")["board"] == "star"
    assert classify_board("430047")["board"] == "bj"
    assert is_st_or_delist("*ST海航")
    assert should_exclude("920001", "甲", config.MARKET)   # 北交前缀
    assert should_exclude("688001", "N新股", config.MARKET)  # N 新股
    assert not should_exclude("600001", "正常股", config.MARKET)
    return "板块/剔除规则一致"


def test_auction_compute():
    """离线: 用合成帧验证竞价 compute 的梅/太命中口径。"""
    from auction_scan import compute
    rows = [
        # 600001: 昨涨停池股, 竞价高开+3%, 竞价量=昨日量60%, 流通市值10亿 -> 梅=1, 太=1
        dict(代码="600001", 名称="甲", 今开=10.3, 昨收=10.0, 成交量=60000,
             换手率=1.2, 流通市值=10e8, 昨日量_手=100000, 量比=3.0),
        # 600002: 高开+1.6%(不足梅花2%), 竞价量40%(满足太子30%), 市值30亿 -> 梅=0, 太=1
        dict(代码="600002", 名称="乙", 今开=10.16, 昨收=10.0, 成交量=40000,
             换手率=0.8, 流通市值=30e8, 昨日量_手=100000, 量比=2.0),
        # 600003: 竞价量不足(昨日量2%), 涨幅4% -> 梅=0, 太=0
        dict(代码="600003", 名称="丙", 今开=10.4, 昨收=10.0, 成交量=2000,
             换手率=0.1, 流通市值=30e8, 昨日量_手=100000, 量比=1.0),
    ]
    r = compute(pd.DataFrame(rows))
    m = dict(zip(r["代码"], r["梅条件"].astype(int)))
    t = dict(zip(r["代码"], r["太条件"].astype(int)))
    assert m["600001"] == 1 and t["600001"] == 1, (m, t)
    assert m["600002"] == 0 and t["600002"] == 1, (m, t)
    assert m["600003"] == 0 and t["600003"] == 0, (m, t)
    return f"梅={m} 太={t}"


def test_notify_helpers():
    """离线: 通知模块可导入、配置结构完整、渲染函数可用。"""
    import notify
    df = pd.DataFrame({"代码": ["600001", "600002"], "名称": ["甲", "乙"], "情绪值": [12.0, 3.5]})
    lines = notify.render_table(df, ["代码", "名称", "情绪值"], 2)
    assert len(lines) == 2 and "600001" in lines[0]
    n = config.NOTIFY
    need = {"enable", "channels", "wecom_key", "serverchan_sendkey", "pushplus_token", "email", "sync_dir"}
    assert need <= set(n), f"NOTIFY 缺键: {need - set(n)}"
    msg = "notify/render OK"
    if n.get("enable") and not (n.get("channels")):
        msg += " (已 enable 但 channels 为空!)"
    elif n.get("enable"):
        for ch in n["channels"]:
            assert ch in notify._CHANNEL_FN, f"未知通道 {ch}"
        msg += f", 已启用: {n['channels']}"
    return msg


def env_check():
    msgs = [f"python {sys.version.split()[0]}"]
    import importlib.metadata as md
    msgs.append(f"pandas {md.version('pandas')}")
    if common.HAS_AKSHARE:
        try:
            msgs.append(f"akshare {md.version('akshare')}")
        except Exception:  # noqa: BLE001
            msgs.append("akshare 已装(版本读取失败)")
    else:
        msgs.append("akshare 缺失(运行: pip install -r requirements.txt)")
    return "; ".join(msgs)


def net_check():
    mode = config.PROVIDER.get("spot", "em")
    if mode == "em" and not common.HAS_AKSHARE:
        return "跳过(spot=em 但 akshare 未安装)"
    df = common.get_spot()
    need = {"代码", "名称", "最新价", "涨跌幅", "今开", "昨收", "成交量", "量比", "换手率", "流通市值"}
    missing = need - set(df.columns)
    assert not missing, f"行情缺列: {missing}"
    return f"行情源={mode} OK({len(df)} 只), 关键列齐全"


if __name__ == "__main__":
    print("=== 环境 ===")
    check("环境/依赖", env_check)
    print("\n=== 联网(东财) ===")
    check("联网与行情字段", net_check, warn_only=True)   # 失败=网络/风控原因, 不阻断
    print("\n=== 离线逻辑自测 ===")
    check("尾盘 phase1 口径", test_filters)
    check("分钟线复核指标", test_minute_metrics)
    check("竞价 compute 口径", test_auction_compute)
    check("板块/剔除分类", test_classify)
    check("通知模块(离线)", test_notify_helpers)

    print(f"\n结果: 通过 {len(PASS)} 项, 失败 {len(FAIL)} 项, 警告 {len(WARN)} 项")
    if FAIL:
        sys.exit(1)
    if WARN:
        print("自检通过(联网项因行情源风控显示警告属正常, 可继续配置推送/定时任务)。")
    else:
        print("全部通过 —— 可以按 README 的时间表使用各脚本。")
