#!/usr/bin/env python3
"""抓取已连接 Android 设备上 TVBox 的运行日志（logcat）与环境信息。

用法:
    python3 tools/capture_tvbox_logs.py                    # dump 现有日志缓冲区
    python3 tools/capture_tvbox_logs.py --lines 15000
    python3 tools/capture_tvbox_logs.py --follow 25       # 清空后实时抓 25 秒（配合手动冷启动复现）

产出:
    tools/device_logs/tvbox_logcat_full.txt   完整 logcat
    tools/device_logs/tvbox_app_log.txt       TVBox 进程/关键字过滤后的日志
    tools/device_logs/tvbox_device_summary.json
    tools/tvbox_logs_report.html             汇总报告
"""
import argparse
import datetime
import json
import pathlib
import re
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "tools/device_logs"
REPORT = ROOT / "tools/tvbox_logs_report.html"
ADB = shutil.which("adb") or "/home/feng/.local/bin/adb"
DEFAULT_PKG = "com.github.tvbox.osc.jun"
KEYWORDS = [
    "tvbox", "apiconfig", "builtinsources", "remoteserver", "xxpermissions",
    "homeactivity", "jarloader", "clan://", "/sdcard/builtin", "spider",
    "loadconfig", "loadjar", "storagepermission", "okgo", "hawkconfig",
]
ERROR_RE = re.compile(r"\b(Exception|Error|FATAL|ANR|failed|failure|denied|refused|timeout|404|403)\b", re.I)
PID_RE = re.compile(r"^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+(\d+)\s+(\d+)\s+[VDIWEF]\s")


