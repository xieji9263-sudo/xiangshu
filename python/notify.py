# -*- coding: utf-8 -*-
"""
notify —— 把扫描结果推到手机（微信/企业微信/邮箱）
====================================================
四种通道(在 config.NOTIFY 里配置并启用):
  1) wecom        企业微信群机器人   群聊推送(需自己拉个只有自己的群并添加机器人, 拿 webhook key)
  2) serverchan   Server酱(方糖)    推送到个人微信(需到 sct.ftqq.com 用微信登录拿 SENDKEY)
  3) pushplus     pushplus          推送到个人微信(需关注公众号获取 token)
  4) email        SMTP 邮件         任意邮箱, 手机邮件 App 收(最通用兜底)

用法:
    python notify.py test                  # 发一条测试推送, 验证通道配置
    python notify.py "标题" "正文"          # 手动推送任意内容到手机

说明:
    - 密钥建议用环境变量注入(见 README), 避免明文存文件;
    - 所有通道失败都只打印警告, 不会中断主脚本;
    - 文本按手机显示习惯做了截断(企业微信单条约2000字节)。
仅供学习研究, 不构成投资建议。
"""
import os
import sys
import json
import shutil
import datetime as dt
import urllib.request
import urllib.parse
from email.mime.text import MIMEText
from email.header import Header

import config


# ------------------------------------------------------------------
# 配置读取(支持环境变量覆盖, 避免密钥写进文件)
# ------------------------------------------------------------------
def _notify_cfg():
    return config.NOTIFY


def _secret(env, default):
    v = os.environ.get(env)
    return v if v else default


def _cfg_email():
    e = dict(_notify_cfg().get("email", {}))
    e["host"] = _secret("NOTIFY_SMTP_HOST", e.get("host", ""))
    e["port"] = int(_secret("NOTIFY_SMTP_PORT", str(e.get("port", 465))))
    e["user"] = _secret("NOTIFY_SMTP_USER", e.get("user", ""))
    e["auth_code"] = _secret("NOTIFY_SMTP_AUTH", e.get("auth_code", ""))
    return e


def _wecom_key():
    return _secret("NOTIFY_WECOM_KEY", _notify_cfg().get("wecom_key", ""))


def _serverchan_key():
    return _secret("NOTIFY_SERVERCHAN_KEY", _notify_cfg().get("serverchan_sendkey", ""))


def _pushplus_token():
    return _secret("NOTIFY_PUSHPLUS_TOKEN", _notify_cfg().get("pushplus_token", ""))


# ------------------------------------------------------------------
# HTTP 小工具(纯 stdlib)
# ------------------------------------------------------------------
def _post_json(url, payload, timeout=10):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json;charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _post_form(url, fields, timeout=10):
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _clip(s, cap):
    """按字符数截断(企业微信按字节限约2048, 中文保守按字符截)。"""
    if len(s) <= cap:
        return s
    return s[:cap] + "\n…(已截断, 完整结果见 output/ 目录csv)"


# ------------------------------------------------------------------
# 各通道发送
# ------------------------------------------------------------------
def _send_wecom(title, content):
    key = _wecom_key()
    if not key:
        return False, "未配置 wecom_key"
    body = f"{title}\n{content}" if title else content
    text = _clip(body, 1800)
    resp = _post_json(f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}",
                      {"msgtype": "text", "text": {"content": text}})
    ok = '"errcode":0' in resp
    return ok, resp[:200]


def _send_serverchan(title, content):
    key = _serverchan_key()
    if not key:
        return False, "未配置 serverchan_sendkey"
    resp = _post_form(f"https://sctapi.ftqq.com/{key}.send",
                      {"title": _clip(title or "盯盘", 32),
                       "desp": _clip(content, 6000)})
    ok = False
    try:
        ok = json.loads(resp).get("code") == 0
    except Exception:  # noqa: BLE001
        ok = False
    return ok, resp[:200]


def _send_pushplus(title, content):
    token = _pushplus_token()
    if not token:
        return False, "未配置 pushplus_token"
    resp = _post_json("https://www.pushplus.plus/send",
                      {"token": token, "title": _clip(title or "盯盘", 100),
                       "content": _clip(content, 6000), "template": "txt"})
    ok = '"code":200' in resp or '"code": 200' in resp
    return ok, resp[:200]


