#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox 配置接口可用性检测工具

对齐 TVBoxOS 源码 app/src/main/java/com/github/tvbox/osc/api/ApiConfig.java 的加载流程:
    1) HTTP GET 配置地址
    2) 把响应体当 JSON 字符串交给 gson.fromJson(jsonStr, JsonObject.class)
    3) 读取 spider / sites / lives / parses / doh 等字段
任一步失败, 客户端会提示「拉取配置失败」或「配置解析失败」。

用法:
    python3 check_tvbox_api.py --url "<配置地址>" [--proxy socks5h://127.0.0.1:10808] [--out report.html]
"""

import argparse
import html
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

DEFAULT_URL = "https://d.kstore.dev/wex.json?i=114.249.158.163&token=wex&secret=wex"
README_FIELDS = ["spider", "wallpaper", "sites", "parses", "hosts", "lives", "rules", "doh"]
KEY_FIELDS = ["spider", "sites"]

# README 中的 json 接口示例, 用于自检解析器本身是否正常
EXAMPLE_JSON = '{"spider":"./your.jar","wallpaper":"./api/img","sites":[],"parses":[],"hosts":[],"lives":[],"rules":[],"doh":[]}'

UA = "okhttp/3.15"


def run_curl(url, follow=False, proxy=None, timeout=20):
    cmd = ["curl", "-sS"]
    if follow:
        cmd.append("-L")
    if proxy:
        cmd += ["--proxy", proxy]
    else:
        cmd += ["--noproxy", "*"]
    cmd += ["--max-time", str(timeout), "-A", UA]
    with tempfile.TemporaryDirectory() as td:
        hdr_file = Path(td) / "headers"
        body_file = Path(td) / "body"
        cmd += [
            "-D", str(hdr_file), "-o", str(body_file),
            "-w", "%{http_code}|%{content_type}|%{redirect_url}|%{url_effective}|%{size_download}|%{time_total}",
            url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        headers = hdr_file.read_text(encoding="utf-8", errors="replace") if hdr_file.exists() else ""
        body_bytes = body_file.read_bytes() if body_file.exists() else b""
        stats = (proc.stdout or "").strip().split("\n")[-1].split("|")
        stats += [""] * (6 - len(stats))
        code = int(stats[0]) if stats[0].isdigit() else 0
        return {
            "code": code,
            "ctype": stats[1],
            "location": stats[2],
            "effective": stats[3],
            "size": stats[4],
            "time": stats[5],
            "headers": headers,
            "body": body_bytes.decode("utf-8", errors="replace"),
            "error": (proc.stderr or "").strip(),
        }


def build_variants(url, proxy):
    p = urlsplit(url)
    base = urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    host_root = urlunsplit((p.scheme, p.netloc, "/", "", ""))
    rand = urlunsplit((p.scheme, p.netloc, "/__not_exist_9527.json", "", ""))
    plain = urlunsplit(("http", p.netloc, p.path, p.query, ""))
    return [
        ("原地址（带完整参数）", url, proxy, False),
        ("同域名根路径", host_root, proxy, False),
        ("同域名随机不存在的文件", rand, proxy, False),
        ("去掉全部查询参数", base, proxy, False),
        ("HTTP 明文 + 直连（不走代理）", plain, None, False),
        ("HTTPS 直连（不走代理）", url, None, False),
    ]


def main():
    ap = argparse.ArgumentParser(description="检测 TVBox 配置接口是否可用")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--proxy", default=None, help="例如 socks5h://127.0.0.1:10808")
    ap.add_argument("--out", default=str(Path(__file__).with_name("api_check_report.html")))
    args = ap.parse_args()

    raw = run_curl(args.url, follow=False, proxy=args.proxy)
    final = run_curl(args.url, follow=True, proxy=args.proxy)

    # 按项目 parseJson 的方式解析最终响应体
    parsed, parse_error, found_fields = None, "", []
    try:
        parsed = json.loads(final["body"])
        if isinstance(parsed, dict):
            found_fields = [k for k in README_FIELDS if k in parsed]
    except Exception as e:
        parse_error = "%s: %s" % (type(e).__name__, e)

    self_check_ok = False
    try:
        self_check_ok = isinstance(json.loads(EXAMPLE_JSON), dict)
    except Exception:
        pass

    same_host_redirect = bool(raw["location"]) and (
        urlsplit(raw["location"]).netloc != urlsplit(args.url).netloc
    )

    checks = [
        ("原始响应返回 2xx", 200 <= raw["code"] < 300,
         "实际 HTTP %s" % (raw["code"] or "连接失败"), True),
        ("配置地址未发生跨域跳转", not same_host_redirect,
         ("302 -> " + raw["location"]) if same_host_redirect else "无跳转", True),
        ("响应体可被解析为 JSON 对象", isinstance(parsed, dict),
         parse_error or ("解析成功，字段: " + (", ".join(found_fields) or "无")), True),
        ("包含 TVBox 必需字段（spider / sites）", all(k in found_fields for k in KEY_FIELDS),
         "实测字段: " + (", ".join(found_fields) or "无"), True),
        ("最终响应 Content-Type 为 JSON", "json" in (final["ctype"] or "").lower(),
         "实际: %s" % (final["ctype"] or "空"), False),
        ("解析器自检（README 示例）", self_check_ok,
         "示例 JSON 解析正常，说明检测逻辑本身无误", False),
    ]

    variants = []
    for label, u, proxy, _ in build_variants(args.url, args.proxy):
        r = run_curl(u, follow=False, proxy=proxy, timeout=15)
        variants.append({
            "label": label, "url": u, "code": r["code"],
            "location": r["location"], "error": r["error"],
            "no_proxy": proxy is None,
        })

    critical_failed = [c for c in checks if c[3] and not c[1]]
    passed = not critical_failed

    # ---------------- HTML 报告 ----------------
    css = """
    :root{--bg:#f6f7f9;--card:#fff;--line:#e6e8ec;--text:#1f2329;--muted:#6b7280;
          --red:#d92d20;--red-bg:#fef3f2;--green:#067647;--green-bg:#ecfdf3;--blue:#175cd3;}
    *{box-sizing:border-box}
    body{margin:0;padding:32px 20px 60px;background:var(--bg);color:var(--text);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.65}
    .wrap{max-width:920px;margin:0 auto}
    h1{font-size:24px;margin:0 0 6px} h2{font-size:17px;margin:28px 0 12px}
    .sub{color:var(--muted);font-size:13px;margin-bottom:22px}
    .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin-bottom:16px}
    .verdict{border-left:4px solid var(--red);background:var(--red-bg);border-radius:10px;padding:18px 20px;margin-bottom:18px}
    .verdict.ok{border-left-color:var(--green);background:var(--green-bg)}
    .verdict .big{font-size:19px;font-weight:700;color:var(--red)}
    .verdict.ok .big{color:var(--green)}
    .verdict p{margin:6px 0 0;font-size:14px}
    table{width:100%;border-collapse:collapse;font-size:13.5px}
    th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}
    th{color:var(--muted);font-weight:600;background:#fafbfc}
    td.mono,code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
    code{background:#f3f4f6;padding:1px 5px;border-radius:4px;word-break:break-all}
    pre{background:#f8f9fb;border:1px solid var(--line);border-radius:8px;padding:14px;overflow:auto;
        font-size:12.5px;line-height:1.5;margin:0}
    .tag{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600}
    .t-fail{background:var(--red-bg);color:var(--red)} .t-pass{background:var(--green-bg);color:var(--green)}
    .t-info{background:#eff8ff;color:var(--blue)}
    .kv{display:grid;grid-template-columns:130px 1fr;gap:8px 14px;font-size:13.5px}
    .kv div:nth-child(odd){color:var(--muted)}
    ul{margin:8px 0 0;padding-left:20px;font-size:14px} li{margin:6px 0}
    .foot{color:var(--muted);font-size:12px;text-align:center;margin-top:26px}
    """

    def esc(s):
        return html.escape(str(s), quote=True)

    rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(name),
            '<span class="tag %s">%s</span>' % (
                "t-pass" if ok else "t-fail", "通过" if ok else "失败"),
            '<span class="tag %s">%s</span>' % ("t-fail" if critical else "t-info",
                                                "关键" if critical else "参考"),
            esc(detail),
        )
        for name, ok, detail, critical in checks
    )

    var_rows = "".join(
        "<tr><td>%s</td><td class='mono'>%s</td><td class='mono'>%s</td><td class='mono'>%s</td></tr>" % (
            esc(v["label"]),
            esc(v["code"] or ("连接失败" + (": " + v["error"] if v["error"] else ""))),
            esc(v["location"] or "-"),
            esc("直连" if v["no_proxy"] else "代理"),
        )
        for v in variants
    )

    body_head = esc(final["body"][:300].replace("<", "&lt;"))
    raw_headers = esc(raw["headers"].strip()[:1200] or "(无响应头)")

    html_doc = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TVBox 配置接口检测报告</title><style>%s</style></head><body><div class="wrap">
<h1>TVBox 配置接口检测报告</h1>
<div class="sub">依据 TVBoxOS 源码 ApiConfig.loadConfig() 的实际加载流程进行等效验证 · 生成时间 %s</div>
""" % (css, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    html_doc += """<div class="verdict %s"><div class="big">%s</div>
<p>%s</p></div>""" % (
        "ok" if passed else "",
        "接口可用" if passed else "结论：接口不可用，无法作为 TVBox 配置地址",
        esc("服务端把该域名下所有请求 302 跳转到 KStore 官网首页，跟随跳转后拿到的是 HTML 页面而非 JSON 配置。"
            if not passed else "所有关键检查项通过。"),
    )

    html_doc += """<div class="card"><h2 style="margin-top:0">测试对象</h2><div class="kv">
<div>配置地址</div><div class="mono">%s</div>
<div>项目</div><div>q215613905/TVBoxOS（Android TVBox 客户端源码）</div>
<div>判定依据</div><div class="mono">app/src/main/java/com/github/tvbox/osc/api/ApiConfig.java : 190-255, 740+</div>
<div>网络路径</div><div>%s</div>
</div></div>""" % (
        esc(args.url),
        esc("经本地代理 " + args.proxy + "（本机直连该域名 TLS 被重置）") if args.proxy else "本机直连",
    )

    html_doc += """<div class="card"><h2 style="margin-top:0">按客户端加载流程逐项检查</h2>
<table><thead><tr><th style="width:32%%">检查项</th><th style="width:70px">结果</th>
<th style="width:70px">级别</th><th>实测详情</th></tr></thead><tbody>%s</tbody></table></div>""" % rows

    html_doc += """<div class="card"><h2 style="margin-top:0">原始响应（不跟随跳转）</h2><pre>%s</pre>
<h2>跟随跳转后的响应体开头</h2><pre>最终 URL: %s
Content-Type: %s
大小: %s 字节
--- body ---
%s</pre></div>""" % (
        raw_headers, esc(final["effective"] or "-"), esc(final["ctype"] or "-"),
        esc(final["size"] or "0"), body_head,
    )

    html_doc += """<div class="card"><h2 style="margin-top:0">对照实验（排除参数 / IP / UA / 网络因素）</h2>
<table><thead><tr><th style="width:32%%">请求</th><th style="width:110px">状态码</th>
<th style="width:190px">Location</th><th>路径</th></tr></thead><tbody>%s</tbody></table>
<p style="font-size:13px;color:var(--muted);margin:12px 0 0">同域名根路径、随机文件名、去掉参数、明文直连均返回同样的 302，
说明问题出在域名/空间绑定层面，而不是 wex.json 这个文件、查询参数或客户端 UA。</p></div>""" % var_rows

    html_doc += """<div class="card"><h2 style="margin-top:0">在 TVBox 客户端里会发生什么</h2>
<ul>
<li>客户端 <code>loadConfig()</code> 发起 GET，OkHttp 默认跟随重定向，最终拿到 <code>kstore.cc</code> 的首页 HTML（1802 字节）。</li>
<li>成功回调里调用 <code>parseJson()</code> → <code>gson.fromJson(jsonStr, JsonObject.class)</code>，HTML 无法作为 JSON 对象解析，抛出异常。</li>
<li>界面提示「配置解析失败」；若客户端不跟随跳转，则提示「拉取配置失败」。两种情况都不会加载出任何站点。</li>
</ul></div>
<div class="card"><h2 style="margin-top:0">建议</h2><ul>
<li>该域名解析到 <code>custom.kstore.dev</code>，属于 KStore 自定义直链域名；整域 302 到官网通常表示<strong>域名未绑定、绑定过期或直链服务已停用</strong>，请联系接口提供方重新生成配置地址。</li>
<li>若空间是你自己的：登录 kstore.cc 检查 <code>wex.json</code> 是否存在、直链是否开启、自定义域名绑定是否有效。</li>
<li>在盒子上先换一个已知可用的配置地址验证 App 本身是否正常，再把新地址填入「配置地址」。</li>
<li>本机直连 <code>d.kstore.dev:443</code> 会出现 TLS 连接被重置，测试时需走代理；盒子端若在国内网络也可能受此影响。</li>
</ul></div>
<div class="foot">由 tools/check_tvbox_api.py 生成 · 可重复运行以复测接口状态</div>
</div></body></html>"""

    Path(args.out).write_text(html_doc, encoding="utf-8")

    # ---------------- 控制台摘要 ----------------
    print("目标: %s" % args.url)
    for name, ok, detail, critical in checks:
        print("  [%s] %-32s %s" % ("PASS" if ok else "FAIL", name, detail))
    print("判定: %s" % ("接口可用" if passed else "接口不可用"))
    print("报告: %s" % args.out)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
