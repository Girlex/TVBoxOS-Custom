#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检测从 qist/tvbox、cluntop/tvbox、22yune/tvbox-kids 提取到的配置链接。

候选来源：
  1) 三个仓库 README 里明确标注的配置地址（tools/check_repo_sources.py 的 EXPLICIT）
  2) 仓库根目录 json/txt 配置文件的 raw 订阅地址（tools/repo_sources_trees.json）
  3) 22yune/tvbox-kids 的 raw / gh-proxy / jsDelivr 三种导入地址

判定：HTTP 200 + 可解析为 TVBox 单仓(sites) / 多仓(urls) / 直播列表；
      单仓额外校验 spider jar 可下载，多仓抽查前 2 条子线路。

产出：tools/repo_sources_valid.json、tools/repo_sources_report.html
用法：python3 tools/check_repo_sources.py
"""
import hashlib
import html
import json
import pathlib
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from encodings import idna
from urllib.parse import urljoin, urlsplit, urlunsplit, quote

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from check_tvbox_batch import tvbox_parse  # noqa: E402

PROXY = "socks5h://127.0.0.1:10808"
UA = "okhttp/3.15"
PROXY_FIRST = ("raw.githubusercontent.com", "github.com", "gh-proxy.com",
               "cdn.jsdelivr.net", "jihulab.com", "agit.ai",
               "github.moeyy.xyz", "mirror.ghproxy.com", "ghproxy.net")

# README 中明确列出的配置地址： (来源仓库, 名称, URL)
EXPLICIT = [
    ("qist/tvbox", "饭太硬", "http://www.饭太硬.top/tv/"),
    ("qist/tvbox", "okjack", "https://jihulab.com/okcaptain/kko/raw/main/ok.txt"),
    ("qist/tvbox", "王二小放牛娃", "http://tvbox.王二小放牛娃.xyz"),
    ("qist/tvbox", "摸鱼儿", "http://我不是.摸鱼儿.top"),
    ("qist/tvbox", "霜辉月明py", "https://raw.githubusercontent.com/lm317379829/PyramidStore/pyramid/py.json"),
    ("qist/tvbox", "小米小爆脾气", "http://xhww.fun/小米/DEMO.json"),
    ("qist/tvbox", "南风", "https://agit.ai/Yoursmile7/TVBox/raw/branch/master/XC.json"),
    ("qist/tvbox", "神器每日推送", "https://神器每日推送.tk/pz.json"),
    ("qist/tvbox", "巧技", "http://pandown.pro/tvbox/tvbox.json"),
    ("qist/tvbox", "Ray", "https://100km.top/0"),
    ("qist/tvbox", "俊于", "http://home.jundie.top:81/top98.json"),
    ("qist/tvbox", "橘子柚", "https://raw.githubusercontent.com/hackyjso/box/main/jzy.txt"),
    ("qist/tvbox", "电视自用 jsm", "https://raw.githubusercontent.com/qist/tvbox/master/jsm.json"),
    ("qist/tvbox", "CF Pages jsm", "https://qist.wyfc.qzz.io/jsm.json"),
    ("qist/tvbox", "潇洒接口", "https://raw.githubusercontent.com/qist/tvbox/master/xiaosa/api.json"),
    ("qist/tvbox", "潇洒镜像", "https://qist.wyfc.qzz.io/xiaosa/api.json"),
    ("qist/tvbox", "饭太硬接口", "https://raw.githubusercontent.com/qist/tvbox/master/fty.json"),
    ("qist/tvbox", "饭太硬镜像", "https://qist.wyfc.qzz.io/fty.json"),
    ("qist/tvbox", "ghproxy 代理 jsm", "https://gh-proxy.com/https://raw.githubusercontent.com/qist/tvbox/master/jsm.json"),
    ("qist/tvbox", "moeyy 代理 jsm", "https://github.moeyy.xyz/https://raw.githubusercontent.com/qist/tvbox/master/jsm.json"),
    ("cluntop/tvbox", "clun box", "https://clun.top/box.json"),
    ("cluntop/tvbox", "clun aa", "https://clun.top/aa.json"),
    ("cluntop/tvbox", "clun jsm", "https://clun.top/jsm.json"),
    ("cluntop/tvbox", "clun fun", "https://clun.top/fun.json"),
    ("22yune/tvbox-kids", "kids raw", "https://raw.githubusercontent.com/22yune/tvbox-kids/main/kids.json"),
    ("22yune/tvbox-kids", "kids gh-proxy", "https://gh-proxy.com/https://raw.githubusercontent.com/22yune/tvbox-kids/main/kids.json"),
    ("22yune/tvbox-kids", "kids jsDelivr", "https://cdn.jsdelivr.net/gh/22yune/tvbox-kids@main/kids.json"),
]


def normalize(url):
    """中文域名转 punycode、路径做 URL 编码，保证 curl 能直接请求。"""
    try:
        p = urlsplit(url)
        labels = []
        for part in (p.hostname or "").split("."):
            if any(ord(c) > 127 for c in part):
                part = idna.ToASCII(part).decode("ascii")
            labels.append(part)
        host = ".".join(labels)
        if p.port:
            host += ":%d" % p.port
        path = quote(p.path, safe="/%:=&?+,;@!$'()*-._~")
        query = quote(p.query, safe="=&?/%:,;+@!$-._~")
        return urlunsplit((p.scheme, host, path, query, ""))
    except Exception:
        return url


def fetch(url, timeout=15, binary=False):
    """返回 {code, body, time, proxy}；直连优先，境外域名直接走代理。"""
    order = [True, False] if any(h in url for h in PROXY_FIRST) else [False, True]
    best = None
    for use_proxy in order:
        cmd = ["curl", "-sS", "-L", "--max-time", str(timeout), "-A", UA]
        cmd += ["--proxy", PROXY] if use_proxy else ["--noproxy", "*"]
        cmd += ["-w", "\n__META__%{http_code}|%{time_total}", url]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        except subprocess.TimeoutExpired:
            continue
        if b"__META__" not in r.stdout:
            continue
        body, meta = r.stdout.rsplit(b"\n__META__", 1)
        parts = meta.decode(errors="replace").strip().split("|", 1)
        cur = {"code": int(parts[0] or 0), "body": body,
               "time": float(parts[1]) if len(parts) > 1 else 0.0,
               "proxy": use_proxy}
        if cur["code"] == 200 and body:
            return cur
        if best is None or cur["code"] > best["code"]:
            best = cur
    return best or {"code": 0, "body": b"", "time": 0.0, "proxy": False}


def esc(s):
    return html.escape(str(s), quote=True)


def build_candidates():
    items = []
    for repo, name, url in EXPLICIT:
        items.append({"name": name, "url": url, "source": repo + " README",
                      "group": "README 标注的配置地址"})
    tree_path = ROOT / "tools" / "repo_sources_trees.json"
    if tree_path.exists():
        trees = json.loads(tree_path.read_text(encoding="utf-8"))
        for repo, d in trees.items():
            br = d["branch"]
            for f in d["root_configs"]:
                if f.lower() == "package.json":
                    continue
                url = "https://raw.githubusercontent.com/%s/%s/%s" % (repo, br, f)
                items.append({"name": "%s/%s" % (repo.split("/")[1], f), "url": url,
                              "source": repo, "group": "仓库内配置（raw）"})
    # 去重
    seen, out = set(), []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)
    return out


def check_one(item):
    url = normalize(item["url"])
    r = fetch(url, timeout=15)
    res = dict(item)
    res.update({"http": r["code"], "ms": round(r["time"], 3),
                "size": len(r["body"]), "proxy": r["proxy"],
                "kind": "无效", "detail": "", "spider_ok": None,
                "hash": "", "verdict": "无效", "level": "bad"})
    if r["code"] != 200 or not r["body"]:
        res["detail"] = "HTTP %s / 无响应" % (r["code"] or "连接失败")
        return res
    body = r["body"]
    res["hash"] = hashlib.md5(body).hexdigest()
    text = body.decode("utf-8", errors="replace")
    data, note = tvbox_parse(text)
    if data is None:
        if re.search(r"#EXTM3U|^[^#\n]+,\s*https?://", text[:2000], re.M):
            res["kind"] = "直播列表"
            res["detail"] = "M3U/直播文本，可作为直播源导入"
            res["verdict"], res["level"] = "可用（直播）", "warn"
        else:
            res["detail"] = "内容不是 TVBox 配置：" + (note or text[:60].replace("\n", " "))
        return res
    if "sites" in data:
        res["kind"] = "单仓"
        res["sites"] = len(data.get("sites") or [])
        spider = str(data.get("spider") or "")
        if spider:
            jar = spider.split(";md5;")[0].strip().replace("img+", "")
            jar_url = urljoin(url, jar)
            jr = fetch(jar_url, timeout=20)
            res["spider_ok"] = jr["code"] == 200 and jr["body"][:2] == b"PK"
            res["spider_detail"] = "HTTP %s / %d 字节" % (jr["code"], len(jr["body"]))
        else:
            res["spider_ok"] = False
            res["spider_detail"] = "配置未声明 spider"
        res["detail"] = "%d 个站点；spider %s" % (
            res["sites"], "可下载" if res["spider_ok"] else "不可下载（" + res.get("spider_detail", "-") + "）")
        res["verdict"] = "可用" if res["spider_ok"] else "部分可用"
        res["level"] = "good" if res["spider_ok"] else "warn"
    elif "urls" in data:
        res["kind"] = "多仓"
        urls = data.get("urls") or []
        res["sites"] = len(urls)
        subs = []
        for e in urls[:2]:
            u = (e.get("url") or e.get("api")) if isinstance(e, dict) else str(e)
            if u:
                sr = fetch(urljoin(url, str(u)), timeout=12)
                subs.append("%s:%s" % (sr["code"] or "失败", str(e.get("name", ""))[:12] if isinstance(e, dict) else ""))
        res["detail"] = "%d 条线路；抽查 %s" % (len(urls), ", ".join(subs) or "无")
        ok = any(s.startswith("200") for s in subs)
        res["verdict"] = "可用" if ok else "部分可用"
        res["level"] = "good" if ok else "warn"
    else:
        res["detail"] = "JSON 可解析但既无 sites 也无 urls"
    return res


def main():
    cands = build_candidates()
    print("候选配置地址：%d 条，开始并发检测…" % len(cands), flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(check_one, it): it for it in cands}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                results.append(f.result())
            except Exception as e:
                it = futs[f]
                bad = dict(it, http=0, ms=0, size=0, kind="无效",
                           detail="检测异常 " + str(e)[:80], verdict="无效", level="bad")
                results.append(bad)
            if i % 10 == 0:
                print("  已完成 %d/%d" % (i, len(futs)), flush=True)

    # 内容去重（相同内容保留更快的）
    valid = [r for r in results if r["level"] in ("good", "warn")]
    invalid = [r for r in results if r["level"] == "bad"]
    valid.sort(key=lambda x: x["ms"])
    seen_hash, dedup = {}, []
    for r in valid:
        h = r.get("hash")
        if h and h in seen_hash:
            r["verdict"] = "重复（与「%s」内容相同）" % seen_hash[h]
            r["level"] = "dup"
            continue
        if h:
            seen_hash[h] = r["name"]
        dedup.append(r)

    out = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "repos": ["qist/tvbox", "cluntop/tvbox", "22yune/tvbox-kids"],
        "total": len(results),
        "valid": dedup,
        "duplicates": [r for r in valid if r["level"] == "dup"],
        "invalid": invalid,
    }
    (ROOT / "tools" / "repo_sources_valid.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n可用链接 %d 条（按耗时升序）：" % len(dedup), flush=True)
    for r in dedup[:30]:
        print("  %6.3fs  %-22s %-10s %s" % (r["ms"], r["name"][:22], r["kind"], r["url"][:95]), flush=True)
    print("重复 %d 条，失效 %d 条" % (len(out["duplicates"]), len(invalid)), flush=True)

    def row(r):
        return ("<tr class='%s'><td>%s</td><td class='mono'>%s</td><td>%s</td>"
                "<td>%s</td><td class='mono'>%s</td><td>%s</td><td class='%s'>%s</td></tr>" % (
                    esc(r["level"]), esc(r["name"]), esc(r["url"]), esc(r["source"]),
                    esc(r["kind"]), "%.3fs" % r["ms"], esc(r["detail"]),
                    esc(r["level"]), esc(r["verdict"])))
    rows = "".join(row(r) for r in dedup) + "".join(
        row(r) for r in out["duplicates"]) + "".join(row(r) for r in invalid)
    report = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>三个 TVBox 仓库 · 有效链接提取</title><style>
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f6f7f9;color:#1f2329;margin:0;padding:32px}
.wrap{max-width:1240px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:28px 32px}
h1{font-size:22px;margin:0 0 6px}h2{font-size:16px;margin:24px 0 10px}
p{color:#5f6672;font-size:13px;line-height:1.8}
table{width:100%%;border-collapse:collapse;font-size:12px}th,td{padding:8px 9px;border-bottom:1px solid #eef0f3;text-align:left;vertical-align:top}
th{background:#f3f5f8;font-weight:600;color:#42474f}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;word-break:break-all;color:#4a5160}
.good{color:#0a8f4a}.warn{color:#b8860b}.bad,.dup{color:#c0392b}.dup{background:#fffbfb}
code{background:#f2f4f7;padding:2px 5px;border-radius:4px}
</style></head><body><div class="wrap">
<h1>三个 TVBox 仓库 · 有效链接提取</h1>
<p>生成时间 %s ｜ 检测 %d 条候选 ｜ <b>可用 %d 条</b>、内容重复 %d 条、失效 %d 条。<br>
来源：<code>qist/tvbox</code>、<code>cluntop/tvbox</code>、<code>22yune/tvbox-kids</code> 的 README 标注地址与仓库根目录配置文件 raw 地址。</p>
<h2>检测结果（可用在前，按响应时间升序）</h2>
<table><thead><tr><th style="width:130px">名称</th><th>地址</th><th style="width:150px">来源</th>
<th style="width:75px">类型</th><th style="width:70px">耗时</th><th style="width:250px">明细</th><th style="width:100px">结论</th></tr></thead>
<tbody>%s</tbody></table>
<h2>判定口径</h2>
<p>HTTP 200 + 内容可按 TVBox 规则解析为单仓（sites）/ 多仓（urls）/ 直播列表（M3U）；
单仓额外下载 spider jar 校验 ZIP 头，多仓抽查前两条子线路。中文域名已转 punycode，
境外域名走本机 10808 代理。仅验证「配置能否获取与解析」，不代表实际播放效果。</p>
</div></body></html>""" % (
        esc(out["generated"]), out["total"], len(dedup),
        len(out["duplicates"]), len(invalid), rows)
    (ROOT / "tools" / "repo_sources_report.html").write_text(report, encoding="utf-8")
    print("产出：tools/repo_sources_valid.json、tools/repo_sources_report.html", flush=True)


if __name__ == "__main__":
    main()