def _send_email(title, content):
    e = _cfg_email()
    if not (e.get("host") and e.get("user") and e.get("auth_code")
            and any(x for x in e.get("to", []) if x)):
        return False, "email 配置不完整(host/user/auth_code/to)"
    import smtplib
    msg = MIMEText(_clip(content, 30000), "plain", "utf-8")
    msg["Subject"] = Header(title or "盯盘", "utf-8")
    msg["From"] = e["user"]
    msg["To"] = ", ".join(x for x in e["to"] if x)
    to_list = [x for x in e["to"] if x]
    if e.get("ssl", True):
        s = smtplib.SMTP_SSL(e["host"], e.get("port", 465), timeout=15)
    else:
        s = smtplib.SMTP(e["host"], e.get("port", 25), timeout=15)
        s.starttls()
    try:
        s.login(e["user"], e["auth_code"])
        s.sendmail(e["user"], to_list, msg.as_string())
    finally:
        s.quit()
    return True, "ok"


_CHANNEL_FN = {
    "wecom": _send_wecom,
    "serverchan": _send_serverchan,
    "pushplus": _send_pushplus,
    "email": _send_email,
}


# ------------------------------------------------------------------
# 对外主入口
# ------------------------------------------------------------------
def send_text(title, content, attach_paths=None) -> dict:
    """
    按 config.NOTIFY 推送; attach_paths 里的文件会复制到 sync_dir(手机可读网盘目录)。
    返回 {通道: 结果}；任何异常都不上抛。
    """
    n = _notify_cfg()
    if not n.get("enable"):
        print("[notify] 未启用(把 config.NOTIFY.enable 设为 True 并配置通道后生效)")
        return {}
    results = {}
    for ch in n.get("channels", []):
        fn = _CHANNEL_FN.get(ch)
        if fn is None:
            results[ch] = f"未知通道 {ch}"
            continue
        try:
            ok, msg = fn(title, content)
            results[ch] = "ok" if ok else f"失败: {msg}"
        except Exception as e:  # noqa: BLE001
            results[ch] = f"异常: {e}"
    for ch, r in results.items():
        print(f"[notify/{ch}] {r}")

    # 可选: 同步文件到手机可读目录(OneDrive/坚果云等)
    sync_dir = n.get("sync_dir") or os.environ.get("NOTIFY_SYNC_DIR", "")
    if sync_dir and attach_paths:
        os.makedirs(sync_dir, exist_ok=True)
        for p in attach_paths:
            if p and os.path.exists(p):
                try:
                    shutil.copy2(p, os.path.join(sync_dir, os.path.basename(p)))
                    print(f"[sync] {p} -> {sync_dir}")
                except Exception as e:  # noqa: BLE001
                    print(f"[sync] 失败 {p}: {e}")
    return results


def send_file_note(title, path):
    """推送"文件已生成"提示(推送里带关键数字, 完整表在手机网盘/电脑看)。"""
    p = os.path.abspath(path)
    if os.path.exists(p):
        send_text(title, f"文件已生成: {p}", attach_paths=[p])


def render_table(df, cols, max_rows=8, floats=2):
    """把 DataFrame 若干列渲染成适合手机宽度的一列行文本(中文/数字混排)。"""
    lines = []
    for _, r in df.head(max_rows).iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if v is None:
                cells.append("-")
            elif isinstance(v, float):
                if v != v:  # NaN
                    cells.append("-")
                else:
                    cells.append(f"{v:.{floats}f}".rstrip("0").rstrip("."))
            else:
                cells.append(str(v))
        lines.append("  ".join(cells))
    return lines


def test():
    t = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = (
        f"测试推送成功 [OK] {t}\n"
        f"通道: {', '.join(_notify_cfg().get('channels', [])) or '(未配置 channels)'}\n"
        "之后各扫描脚本会在命中时自动推送到这里。"
    )
    print(body)
    r = send_text("盯盘工具测试", body)
    if not r:
        print("[notify] 未发出: 请先到 config.py 设置 NOTIFY.enable=True 并配置至少一个通道")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "test":
        test()
    elif len(sys.argv) >= 3:
        send_text(sys.argv[1], sys.argv[2])
    else:
        print(__doc__)
