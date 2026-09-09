# -*- coding: utf-8 -*-
"""
providers —— 备用行情源适配层(腾讯行情, 新浪代码清单, 东财数据中心涨停池)
====================================================
背景: 部分网络环境对 东方财富 push2 实时行情接口做程序化流量拦截(浏览器可开, 脚本被断连),
      而 腾讯 qt.gtimg.cn / web.ifzq.gtimg.cn 与 新浪 可用。
本模块提供与 akshare-东财"同列名"的输出, 使 auction/tail/mood/build 四个脚本
无需改动筛选逻辑即可切换数据源。口径说明见文件内注释。

数据源(全部免费):
  - 实时全市场行情: 腾讯 qt.gtimg.cn 批量报价(含 量比/换手率/流通市值/涨幅 等字段)
  - 全市场代码清单:  新浪 Market_Center hs_a(沪深, 排除北交)
  - 日K(昨量等):     腾讯 web.ifzq.gtimg.cn fqkline(日线, 前复权)
  - 当日分钟线:      腾讯 web.ifzq.gtimg.cn minute(个股与指数)
  - 涨停池:          东财数据中心 datacenter-web.eastmoney.com (akshare stock_zt_pool_em)

腾讯 qt 行情字段位置(实测 v_sz000001, 位置稳定):
  1名称 2代码 3现价 4昨收 5今开 30时间 31涨跌额 32涨跌幅% 33最高 34最低
  36成交量(手) 37成交额(万元) 38换手率% 43振幅% 44流通市值(亿) 45总市值(亿)
  47涨停价 48跌停价 49量比 51均价
仅供学习研究, 不构成投资建议。
"""
import datetime as dt
import json
import os
import time
import urllib.request

import pandas as pd

import config

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
TMO = 12


# ------------------------------------------------------------------
# 底层请求
# ------------------------------------------------------------------
def _get(url, ref="https://gu.qq.com/"):
    req = urllib.request.Request(url, headers={**UA, "Referer": ref})
    return urllib.request.urlopen(req, timeout=TMO).read()


def _retry(fn, tries=3):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.8 * (i + 1))
    raise last


# ------------------------------------------------------------------
# 代码清单(新浪沪深 A 股) + 缓存
# ------------------------------------------------------------------
def _cache_codes_path():
    d = os.path.join(config.BASE_DIR, "output", "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"a_codes_{dt.date.today():%Y%m%d}.json")


def fetch_codes(force=False):
    """沪深 A 股 (sh/sz 前缀) 代码+名称列表, 当日缓存。"""
    p = _cache_codes_path()
    if not force and os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)

    rows = []
    seen = set()
    page = 1
    empty_streak = 0
    base = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData?sort=symbol&asc=1&node=hs_a&num=100&page={page}")
    while page <= 60 and empty_streak < 3:
        try:
            raw = _retry(lambda: _get(base.format(page=page), "https://finance.sina.com.cn/"),
                         tries=3).decode("gbk", "ignore")
            items = json.loads(raw or "[]")
        except Exception as e:  # noqa: BLE001
            print(f"[codes] 第{page}页抓取失败: {e}")
            empty_streak += 1
            page += 1
            continue
        if not items:
            empty_streak += 1
            page += 1
            continue
        empty_streak = 0  # 有数据就算有效页(可能是纯北交页, 也会被继续翻)
        for it in items:
            sym = str(it.get("symbol", ""))
            if sym[:2] in ("sh", "sz"):
                code = str(it.get("code", "")).zfill(6)
                if code not in seen:
                    seen.add(code)
                    rows.append({"code": code, "name": it.get("name", "")})
        page += 1
    if not rows:
        raise RuntimeError("新浪沪深代码清单抓取为空(网络受限或接口调整), 请稍后重试或改 PROVIDER['spot']='em'")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    return rows


def to_tencent_symbol(code):
    code = str(code).zfill(6)
    if code.startswith("6"):
        return "sh" + code
    return "sz" + code


def to_sina_symbol(code):
    code = str(code).zfill(6)
    if code.startswith("6"):
        return "sh" + code
    return "sz" + code


# ------------------------------------------------------------------
# 实时全市场行情 -> 东财同列名 DataFrame
# ------------------------------------------------------------------
def _num(v):
    try:
        f = float(v)
        return f
    except Exception:  # noqa: BLE001
        return float("nan")


