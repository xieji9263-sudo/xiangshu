# -*- coding: utf-8 -*-
"""
common —— 公共工具库
- akshare 懒加载（未安装/离线时允许模块导入, 由 selfcheck/各脚本给出明确提示）
- 东财字段中文名别名解析
- 板块/涨跌停口径分类（情绪报告用）
- 带重试的网络调用包装
- 分钟线复核指标（纯函数, 可离线单测）
"""
import os
import sys
import time
import datetime as dt
import traceback

import pandas as pd

# ------------------------------------------------------------------
# akshare 懒加载
# ------------------------------------------------------------------
HAS_AKSHARE = False
try:
    import akshare as ak  # noqa: F401
    HAS_AKSHARE = True
except Exception as _e:  # pragma: no cover - 环境相关
    ak = None
    _AKSHARE_IMPORT_ERR = _e

def need_akshare():
    """调用网络接口前检查；缺失时给出中文安装指引。"""
    if not HAS_AKSHARE:
        print("[错误] 未安装 akshare。请在有外网/正常网络环境执行:")
        print("       pip install -r requirements.txt   (python/requirements.txt)")
        raise SystemExit(2)


def get_spot():
    """
    按 config.PROVIDER['spot'] 返回"东财同列名"的全市场实时行情:
      em      -> akshare 东财 push2(正常网络推荐)
      tencent -> 腾讯 qt.gtimg.cn 备用(部分受限网络可用)
    调用方不需要关心来源, 列名一致。
    """
    import providers
    return providers.get_spot()


# ------------------------------------------------------------------
# 传输层伪装: 把 akshare 的 python-requests 换成 curl_cffi 的 Chrome 指纹请求
# 原因: 部分行情源(东方财富等)对 python-requests 的 TLS 指纹做风控,
#       表现为"浏览器能打开, 脚本连接被直接断开(RemoteDisconnected)"。
# 可在 config.FETCH["impersonate"]=False 关闭。
# ------------------------------------------------------------------
def _install_impersonated_transport():
    if not HAS_AKSHARE:
        return
    try:
        import sys
        import curl_cffi.requests as cc
        import akshare.utils.request as akreq
        import akshare.utils.func as akfunc
        import config as _cfg
        if not _cfg.FETCH.get("impersonate", True):
            return
        orig = akreq.request_with_retry

        def _cc_request_with_retry(url, params=None, timeout=15, max_retries=3,
                                   base_delay=1.0, random_delay_range=(0.5, 1.5)):
            import random
            import time
            last = None
            for attempt in range(max_retries):
                try:
                    resp = cc.get(url, params=params, timeout=timeout, impersonate="chrome")
                    resp.raise_for_status()
                    return resp
                except Exception as e:  # noqa: BLE001
                    last = e
                    if attempt < max_retries - 1:
                        time.sleep(base_delay * (2 ** attempt) + random.uniform(*random_delay_range))
            raise last

        akreq.request_with_retry = _cc_request_with_retry
        akfunc.request_with_retry = _cc_request_with_retry
        # 已加载的 akshare 子模块若持有旧引用, 一并替换
        n = 0
        for _name, _mod in list(sys.modules.items()):
            if (_name.startswith("akshare") and hasattr(_mod, "request_with_retry")
                    and getattr(_mod, "request_with_retry") is orig):
                setattr(_mod, "request_with_retry", _cc_request_with_retry)
                n += 1
        print(f"[transport] akshare 已切换 curl_cffi(chrome 指纹), 修补 {n} 个模块引用")
    except Exception as e:  # noqa: BLE001
        print(f"[transport] curl_cffi 接入失败, 继续使用原 requests: {e}")


_install_impersonated_transport()


# ------------------------------------------------------------------
# 时间与交易日
# ------------------------------------------------------------------
def now_str(fmt="%Y-%m-%d %H:%M:%S"):
    return dt.datetime.now().strftime(fmt)

def today_str():
    return dt.date.today().strftime("%Y%m%d")

