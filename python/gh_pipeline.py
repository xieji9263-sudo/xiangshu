# -*- coding: utf-8 -*-
"""
gh_pipeline —— GitHub Actions 每日调度编排
====================================================
优先用 GitHub 注入的 GITHUB_EVENT_SCHEDULE(即触发本次运行的 cron 表达式)精确判断任务，
这样即使排队延迟 10~30 分钟也不会走错分支；若取不到再按北京时间窗口兜底。

对应关系(北京时间为准, TZ=Asia/Shanghai):
  08:45  cron '45 0 * * 1-5'   盘前情绪报告
  09:35  cron '35 1 * * 1-5'   开盘扫描(可打板候选 + 全池数据)
  11:32  cron '32 3 * * 1-5'   午盘情绪报告(量能 + 昨日涨停晋级 + 建议)
  13:58  cron '58 5 * * 1-5'   尾盘一夜持股(早盘模式, 14:05 前后到手)
  14:30  cron '30 6 * * 1-5'   收盘前情绪与操作建议(量能/晋级率/主线强弱)
  15:05  cron '5 7 * * 1-5'    当日涨停池

Actions 环境无状态, 因此开盘扫描前会现场重建"上一交易日"涨停池。
仅供学习研究, 不构成投资建议。
"""
import datetime as dt
import os
import subprocess
import sys

CRON_MAP = {
    "45 0 * * 1-5": "mood_am",
    "35 1 * * 1-5": "auction",
    "32 3 * * 1-5": "mood_mid",
    "58 5 * * 1-5": "tail",
    "30 6 * * 1-5": "mood_pm",
    "5 7 * * 1-5": "pool",
}


def run(module, *args):
    print(f"===== RUN: python -m {module} {' '.join(args)} =====", flush=True)
    return subprocess.run([sys.executable, "-m", module, *args]).returncode


def prev_trading_date_str():
    """上一交易日(仅跳周末; 节假日取到无池日期时, 开盘扫描会提示无候选)。"""
    d = dt.date.today()
    for _ in range(10):
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            return d.strftime("%Y%m%d")
    return d.strftime("%Y%m%d")


def task_by_window(hm):
    """兜底: 按北京时间窗口判断。"""
    if 835 <= hm <= 930:
        return "mood_am"
    if 930 <= hm <= 1010:
        return "auction"
    if 1120 <= hm <= 1215:
        return "mood_mid"
    if 1350 <= hm <= 1425:
        return "tail"
    if 1425 <= hm <= 1505:
        return "mood_pm"
    if 1505 <= hm <= 1545:
        return "pool"
    return None


def main():
    now = dt.datetime.now()
    hm = int(now.strftime("%H%M"))
    weekday = now.weekday()
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    sched = (os.environ.get("GITHUB_EVENT_SCHEDULE") or "").strip()
    print(f"[gh_pipeline] local={now:%Y-%m-%d %H:%M:%S} weekday={weekday} event={event} cron='{sched}'",
          flush=True)

    if event == "workflow_dispatch" or weekday >= 5:
        # 手动触发 / 周末: 复盘一轮(情绪+涨停池+尾盘)
        run("mood_report")
        run("build_limitup_pool")
        run("tail_scan", "--force", "--max-cand", "12")
        return

    task = CRON_MAP.get(sched) or task_by_window(hm)
    print(f"[gh_pipeline] task={task}", flush=True)

    if task == "mood_am":
        run("mood_report")
    elif task == "auction":
        prev = prev_trading_date_str()
        print(f"[gh_pipeline] 开盘扫描: 涨停池取上一交易日 {prev}", flush=True)
        rc = run("build_limitup_pool", "--date", prev)
        if rc != 0:
            print("[gh_pipeline] 涨停池重建失败, 开盘扫描终止", flush=True)
            sys.exit(rc)
        run("auction_scan", "--force")
    elif task == "mood_mid":
        run("mood_report", "--slot", "midday")
    elif task == "tail":
        run("tail_scan", "--force")
    elif task == "mood_pm":
        run("mood_report", "--slot", "afternoon")
    elif task == "pool":
        run("build_limitup_pool")
    else:
        print(f"[gh_pipeline] {now:%H:%M} 未匹配任何任务, 跳过", flush=True)


if __name__ == "__main__":
    main()