def spot_tencent_frame():
    """
    全市场实时行情(腾讯批量), 返回与 akshare stock_zh_a_spot_em 同名的中文列:
    代码 名称 最新价 涨跌幅 今开 昨收 成交量(手) 成交额(元) 最高 最低
    量比 换手率 总市值(元) 流通市值(元) 涨跌额 振幅
    注: 竞价窗口(9:25-9:30)内 成交量=竞价量, 其余时段为累计量(与东财口径一致)。
    """
    codes = [c["code"] for c in fetch_codes()]
    out = []
    chunk = 300
    for i in range(0, len(codes), chunk):
        syms = [to_tencent_symbol(c) for c in codes[i:i + chunk]]
        body = _retry(lambda s=",".join(syms): _get("https://qt.gtimg.cn/q=" + s).decode("gbk", "ignore"))
        for line in body.split(";"):
            line = line.strip()
            if not line or '"' not in line:
                continue
            try:
                fields = line.split('"')[1].split("~")
            except Exception:  # noqa: BLE001
                continue
            if len(fields) < 52:
                continue
            out.append({
                "代码": str(fields[2]).zfill(6),
                "名称": fields[1],
                "最新价": _num(fields[3]),
                "昨收": _num(fields[4]),
                "今开": _num(fields[5]),
                "最高": _num(fields[33]),
                "最低": _num(fields[34]),
                "成交量": _num(fields[36]),           # 手
                "成交额": _num(fields[37]) * 1e4,    # 万元 -> 元
                "换手率": _num(fields[38]),          # %
                "涨跌幅": _num(fields[32]),          # %
                "涨跌额": _num(fields[31]),
                "量比": _num(fields[49]),
                "振幅": _num(fields[43]),
                "总市值": _num(fields[45]) * 1e8,    # 亿 -> 元
                "流通市值": _num(fields[44]) * 1e8,
                "均价": _num(fields[51]),
            })
        time.sleep(0.2)
    df = pd.DataFrame(out)
    df["代码"] = df["代码"].astype(str).str.zfill(6)
    return df


# ------------------------------------------------------------------
# 日K(前复权) -> 取 date_str 那天的成交量(手)
# ------------------------------------------------------------------
def prev_day_volume(code, date_str):
    """date_str 形如 '2025-02-28' 或 '20250228'。返回 (成交量手, 收盘价), 取不到返回 (None, None)。"""
    if "-" not in date_str:
        date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    sym = to_tencent_symbol(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,30,qfq"
    raw = _retry(lambda: _get(url)).decode("utf-8", "ignore")
    j = json.loads(raw)
    node = j["data"].get(sym, {})
    rows = node.get("qfqday") or node.get("day") or []
    for r in rows:
        if len(r) >= 6 and r[0] == date_str:
            try:
                vol = float(r[5])
            except Exception:  # noqa: BLE001
                vol = None
            try:
                close = float(r[2])
            except Exception:  # noqa: BLE001
                close = None
            return vol, close
    return None, None


# ------------------------------------------------------------------
# 当日分钟线(腾讯) -> 近似东财分钟列: 时间(ts) 收盘 最高 最低 成交量 成交额
# 腾讯分钟原文: "0930 11.76 1860 2187360.00" = 时间 价格 累计量(手) 累计额(元)
# 我们把累计量/额差分还原为每分钟量/额; 每股每分钟只有价格(无OHLC),
# 因此 最高/最低 用该分钟价格近似; day_high/day_low 用于在末行写全日极值。
# ------------------------------------------------------------------
def minute_frame(code, day_high=None, day_low=None, _date=None):
    sym = to_tencent_symbol(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}"
    raw = _retry(lambda: _get(url)).decode("utf-8", "ignore")
    j = json.loads(raw)
    node = j["data"][sym]["data"]["data"]
    rows = []
    prev_v = prev_a = 0.0
    for line in node:
        parts = line.split()
        if len(parts) < 4:
            continue
        hm, price, vcum, acum = parts[0], _num(parts[1]), _num(parts[2]), _num(parts[3])
        rows.append((hm, price, vcum - prev_v, acum - prev_a))
        prev_v, prev_a = vcum, acum
    date = _date or dt.date.today()
    recs = []
    for hm, price, v, a in rows:
        ts = dt.datetime(date.year, date.month, date.day,
                         int(hm[:2]), int(hm[2:4]))
        recs.append({"时间": ts, "收盘": price, "最高": price, "最低": price,
                     "成交量": v, "成交额": a})
    if recs:
        if day_high is not None and day_high == day_high:
            recs[-1]["最高"] = day_high
        if day_low is not None and day_low == day_low:
            recs[-1]["最低"] = day_low
    df = pd.DataFrame(recs)
    return df


def index_minute_frame(index_code, _date=None):
    """指数分钟(收盘序列即可), index_code 如 '000985'/'000001'/'399001'。"""
    code = str(index_code)
    if code.isdigit():
        prefix = "sz" if code.startswith("399") else "sh"
        code = prefix + code
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={code}"
    raw = _retry(lambda: _get(url)).decode("utf-8", "ignore")
    j = json.loads(raw)
    if code not in (j.get("data") or {}):
        raise KeyError(f"腾讯指数分钟无数据: {code}")
    node = j["data"][code]["data"]["data"]
    date = _date or dt.date.today()
    recs = []
    for line in node:
        parts = line.split()
        if len(parts) < 2:
            continue
        hm, price = parts[0], _num(parts[1])
        ts = dt.datetime(date.year, date.month, date.day, int(hm[:2]), int(hm[2:4]))
        recs.append({"时间": ts, "收盘": price})
    return pd.DataFrame(recs)


# ------------------------------------------------------------------
# 统一入口(供 common.get_spot 调用)
# ------------------------------------------------------------------
def get_spot():
    """按 config.PROVIDER['spot'] 返回东财同列名全市场行情。"""
    mode = config.PROVIDER.get("spot", "em")
    if mode == "tencent":
        return spot_tencent_frame()
    # em: akshare 东财
    import common
    common.need_akshare()
    import akshare as ak
    df = common.fetch_retry(ak.stock_zh_a_spot_em, desc="全市场实时行情(东财)")
    df["代码"] = df["代码"].astype(str).str.zfill(6)
    return df
