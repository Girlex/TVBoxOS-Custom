#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量检测 TVBox 配置地址是否可用。

检测链路对齐 TVBoxOS (q215613905/TVBoxOS) 源码:
  ApiConfig.fetchConfigAsync() -> FindResult() -> fixContentPath()
  -> parseJson() / parseApiCollection() -> loadJar()

用法:
    python3 check_tvbox_batch.py [--proxy socks5h://127.0.0.1:10808] [--out report.html]
"""

import argparse
import html
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).parent))
from check_tvbox_api import run_curl  # noqa: E402

TARGETS = [
    ("老刘备", "https://raw.liucn.cc/box/m.json", "单仓"),
    ("小马", "https://szyyds.cn/tv/x.json", "单仓"),
    ("4K小盒子", "http://xhztv.top/4k.json", "单仓"),
    ("小盒子单仓", "http://xhztv.top/xhz", "单仓"),
    ("拾光多仓", "http://xmbjm.fh4u.org/dc.txt", "多仓"),
    ("挺好分享多仓", "http://ztha.top/TVBox/GYCK.json", "多仓"),
]


# ---------- 模拟 Gson lenient + ApiConfig.trimJsonObject 的清洗 ----------
def strip_comments(s):
    out, i, in_str, esc = [], 0, False, False
    while i < len(s):
        c = s[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == "/" and i + 1 < len(s) and s[i + 1] == "/":
            j = s.find("\n", i)
            if j < 0:
                break
            i = j
        elif c == "/" and i + 1 < len(s) and s[i + 1] == "*":
            j = s.find("*/", i + 2)
            i = j + 2 if j >= 0 else len(s)
        else:
            out.append(c)
            i += 1
    return "".join(out)


def fix_string_control_chars(s):
    """把字符串内部未转义的换行/制表符替换为空格，模拟 Gson 宽松容错。"""
    out, in_str, esc = [], False, False
    for ch in s:
        if in_str and ch in "\r\n\t":
            out.append(" ")
            continue
        out.append(ch)
        if ch == '"' and not esc:
            in_str = not in_str
        esc = (ch == "\\" and not esc)
    return "".join(out)


def tvbox_parse(body):
    """返回 (data, 说明)。按项目 trimJsonObject + Gson 宽松模式处理。"""
    txt = body.lstrip("\ufeff").strip()
    start, end = txt.find("{"), txt.rfind("}")
    if start >= 0 and end > start:
        txt = txt[start:end + 1]
    cleaned = re.sub(r",\s*([}\]])", r"\1", strip_comments(txt))
    relaxed = re.sub(r",\s*([}\]])", r"\1", fix_string_control_chars(cleaned))
    candidates = (
        (cleaned, ""),
        (txt, "（仅截取JSON片段）"),
        (relaxed, "含字符串内裸换行等格式瑕疵，标准 JSON 解析会失败"),
    )
    for candidate, note in candidates:
        try:
            return json.loads(candidate), note
        except Exception:
            continue
    return None, "无法解析为 JSON"


def classify(data):
    if not isinstance(data, dict):
        return "无效", "顶层不是对象"
    if "sites" in data or "spider" in data:
        return "单仓", "含 sites/spider"
    if isinstance(data.get("urls"), list) and len(data["urls"]) > 0:
        return "多仓", "含 urls 线路列表"
    return "无效", "缺少 sites 与 urls"


def spider_url(spider, config_url):
    u = (spider or "").split(";md5;")[0].strip()
    if not u:
        return ""
    return urljoin(config_url, u) if not u.lower().startswith("http") else u


# ---------- 单项检测 ----------
def check_one(item, proxy):
    name, url, claimed = item
    direct = run_curl(url, follow=True, proxy=None, timeout=20)
    proxied = run_curl(url, follow=True, proxy=proxy, timeout=20) if proxy else None
    best = direct if direct["code"] == 200 else (proxied if proxied and proxied["code"] == 200 else direct)

    res = {
        "name": name, "url": url, "claimed": claimed,
        "direct": direct, "proxy": proxied, "best": best,
        "note": "", "flaw": False, "kind": "无效", "detail": "",
        "sites": 0, "lives": 0, "parses": 0, "urls": 0,
        "spider": "", "spider_status": "-", "spider_code": 0,
        "sub_checks": [], "verdict": "不可用", "level": "bad",
    }
    if best["code"] != 200:
        res["detail"] = "HTTP %s，无法获取配置" % best["code"]
        return res

    data, note = tvbox_parse(best["body"])
    res["note"] = note
    res["flaw"] = "瑕疵" in note
    if data is None:
        res["detail"] = note
        return res
    kind, why = classify(data)
    res["kind"], res["detail"] = kind, why

    if kind == "单仓":
        sites = data.get("sites") or []
        lives = data.get("lives") or []
        parses = data.get("parses") or []
        res.update(sites=len(sites) if isinstance(sites, list) else 0,
                   lives=len(lives) if isinstance(lives, list) else 0,
                   parses=len(parses) if isinstance(parses, list) else 0,
                   spider=str(data.get("spider") or ""))
        sp = spider_url(res["spider"], url)
        res["spider_url"] = sp
    elif kind == "多仓":
        urls = data.get("urls") or []
        res["urls"] = len(urls)
        res["sub_checks"] = [{"url": (e.get("url") or e.get("api") or "") if isinstance(e, dict) else str(e)}
                             for e in urls[:3] if e]
    return res


def check_spider(r, proxy):
    sp = r.get("spider_url", "")
    if not sp:
        r["spider_status"] = "配置中未提供 spider"
        return
    res = run_curl(sp, follow=True, proxy=None, timeout=20)
    if res["code"] not in (200, 206) and proxy:
        res = run_curl(sp, follow=True, proxy=proxy, timeout=20)
    r["spider_code"] = res["code"]
    magic = res["body"][:4] if res["body"] else ""
    is_jar = magic.startswith("PK") or magic.startswith("dex")
    if res["code"] in (200, 206):
        r["spider_status"] = ("可下载 (%s 字节，jar/dex 魔数 %s)" % (res["size"], "正确" if is_jar else "不符"))
        r["spider_ok"] = is_jar or int(res["size"] or 0) > 1000
    else:
        r["spider_status"] = "下载失败 (HTTP %s)" % (res["code"] or "连接失败")
        r["spider_ok"] = False


def check_sub(r, sub, proxy):
    u = sub.get("url", "")
    if not u:
        sub["result"] = "空地址"
        return
    res = run_curl(u, follow=True, proxy=None, timeout=15)
    if res["code"] != 200 and proxy:
        res = run_curl(u, follow=True, proxy=proxy, timeout=15)
    ok_json = res["code"] == 200 and ("{" in res["body"][:500])
    sub["code"] = res["code"]
    sub["result"] = "HTTP %s / %s" % (res["code"] or "失败", "内容像配置" if ok_json else "内容异常")
    sub["ok"] = bool(ok_json)


def finalize(r):
    if r["kind"] == "无效":
        r["verdict"], r["level"] = "不可用", "bad"
        return
    if r["kind"] == "单仓":
        if not r.get("spider_ok"):
            r["verdict"], r["level"] = "不可用（spider 无法下载）", "bad"
        elif r.get("flaw"):
            r["verdict"], r["level"] = "存疑（配置含格式瑕疵）", "warn"
        else:
            r["verdict"], r["level"] = "可用", "good"
        return
    hits = sum(1 for s in r["sub_checks"] if s.get("ok"))
    if hits:
        r["verdict"], r["level"] = "可用（多仓，%d/%d 条线路抽查正常）" % (hits, len(r["sub_checks"])), "good"
    else:
        r["verdict"], r["level"] = "存疑（线路均未响应）", "warn"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--out", default=str(Path(__file__).with_name("batch_check_report.html")))
    args = ap.parse_args()

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda t: check_one(t, args.proxy), TARGETS))

    jobs = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for r in results:
            if r["kind"] == "单仓":
                jobs.append(ex.submit(check_spider, r, args.proxy))
            for sub in r["sub_checks"]:
                jobs.append(ex.submit(check_sub, r, sub, args.proxy))
        for j in jobs:
            j.result()
    for r in results:
        finalize(r)

    build_report(results, args.proxy, args.out)

    print("TVBox 配置地址批量检测（%s）" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    for r in results:
        print("  %-14s %-8s 直连=%-4s 代理=%-4s 站点=%-4s spider=%-30s => %s" % (
            r["name"], r["kind"], r["direct"]["code"] or "X",
            (r["proxy"]["code"] if r["proxy"] else "-") or "X",
            r["sites"] or r["urls"], r["spider_status"][:28], r["verdict"]))
    usable = [r["name"] for r in results if r["level"] == "good"]
    print("可用: %s" % (", ".join(usable) if usable else "无"))
    print("报告: %s" % args.out)


def build_report(results, proxy, out):
    css = """
    :root{--bg:#f6f7f9;--card:#fff;--line:#e6e8ec;--text:#1f2329;--muted:#6b7280;
          --red:#d92d20;--red-bg:#fef3f2;--green:#067647;--green-bg:#ecfdf3;
          --amber:#b54708;--amber-bg:#fffaeb;--blue:#175cd3}
    *{box-sizing:border-box}
    body{margin:0;padding:32px 20px 60px;background:var(--bg);color:var(--text);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.6}
    .wrap{max-width:1080px;margin:0 auto}
    h1{font-size:24px;margin:0 0 6px} h2{font-size:17px;margin:26px 0 12px}
    .sub{color:var(--muted);font-size:13px;margin-bottom:22px}
    .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin-bottom:16px}
    table{width:100%;border-collapse:collapse;font-size:13px}
    th,td{text-align:left;padding:11px 8px;border-bottom:1px solid var(--line);vertical-align:top}
    th{color:var(--muted);font-weight:600;background:#fafbfc}
    .mono,code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
    code{background:#f3f4f6;padding:1px 5px;border-radius:4px;word-break:break-all}
    .tag{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600;white-space:nowrap}
    .t-good{background:var(--green-bg);color:var(--green)} .t-warn{background:var(--amber-bg);color:var(--amber)}
    .t-bad{background:var(--red-bg);color:var(--red)}
    .sum{border-left:4px solid var(--blue);background:#eff8ff;border-radius:10px;padding:16px 20px;margin-bottom:18px;font-size:14px}
    .sum b{color:var(--blue)}
    ul{margin:8px 0 0;padding-left:20px;font-size:14px} li{margin:6px 0}
    .foot{color:var(--muted);font-size:12px;text-align:center;margin-top:26px}
    .small{color:var(--muted);font-size:12px}
    """

    def esc(s):
        return html.escape(str(s), quote=True)

    good = [r for r in results if r["level"] == "good"]
    warn = [r for r in results if r["level"] == "warn"]
    bad = [r for r in results if r["level"] == "bad"]

    rows = []
    for r in results:
        rows.append("<tr><td><b>%s</b><div class='small mono'>%s</div></td>"
                    "<td>%s</td><td class='mono'>%s / %s</td><td>%s</td>"
                    "<td class='mono'>%s</td><td>%s</td><td><span class='tag t-%s'>%s</span></td></tr>" % (
                        esc(r["name"]), esc(r["url"]),
                        '<span class="tag %s">%s</span>' % (
                            "t-good" if r["kind"] != "无效" else "t-bad", esc(r["kind"])),
                        esc(r["direct"]["code"] or "失败"),
                        esc((r["proxy"]["code"] if r["proxy"] else "-") or "失败"),
                        esc(("%d 站点 / %d 直播" % (r["sites"], r["lives"])) if r["kind"] == "单仓"
                            else ("%d 条线路" % r["urls"] if r["kind"] == "多仓" else "-")),
                        esc(r["spider_status"] if r["kind"] == "单仓" else
                            ("抽查 " + ", ".join(str(s.get("code") or "X") for s in r["sub_checks"])
                             if r["sub_checks"] else "-")),
                        esc(r["detail"] + ("；含格式瑕疵" if r.get("flaw") else "")), r["level"], esc(r["verdict"]),
                    ))

    detail_cards = []
    for r in results:
        extras = []
        if r.get("flaw"):
            extras.append("<li><b>格式瑕疵：%s</b>，标准 JSON 解析器会报错，依赖客户端宽松模式容错</li>" % esc(r["note"]))
        if r["kind"] == "单仓":
            extras.append("<li>spider: <code>%s</code></li>" % esc(r.get("spider_url") or r["spider"] or "无"))
            extras.append("<li>解析容器数: %d，直播源组: %d，解析器: %d</li>" % (r["sites"], r["lives"], r["parses"]))
            extras.append("<li>spider 下载: %s</li>" % esc(r["spider_status"]))
        elif r["kind"] == "多仓":
            for s in r["sub_checks"]:
                extras.append("<li>线路 <span class='mono'>%s</span> → %s</li>" % (esc(s["url"]), esc(s["result"])))
        detail_cards.append(
            "<div class='card'><h2 style='margin-top:0'>%s <span class='tag t-%s'>%s</span></h2>"
            "<div class='small mono' style='margin-bottom:8px'>%s</div><ul>"
            "<li>清洗: %s</li><li>直连 HTTP %s / 代理 HTTP %s / 大小 %s 字节</li>%s</ul></div>" % (
                esc(r["name"]), r["level"], esc(r["verdict"]), esc(r["url"]),
                esc(r["note"] or "标准 JSON" if r["kind"] != "无效" else r["detail"]),
                esc(r["direct"]["code"] or "失败"),
                esc((r["proxy"]["code"] if r["proxy"] else "-") or "失败"),
                esc(r["best"]["size"] or "0"), "".join(extras)))

    doc = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TVBox 配置地址批量检测报告</title>
<style>%s</style></head><body><div class="wrap">
<h1>TVBox 配置地址批量检测报告</h1>
<div class="sub">基于 TVBoxOS 源码 ApiConfig 加载链路的等效验证 · %s · %s</div>
<div class="sum"><b>共 %d 个地址：</b>可用 %d 个，部分可用/存疑 %d 个，不可用 %d 个。
%s</div>
""" % (css, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
       ("代理 " + proxy if proxy else "直连"), len(results), len(good), len(warn), len(bad),
       ("推荐优先使用：<b>%s</b>。" % esc("、".join(r["name"] for r in good))) if good else "无可用地址。")

    doc += """<div class="card"><h2 style="margin-top:0">总览</h2><table>
<thead><tr><th style="width:22%">名称 / 地址</th><th style="width:70px">类型</th>
<th style="width:90px">直连/代理</th><th style="width:120px">内容规模</th>
<th style="width:170px">spider / 线路抽查</th><th style="width:130px">解析情况</th><th>结论</th></tr></thead>
<tbody>__ROWS__</tbody></table>
<p class="small" style="margin-top:12px">「直连/代理」列均为 HTTP 状态码；单仓配置的 spider 是爬虫 jar，下载失败会导致站点无法加载；
多仓配置需在客户端里选择具体线路，抽查结果为线路地址的 HTTP 状态。</p></div>""".replace("__ROWS__", "".join(rows))

    doc += "<h2>逐项详情</h2>" + "".join(detail_cards)

    doc += """<div class="card"><h2 style="margin-top:0">检测方法与注意点</h2><ul>
<li>这些配置普遍带 <code>//</code> 注释、UTF-8 BOM、尾逗号或尾部 HTML 附加内容；TVBox 使用 Gson 宽松解析 +
<code>trimJsonObject()</code> 截取，能正常处理，本次检测按相同规则清洗。</li>
<li>单仓配置的 <code>spider</code> 若为相对路径，按 <code>fixContentPath()</code> 规则相对配置地址补全后再验证下载。</li>
<li>多仓配置（<code>urls</code> 数组）在 <code>parseApiCollection()</code> 中会切换成子线路地址重新加载，因此还需抽查子线路。</li>
<li>本次仅验证「能否获取并解析配置 + 关键资源可达」，不代表所有资源站点的实际播放效果；内容合规性与稳定性请自行判断。</li>
<li>本机直连 GitHub 与部分境外域名会超时/TLS 重置，故同时做了直连与代理两条路径的对照。</li>
</ul></div>
<div class="foot">由 tools/check_tvbox_batch.py 生成 · 可重复运行复测</div></div></body></html>"""

    Path(out).write_text(doc, encoding="utf-8")


if __name__ == "__main__":
    main()