def adb(args, timeout=90, check=True):
    try:
        r = subprocess.run([ADB] + [str(x) for x in args], capture_output=True,
                           text=True, errors="replace", timeout=timeout)
    except FileNotFoundError:
        sys.exit("未找到 adb，请安装 Android platform-tools 并加入 PATH")
    except subprocess.TimeoutExpired:
        sys.exit("adb 命令超时: %s" % " ".join(args))
    if check and r.returncode:
        sys.exit("adb 命令失败: %s\n%s" % (" ".join(args), (r.stderr or r.stdout)[:500]))
    return r.stdout


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def main():
    ap = argparse.ArgumentParser(description="抓取 TVBox 设备运行日志")
    ap.add_argument("--lines", type=int, default=12000, help="最多 dump 多少行 logcat")
    ap.add_argument("--follow", type=int, default=0,
                    help="清空缓冲区后实时抓取指定秒数，用于冷启动复现（0 表示只 dump）")
    ap.add_argument("--pkg", default="", help="指定包名，缺省自动探测")
    args = ap.parse_args()

    devices = [ln.split("\t")[0] for ln in adb(["devices"]).splitlines()
               if "\tdevice" in ln and not ln.startswith("List")]
    if not devices:
        sys.exit("没有已授权的设备，请确认 USB 调试/网络 adb 已连接")
    serial = devices[0]
    print("设备: %s" % serial)

    packages = [ln.strip().replace("package:", "") for ln in
                adb(["shell", "pm", "list", "packages"]).splitlines()
                if "tvbox" in ln.lower() or "osc" in ln.lower()]
    pkg = args.pkg or (DEFAULT_PKG if DEFAULT_PKG in packages else (packages[0] if packages else DEFAULT_PKG))
    print("目标包名: %s" % pkg)
    print("设备上匹配到的包: %s" % (", ".join(packages) or "无"))

    dumpsys = adb(["shell", "dumpsys", "package", pkg], check=False)
    version_name = (re.search(r"versionName=([\w.\-]+)", dumpsys) or [None, "未知"])[1]
    version_code = (re.search(r"versionCode=(\d+)", dumpsys) or [None, "未知"])[1]
    first_install = (re.search(r"firstInstallTime=([\w\-: .]+)", dumpsys) or [None, "未知"])[1]
    last_update = (re.search(r"lastUpdateTime=([\w\-: .]+)", dumpsys) or [None, "未知"])[1]
    granted = re.findall(r"android\.permission\.(READ|WRITE)_EXTERNAL_STORAGE:\s*granted=(\w+)", dumpsys)
    permissions = [{"name": "android.permission.%s_EXTERNAL_STORAGE" % a.upper(), "granted": b == "true"}
                   for a, b in granted]
    runtime_block = dumpsys.split("runtime permissions:", 1)[-1][:1200] if "runtime permissions:" in dumpsys else ""
    for perm in ("READ_EXTERNAL_STORAGE", "WRITE_EXTERNAL_STORAGE", "MANAGE_EXTERNAL_STORAGE"):
        m = re.search(r"android\.permission\.%s:\s*granted=(\w+)" % perm, runtime_block)
        if m and not any(p["name"].endswith(perm) for p in permissions):
            permissions.append({"name": "android.permission." + perm, "granted": m.group(1) == "true"})

    pid_out = adb(["shell", "pidof", pkg], check=False).strip()
    pids = pid_out.split() if pid_out else []
    if not pids:  # 部分老设备没有 pidof，回退到 ps
        ps = adb(["shell", "ps", "-A"], check=False)
        for ln in ps.splitlines():
            cols = ln.split()
            if len(cols) > 8 and cols[-1] == pkg and cols[1].isdigit():
                pids.append(cols[1])
    print("运行中的进程 pid: %s" % (", ".join(pids) or "无（应用未在运行）"))

    if args.follow > 0:
        adb(["logcat", "-c"], check=False)
        proc = subprocess.Popen([ADB, "logcat", "-v", "threadtime"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, errors="replace")
        print("实时抓取 %d 秒，请在设备上冷启动 TVBox…" % args.follow)
        time.sleep(args.follow)
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate(timeout=10)
        lines = out.splitlines()
    else:
        lines = adb(["logcat", "-d", "-v", "threadtime", "-t", str(args.lines)],
                    timeout=120, check=False).splitlines()

    # 保存完整日志
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tvbox_logcat_full.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", errors="replace")

    def is_app_line(ln):
        m = PID_RE.match(ln)
        if m and m.group(1) in pids:
            return True
        low = ln.lower()
        return any(k in low for k in KEYWORDS)

    app_lines = [ln for ln in lines if is_app_line(ln)]
    (OUT / "tvbox_app_log.txt").write_text("\n".join(app_lines) + "\n", encoding="utf-8", errors="replace")
    error_lines = [ln for ln in app_lines if ERROR_RE.search(ln)]
    perm_lines = [ln for ln in app_lines if re.search(r"permission|存储权限|sdcard/builtin", ln, re.I)]
    builtin_lines = [ln for ln in app_lines if re.search(r"builtin|clan://|spider|loadjar|apiconfig", ln, re.I)]

    summary = {
        "captured_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "serial": serial, "package": pkg, "matched_packages": packages,
        "versionName": version_name, "versionCode": version_code,
        "firstInstallTime": first_install, "lastUpdateTime": last_update,
        "permissions": permissions, "pids": pids, "follow_seconds": args.follow,
        "logcat_lines": len(lines), "app_lines": len(app_lines),
        "error_lines": len(error_lines), "permission_lines": len(perm_lines),
        "builtin_lines": len(builtin_lines),
    }
    (OUT / "tvbox_device_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def block(title, rows, limit=40):
        if not rows:
            return "<h2>%s</h2><p class='muted'>无匹配记录</p>" % title
        return ("<h2>%s（%d 条，显示前 %d 条）</h2><pre>%s</pre>" %
                (title, len(rows), min(limit, len(rows)), esc("\n".join(rows[:limit]))))

    html = ["<meta charset='utf-8'><style>body{font-family:system-ui;margin:28px;background:#fafafa;color:#222}"
            "h1{margin-bottom:4px}h2{margin-top:26px;font-size:17px}.muted{color:#777}"
            "table{border-collapse:collapse;background:#fff}td,th{border:1px solid #ddd;padding:6px 12px;text-align:left}"
            "pre{background:#fff;border:1px solid #ddd;padding:12px;max-height:340px;overflow:auto;font-size:12px;line-height:1.5}"
            "code{background:#eee;padding:1px 5px}.warn{color:#c62828}</style>",
            "<h1>TVBox 设备运行日志</h1>",
            "<p class='muted'>抓取时间：%s ｜ 模式：%s</p>" %
            (esc(summary["captured_at"]), "实时跟随 %d 秒" % args.follow if args.follow else "dump 现有缓冲区"),
            "<table><tr><th>项目</th><th>值</th></tr>",
            "<tr><td>设备序列号</td><td><code>%s</code></td></tr>" % esc(serial),
            "<tr><td>包名</td><td><code>%s</code></td></tr>" % esc(pkg),
            "<tr><td>versionCode / versionName</td><td><code>%s</code> / <code>%s</code></td></tr>" %
            (esc(version_code), esc(version_name)),
            "<tr><td>首次安装 / 最后更新</td><td>%s / %s</td></tr>" %
            (esc(first_install), esc(last_update)),
            "<tr><td>运行中 pid</td><td>%s</td></tr>" % (esc(", ".join(pids)) or "无"),
            "<tr><td>logcat 总行数 / 应用相关</td><td>%d / %d</td></tr>" % (len(lines), len(app_lines)),
            "<tr><td>存储权限</td><td>%s</td></tr>" %
            (esc("，".join("%s=%s" % (p["name"].split(".")[-1], p["granted"]) for p in permissions)) or "未读到"),
            "</table>",
            block("内置源 / 配置加载相关", builtin_lines),
            block("权限相关", perm_lines),
            block("错误与异常", error_lines),
            "<h2>文件</h2><ul>"
            "<li><code>tools/device_logs/tvbox_logcat_full.txt</code> 完整 logcat</li>"
            "<li><code>tools/device_logs/tvbox_app_log.txt</code> 过滤后的应用日志</li>"
            "<li><code>tools/device_logs/tvbox_device_summary.json</code> 设备与版本摘要</li></ul>"]
    REPORT.write_text("\n".join(html) + "\n", encoding="utf-8")

    print("\nlogcat %d 行，应用相关 %d 行，错误 %d 行，权限 %d 行，内置源 %d 行" %
          (len(lines), len(app_lines), len(error_lines), len(perm_lines), len(builtin_lines)))
    print("versionCode=%s versionName=%s" % (version_code, version_name))
    print("报告: %s" % REPORT)
    print("日志: %s" % OUT)


if __name__ == "__main__":
    main()
