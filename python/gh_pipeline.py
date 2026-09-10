# -*- coding: utf-8 -*-
"""
gh_pipeline —— GitHub Actions 每日调度编排
====================================================
按北京时间当前时刻自动选择要跑的任务(工作流已把 TZ 设为 Asia/Shanghai):
  08:00-09:20  情绪报告(mood_report)
  09:20-09:55  竞价扫描: 先补建"上一交易日"涨停池, 再跑 auction_scan
  14:30-15:00  尾盘一夜持股(tail_scan)
  15:00-15:40  当日涨停池(build_limitup_pool)
  其余时间/手动触发(workflow_dispatch): 跑一轮"情绪+涨停池+尾盘"复盘(不跑竞价)
Actions 环境是无状态的(每次全新虚拟机), 因此竞价前会现场重建涨停池。
仅供学习研究, 不构成投资建议。
"""
import datetime as dt
import os
import subprocess
import sys


def run(module, *args):
    print(f"===== RUN: python -m {module} {' '.join(args)} =====", flush=True)
    return subprocess.run([sys.executable, "-m", module, *args]).returncode


def prev_trading_date_str():
    """上一交易日(仅跳周末; 节假日会取到前一交易日的前一日, 该日无涨停池则竞价结果为空, 属正常)。"""
    d = dt.date.today()
    for _ in range(10):
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            return d.strftime("%Y%m%d")
    return d.strftime("%Y%m%d")


def main():
    now = dt.datetime.now()
    hm = int(now.strftime("%H%M"))
    weekday = now.weekday()
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    print(f"[gh_pipeline] local={now:%Y-%m-%d %H:%M:%S} weekday={weekday} event={event}", flush=True)

    if event == "workflow_dispatch" or weekday >= 5:
        # 手动触发 / 周末: 复盘一轮(情绪+涨停池+尾盘), 竞价需交易日早盘窗口
        run("mood_report")
        run("build_limitup_pool")
        run("tail_scan", "--force", "--max-cand", "12")
        return

    if 830 <= hm <= 935:
        run("mood_report")
    elif 920 <= hm <= 1000:
        prev = prev_trading_date_str()
        print(f"[gh_pipeline] auction: 涨停池日期取上一交易日 {prev}", flush=True)
        rc = run("build_limitup_pool", "--date", prev)
        if rc != 0:
            print("[gh_pipeline] 涨停池重建失败, 竞价终止", flush=True)
            sys.exit(rc)
        run("auction_scan", "--force")
    elif 1350 <= hm <= 1500:
        run("tail_scan", "--force")
    elif 1500 <= hm <= 1540:
        run("build_limitup_pool")
    else:
        print(f"[gh_pipeline] {now:%H:%M} 不在任何任务窗口内, 本次调度跳过", flush=True)


if __name__ == "__main__":
    main()
