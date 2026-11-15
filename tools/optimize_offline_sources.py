#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线内置源 · 第二步：整理本地快照（过筛、多轮测速、去重、排序）。

流程：读取本地保存的原始内容 → 每入口多轮测速取中位 → 内容/URL 去重
      → 剔除失效 → 多仓展开默认子线路 → 解析 spider 绝对地址并下载校验
      → 按端到端耗时升序排序（快的排前面，客户端默认使用第一条）。

输入：tools/offline_sources/candidates.json（由 snapshot_repo_sources.py 产出）
      tools/offline_sources/raw/*.txt       原始内容快照
输出：tools/offline_sources/optimized.json  整理结果，供 build_offline_sources.py 使用
      tools/offline_sources/raw/resolved_*.json / jar_*.bin
      tools/offline_sources/report.html      整理报告

注意：本版不再产出「在线备用线路」，所有线路最终都落地成本地文件。
用法：python3 tools/optimize_offline_sources.py [--rounds 3]
"""
import argparse
import hashlib
import html
import json
import pathlib
import subprocess
import sys
from datetime import datetime
from encodings import idna
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from check_tvbox_batch import tvbox_parse  # noqa: E402

BASE = ROOT / "tools" / "offline_sources"
RAW = BASE / "raw"
PROXY = "socks5h://127.0.0.1:10808"
UA = "okhttp/3.15"
PROXY_FIRST = ("raw.githubusercontent.com", "github.com", "gh-proxy.com",
               "cdn.jsdelivr.net", "jihulab.com", "agit.ai")


def normalize(url):
    p = urlsplit(url)
    labels = []
    for part in (p.hostname or "").split("."):
        if any(ord(c) > 127 for c in part):
            part = idna.ToASCII(part).decode("ascii")
        labels.append(part)
    host = ".".join(labels) + (":%d" % p.port if p.port else "")
    path = quote(p.path, safe="/%:=&?+,;@!$'()*-._~")
    return urlunsplit((p.scheme, host, path, p.query, ""))


def fetch(url, timeout=20):
    """境外域名优先代理，其余直连优先；返回 {code, body, time}。"""
    real = normalize(url)
    order = [True, False] if any(h in url for h in PROXY_FIRST) else [False, True]
    best = None
    for use_proxy in order:
        cmd = ["curl", "-sS", "-L", "--max-time", str(timeout), "-A", UA]
        cmd += (["--proxy", PROXY] if use_proxy else ["--noproxy", "*"])
        cmd += ["-w", "\n__META__%{http_code}|%{time_total}", real]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        except subprocess.TimeoutExpired:
            continue
        if b"__META__" not in r.stdout:
            continue
        body, meta = r.stdout.rsplit(b"\n__META__", 1)
        parts = meta.decode(errors="replace").strip().split("|", 1)
        cur = {"code": int(parts[0] or 0), "body": body,
               "time": float(parts[1]) if len(parts) > 1 else 0.0}
        if cur["code"] == 200 and body:
            return cur
        if best is None or cur["code"] > best["code"]:
            best = cur
    return best or {"code": 0, "body": b"", "time": 0.0}


def median(values):
    values = sorted(v for v in values if v > 0)
    if not values:
        return 99.0
    n = len(values)
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2


def parse(body):
    return tvbox_parse(body.decode("utf-8", errors="replace"))


def collection_first_url(cfg):
    urls = cfg.get("urls") or []
    if not urls:
        return ""
    item = urls[0]
    if isinstance(item, dict):
        return str(item.get("url") or item.get("api") or "")
    return str(item)


def resolve_spider(cfg, base_url):
    raw = cfg.get("spider")
    if not raw:
        return "", ""
    parts = str(raw).split(";md5;")
    jar = parts[0].strip().replace("img+", "")
    md5 = parts[1].strip() if len(parts) > 1 else ""
    return urljoin(base_url, jar), md5


def esc(s):
    return html.escape(str(s), quote=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3, help="每个地址测速次数")
    args = ap.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)

    cand_path = BASE / "candidates.json"
    if not cand_path.exists():
        raise SystemExit("缺少 %s，请先运行 tools/snapshot_repo_sources.py" % cand_path)
    cand = json.loads(cand_path.read_text(encoding="utf-8"))
    sources = cand["point"]

    print("整理开始：%d 个点播候选，每个测速 %d 次（另含多仓子线路）" % (
        len(sources), args.rounds), flush=True)
    items = []
    for n, src in enumerate(sources, 1):
        key = src["key"]
        fetch_url = src.get("fetch_url") or normalize(src["url"])
        saved = (BASE / src["raw"]).read_bytes() if (BASE / src["raw"]).exists() else b""

        times, online_body = [], b""
        for _ in range(args.rounds):
            r = fetch(fetch_url, timeout=12)
            if r["code"] == 200 and r["body"]:
                times.append(r["time"])
                if not online_body:
                    online_body = r["body"]
        entry_ms = median(times)

        body = saved or online_body
        cfg, note = parse(body)
        reasons = []
        if cfg is None:
            reasons.append("内容无法解析为 TVBox 配置")
        if not times:
            reasons.append("在线入口测速全部失败（仍可用本地快照，但排名靠后）")
            entry_ms = 99.0

        resolved_url, sub_ms, chain = fetch_url, 0.0, []
        if isinstance(cfg, dict):
            for _ in range(3):
                if "urls" not in cfg or "sites" in cfg:
                    break
                u = collection_first_url(cfg)
                if not u or u in chain:
                    reasons.append("多仓默认线路为空或成环")
                    break
                u = normalize(u)
                chain.append(u)
                r = fetch(u, timeout=15)
                sub_ms += r["time"]
                if r["code"] != 200 or not r["body"]:
                    reasons.append("默认子线路不可达：%s" % u)
                    break
                sub_cfg, _ = parse(r["body"])
                if sub_cfg is None:
                    reasons.append("默认子线路不是有效配置：%s" % u)
                    break
                cfg, resolved_url = sub_cfg, u

        sites = len(cfg.get("sites") or []) if isinstance(cfg, dict) else 0
        urls_left = len(cfg.get("urls") or []) if isinstance(cfg, dict) else 0
        if isinstance(cfg, dict) and not sites and not urls_left:
            reasons.append("配置中既无 sites 也无 urls")

        jar_url, jar_md5 = resolve_spider(cfg, resolved_url) if isinstance(cfg, dict) else ("", "")
        jar_ok, jar_size, jar_file, jar_md5_actual = False, 0, "", ""
        if jar_url:
            r = fetch(jar_url, timeout=30)
            jar_ok = r["code"] == 200 and r["body"][:2] == b"PK"
            jar_size = len(r["body"])
            if jar_ok:
                jar_file = "raw/jar_%s.bin" % key
                (RAW / ("jar_%s.bin" % key)).write_bytes(r["body"])
                jar_md5_actual = hashlib.md5(r["body"]).hexdigest()
            else:
                reasons.append("spider 不可下载（HTTP %s, %d 字节）" % (r["code"], jar_size))
        elif isinstance(cfg, dict) and sites:
            reasons.append("单仓配置缺少 spider")

        resolved_file = ""
        if isinstance(cfg, dict):
            resolved_file = "raw/resolved_%s.json" % key
            (RAW / ("resolved_%s.json" % key)).write_text(
                json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")

        content_hash = hashlib.md5(
            json.dumps(cfg, ensure_ascii=False, sort_keys=True).encode()).hexdigest() if isinstance(cfg, dict) else ""
        it = {
            "key": key, "name": src["name"], "origin": src["url"],
            "group": src["group"], "resolved_url": resolved_url,
            "chain": chain, "kind": "单仓" if sites else ("多仓" if urls_left else "无效"),
            "sites": sites, "spider_url": jar_url, "spider_md5": jar_md5,
            "spider_md5_actual": jar_md5_actual, "spider_file": jar_file,
            "spider_ok": jar_ok, "spider_size": jar_size,
            "entry_ms": round(entry_ms, 3), "sub_ms": round(sub_ms, 3),
            "total_ms": round(entry_ms + sub_ms, 3),
            "raw_file": src["raw"], "resolved_file": resolved_file,
            "content_hash": content_hash, "reasons": reasons,
        }
        items.append(it)
        print("  [%2d/%2d] %-16s 入口 %.3fs + 子线路 %.3fs = %.3fs | %s/%d站点 | spider %s %dKB | %s" % (
            n, len(sources), src["name"], entry_ms, sub_ms, it["total_ms"],
            it["kind"], sites, "OK" if jar_ok else "FAIL", jar_size // 1024,
            "; ".join(reasons) or "有效"), flush=True)

    # 去重：解析后的真实地址 + 配置内容；重复时保留更快的。
    # 不同配置共用同一个 spider jar 属正常现象，不能据此判为重复。
    kept, dropped = [], []
    seen_url, seen_content = {}, {}
    for it in sorted(items, key=lambda x: x["total_ms"]):
        dup = ""
        key_url = it["resolved_url"].split("?")[0].rstrip("/")
        if key_url in seen_url:
            dup = "与「%s」解析到同一地址" % seen_url[key_url]
        elif it["content_hash"] and it["content_hash"] in seen_content:
            dup = "与「%s」配置内容完全相同" % seen_content[it["content_hash"]]
        if it["reasons"]:
            dropped.append(it)
            continue
        if dup:
            it["reasons"] = [dup, "重复项保留响应更快的那个"]
            dropped.append(it)
            continue
        seen_url[key_url] = it["name"]
        if it["content_hash"]:
            seen_content[it["content_hash"]] = it["name"]
        kept.append(it)

    result = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rounds": args.rounds,
        "kept": kept,
        "dropped": dropped,
        "live": cand.get("live", []),
        "excluded": cand.get("excluded", []),
        "online": [],  # 用户要求：去掉在线的，全部走本地
    }
    (BASE / "optimized.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # 整理报告
    rows = []
    for i, it in enumerate(kept, 1):
        rows.append("<tr><td>%d</td><td><b>%s</b></td><td class='mono'>%s</td><td>%s</td>"
                    "<td>%d</td><td class='mono'>%.3fs</td><td class='mono'>%s</td>"
                    "<td class='ok'>保留 · 本地线路</td></tr>" % (
                        i, esc(it["name"]), esc(it["origin"]), esc(it["group"]),
                        it["sites"], it["total_ms"],
                        esc("%d KB" % (it["spider_size"] // 1024)) if it["spider_ok"] else "—"))
    for it in dropped:
        rows.append("<tr class='drop'><td>—</td><td>%s</td><td class='mono'>%s</td><td>%s</td>"
                    "<td>%d</td><td class='mono'>%.3fs</td><td class='mono'>—</td>"
                    "<td class='bad'>%s</td></tr>" % (
                        esc(it["name"]), esc(it["origin"]), esc(it["group"]),
                        it["sites"], it["total_ms"], esc("；".join(it["reasons"]))))
    for x in result["excluded"]:
        rows.append("<tr class='drop'><td>—</td><td>%s</td><td class='mono'>%s</td><td>预筛</td>"
                    "<td>—</td><td class='mono'>—</td><td class='mono'>—</td>"
                    "<td class='bad'>%s</td></tr>" % (
                        esc(x["name"]), esc(x["url"]), esc(x["why"])))
    report = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>内置源全本地化 · 过筛与测速结果</title><style>
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f6f7f9;color:#1f2329;margin:0;padding:32px}
.wrap{max-width:1180px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:28px 32px}
h1{font-size:22px;margin:0 0 6px}h2{font-size:16px;margin:26px 0 10px}
p{color:#5f6672;font-size:13px;line-height:1.8}
table{width:100%%;border-collapse:collapse;font-size:13px}th,td{padding:9px 10px;border-bottom:1px solid #eef0f3;text-align:left}
th{background:#f3f5f8;font-weight:600;color:#42474f}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:#4a5160;word-break:break-all}
.ok{color:#0a8f4a}.bad{color:#c0392b}.drop{background:#fffbfb}
code{background:#f2f4f7;padding:2px 5px;border-radius:4px;font-size:12px}
.box{background:#f7f9fc;border-left:4px solid #3b7cff;padding:12px 16px;border-radius:0 8px 8px 0;margin:14px 0}
</style></head><body><div class="wrap">
<h1>内置源全本地化 · 过筛与测速结果</h1>
<p>生成时间 %s ｜ %d 个候选，每入口测速 %d 次取中位 ｜ 耗时 = 入口请求 + 多仓默认子线路请求（端到端）</p>
<div class="box"><b>本轮要求：</b>把三仓库筛出的可用链接并入候选池，<strong>按响应速度升序排名</strong>，
快的排前面作为客户端默认线路；<strong>去掉所有在线备用线路</strong>，配置、爬虫 jar、直播列表全部落地为本地文件。</div>
<h2>保留线路（%d 条，按端到端耗时升序 = 客户端优先级）</h2>
<table><thead><tr><th>#</th><th>名称</th><th>原始入口</th><th>来源</th><th>站点数</th><th>端到端</th><th>spider</th><th>结论</th></tr></thead>
<tbody>%s</tbody></table>
<h2>说明</h2>
<p>每条线路都被解析成一份<strong>可直接加载的单仓配置</strong>（多仓已展开到默认子线路），
spider 相对路径解析成绝对地址后实际下载校验（必须是 ZIP/JAR 头 <code>PK</code>）。
落地后客户端读取的是 APK 内的本地文件，这里测的是源站响应质量，用来决定<strong>默认优先级</strong>。</p>
<p>共用同一个 spider jar 的不同配置不判为重复；jar 文件在打包阶段按 md5 复用，避免 APK 膨胀。</p>
<p>下一步由 <code>tools/build_offline_sources.py</code> 写入 assets；聚合入口中不再出现任何 http(s) 地址。</p>
</div></body></html>""" % (esc(result["generated"]), len(items), args.rounds,
                           len(kept), "".join(rows))
    (BASE / "report.html").write_text(report, encoding="utf-8")

    print("\n最终保留 %d 条（按端到端耗时，即默认优先级）：" % len(kept))
    for i, it in enumerate(kept, 1):
        print("  %2d. %-16s %.3fs  %d 站点  %s" % (
            i, it["name"], it["total_ms"], it["sites"], it["group"]))
    print("剔除 %d 条（含预筛剔除 %d 条）" % (len(dropped), len(result["excluded"])))
    print("直播本地列表 %d 条，在线备用 %d 条" % (len(result["live"]), len(result["online"])))
    print("产出：tools/offline_sources/optimized.json、report.html")


if __name__ == "__main__":
    main()