def hhmm_now():
    return dt.datetime.now().strftime("%H:%M")

def last_trading_date_str():
    """最近一个交易日 YYYYMMDD。优先用东财交易日历, 失败退化为自然日向前最多5天。"""
    if HAS_AKSHARE:
        try:
            df = ak.tool_trade_date_hist_sina()
            dates = sorted(pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d").tolist())
            today = dt.date.today()
            # 今天若是交易日则返回今天（盘中构建池通常盘后跑, 我们默认要"昨天"涨停 -> 由调用方再减一天? 见 build_limitup_pool）
            for d in reversed(dates):
                if d <= today.strftime("%Y%m%d"):
                    return d
        except Exception:
            pass
    # 退化: 自然日向前找到工作日
    d = dt.date.today()
    for _ in range(7):
        if d.weekday() < 5:
            return d.strftime("%Y%m%d")
        d -= dt.timedelta(days=1)
    return today_str()


# ------------------------------------------------------------------
# 板块分类（仅用于跌停/涨停近似统计与口径标注）
# ------------------------------------------------------------------
def classify_board(code: str, name: str = "") -> dict:
    """按代码前缀粗分板块。返回 board: main|chinext|star|bj|unk。"""
    code = str(code).zfill(6)
    if code.startswith(("60", "00", "001", "002", "003")):
        return {"board": "main", "limit": 10.0}
    if code.startswith(("300", "301")):
        return {"board": "chinext", "limit": 20.0}
    if code.startswith(("688", "689")):
        return {"board": "star", "limit": 20.0}
    if code.startswith(("43", "83", "87", "88", "92")):
        return {"board": "bj", "limit": 30.0}
    return {"board": "unk", "limit": 10.0}

def is_st_or_delist(name: str) -> bool:
    name = str(name or "")
    return ("ST" in name.upper()) or ("退" in name)

def should_exclude(code: str, name: str, cfg_market: dict) -> bool:
    """是否应剔除: ST/退市/新股代码(N,C开头)/北交所前缀/特征前缀。"""
    kw = cfg_market.get("exclude_name_kw", ())
    up = str(name or "").upper()
    if any(up.startswith(k) for k in kw):
        return True
    code = str(code).zfill(6)
    if any(code.startswith(p) for p in cfg_market.get("exclude_prefix", ())):
        return True
    return False


# ------------------------------------------------------------------
# 东财数据帧字段别名解析
# ------------------------------------------------------------------
def col(df: pd.DataFrame, *names):
    """按候选名顺序取第一列；找不到则给出可用列清单并报错。"""
    for n in names:
        if n in df.columns:
            return df[n]
    avail = "、".join(str(c) for c in df.columns)
    raise KeyError(f"缺少所需字段(依次尝试: {'/'.join(names)})。实际列: {avail}")

# 东财实时行情常用中文列名（ak.stock_zh_a_spot_em）
SPOT_COLS = dict(
    code="代码", name="名称", price="最新价", pct="涨跌幅",
    vol="成交量", amount="成交额", high="最高", low="最低",
    open="今开", pre_close="昨收", vol_ratio="量比",
    turnover="换手率", mktcap="总市值", float_mktcap="流通市值",
    change="涨跌额",
)


# ------------------------------------------------------------------
# 网络调用（重试+间隔）
# ------------------------------------------------------------------
def fetch_retry(fn, *, retries=None, sleep=None, desc="请求"):
    import config
    cfg = config.FETCH
    retries = retries if retries is not None else cfg.get("retries", 3)
    sleep = sleep if sleep is not None else cfg.get("sleep", 0.4)
    last = None
    for i in range(retries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries:
                time.sleep(sleep * (i + 1))
    raise RuntimeError(f"{desc} 失败({retries+1}次重试后): {last}")


# ------------------------------------------------------------------
# CSV 读写（统一 UTF-8-sig, 便于 Excel 打开）
# ------------------------------------------------------------------
def save_csv(df: pd.DataFrame, path: str):
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"[已保存] {path}  ({len(df)} 行)")

def load_csv(path: str) -> pd.DataFrame:
    return pd.read_csv(path, dtype={"代码": str, "code": str})


# ------------------------------------------------------------------
# 分钟线指标（纯函数；分钟帧需含列: 时间(datetime), 收盘, 最高, 最低, 成交量(手), 成交额(元)）
# ------------------------------------------------------------------
def _t(x):
    if isinstance(x, str):
        return pd.to_datetime(x)
    return pd.Timestamp(x)

def minute_metrics(mdf: pd.DataFrame, cfg_tail: dict, index_mdf: pd.DataFrame = None) -> dict:
    """
    对单只股票的当日分钟线计算尾盘复核指标, 返回 dict。
    index_mdf: 基准指数当日分钟线(含 时间/收盘), 用于"分时跑赢大盘"。
    全部为近似统计, 用于初筛后的人工确认, 不是精确交易信号。
    """
    out = {}
    m = mdf.copy()
    m["_t"] = pd.to_datetime(m["时间"] if "时间" in m.columns else m["datetime"])
    m = m[m["_t"].dt.date == m["_t"].dt.date.iloc[0]].reset_index(drop=True)
    out["n_minutes"] = int(len(m))

    # --- 分时均价线与回踩判断 ---
    cum_amt = m["成交额"].astype(float).cumsum()
    cum_vol = m["成交量"].astype(float).cumsum()  # 手
    vwap = (cum_amt / (cum_vol * 100)).replace([float("inf")], float("nan"))
    out["vwap"] = float(vwap.iloc[-1]) if len(vwap) and pd.notna(vwap.iloc[-1]) else float("nan")
    out["last"] = float(m["收盘"].iloc[-1]) if len(m) else float("nan")
    out["high"] = float(m["最高"].max()) if len(m) else float("nan")
    conf = cfg_tail.get("confirm", {})
    if out["vwap"] == out["vwap"] and out["last"] == out["last"]:
        out["above_vwap"] = bool(out["last"] >= out["vwap"]) if conf.get("above_vwap", True) else None
    else:
        out["above_vwap"] = None
    if out["high"] == out["high"] and out["last"] == out["last"]:
        out["near_high"] = bool(out["last"] >= out["high"] * conf.get("near_high", 0.995))
    else:
        out["near_high"] = None

    # --- 当日最高出现时点 ---
    hi_t = m.loc[m["最高"].astype(float).idxmax(), "_t"]
    out["high_time"] = hi_t.strftime("%H:%M:%S")
    ha = conf.get("high_after", "14:25")
    out["high_after"] = bool(hi_t.strftime("%H:%M") >= ha)

    # --- 阶梯式温和放大: 4 桶每分钟均量 ---
    vol = m["成交量"].astype(float)
    buckets = {"early": ("09:30", "10:30"), "mid1": ("10:30", "11:30"),
               "mid2": ("13:00", "14:00"), "tail": ("14:00", "15:00")}
    per_min = {}
    hm = m["_t"].dt.strftime("%H:%M")
    for k, (a, b) in buckets.items():
        seg = vol[(hm >= a) & (hm < b)]
        per_min[k] = float(seg.mean()) if len(seg) else float("nan")
    prev_avg = pd.Series([per_min.get("early"), per_min.get("mid1"), per_min.get("mid2")],
                         dtype="float").mean()
    out["per_min_vol"] = {k: (round(v, 1) if v == v else None) for k, v in per_min.items()}
    lad = cfg_tail.get("ladder", {})
    lo, hi = lad.get("tail_vs_prev", (0.8, 2.5))
    if prev_avg == prev_avg and per_min["tail"] == per_min["tail"] and prev_avg > 0:
        r = per_min["tail"] / prev_avg
        out["tail_vs_prev"] = round(r, 3)
        out["ladder_ok"] = bool(lo <= r <= hi)
    else:
        out["tail_vs_prev"] = None
        out["ladder_ok"] = None

    # --- 脉冲检查: 任意连续5分钟量 vs 全天5分钟均量 ---
    v5 = vol.rolling(5).mean()
    avg5 = vol.mean()
    out["max_5min_spike"] = round(float(v5.max() / avg5), 3) if len(v5) and avg5 > 0 else None
    out["spike_ok"] = bool(out["max_5min_spike"] is not None and
                           out["max_5min_spike"] <= lad.get("max_5min_spike", 4.0))

    # --- 全天逐分钟涨幅 vs 大盘 ---
    out["beat_index_frac"] = None
    out["beat_index_ok"] = None
    if index_mdf is not None and len(index_mdf):
        ix = index_mdf.copy()
        ix["_t"] = pd.to_datetime(ix["时间"] if "时间" in ix.columns else ix["datetime"])
        ix = ix.sort_values("_t").reset_index(drop=True)
        merged = pd.merge(m[["_t", "收盘"]], ix[["_t", "收盘"]], on="_t", suffixes=("_stk", "_ix"))
        if len(merged) > 2 and merged["收盘_ix"].iloc[0] > 0 and merged["收盘_stk"].iloc[0] > 0:
            ret_s = merged["收盘_stk"] / merged["收盘_stk"].iloc[0] - 1
            ret_i = merged["收盘_ix"] / merged["收盘_ix"].iloc[0] - 1
            frac = float((ret_s >= ret_i).mean())
            out["beat_index_frac"] = round(frac, 3)
            out["beat_index_ok"] = bool(frac >= cfg_tail.get("beat_index", {}).get("min_frac", 0.9))
    return out


def human_checklist() -> list:
    """机器无法完成的复核项（永远需要人工）。"""
    return [
        "上方近期套牢/压力位（对照日K前高、筹码峰）",
        "利空公告（减持/解禁/业绩预亏/立案）—— 查 F10 公告",
        "板块情绪是否退潮（对照当日 mood 报告与板块涨跌）",
        "大盘环境（对照情绪报告温度区间, 只做情绪好时）",
    ]


def market_mood_block(spot: pd.DataFrame) -> tuple:
    """
    从全市场实时快照生成简短情绪摘要(供尾盘推送等复用)。
    返回 (多行文本, 一句话建议)。
    """
    import config as _cfg
    spot = spot.copy()
    pct = pd.to_numeric(spot["涨跌幅"], errors="coerce")
    up = int((pct > 0).sum())
    dn = int((pct < 0).sum())
    flat = int((pct == 0).sum())
    total = up + dn + flat
    red = up / total if total else 0.0
    lu = ld = 0
    mkt = _cfg.MARKET
    for _, r in spot.iterrows():
        code, nm = str(r["代码"]), str(r.get("名称", ""))
        if should_exclude(code, nm, mkt) or is_st_or_delist(nm):
            continue
        lim = classify_board(code)["limit"]
        try:
            px, pc, hi, lo = float(r["最新价"]), float(r["昨收"]), float(r["最高"]), float(r["最低"])
        except Exception:  # noqa: BLE001
            continue
        if pc > 0 and px > 0:
            if px >= round(pc * (1 + lim / 100), 2) - 0.01 and px >= hi - 1e-9:
                lu += 1
            if px <= round(pc * (1 - lim / 100), 2) + 0.01 and px <= lo + 1e-9:
                ld += 1
    m = _cfg.MOOD
    if red >= m["red_rate_ok"] and lu >= m["limit_count_ok"]:
        verdict = "情绪尚可: 可执行今日两轮筛选, 但务必守止损纪律"
    elif red <= 0.35 or lu < m["limit_count_ok"] / 2:
        verdict = "情绪偏弱: 建议降仓或空仓等待, 谨慎打板/接力"
    else:
        verdict = "结构性行情: 只做主线板块内强势股, 控制单票仓位"
    block = (f"上涨 {up} / 下跌 {dn} / 平盘 {flat}, 上涨占比 {red:.0%}\n"
             f"涨停≈{lu}家 / 跌停≈{ld}家")
    return block, verdict
