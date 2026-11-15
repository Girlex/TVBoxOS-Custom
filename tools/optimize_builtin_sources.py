#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[已废弃] 内置配置源优化（旧版：只维护一串在线 URL）。

内置源已改为「本地文件」形式：配置内容与爬虫 jar 随 APK 打包在 assets/builtin/，
Java 源码里不再有 SOURCES 常量，因此本脚本不再适用。请改用：

    python3 tools/optimize_offline_sources.py   # 测速 / 去重 / 剔除失效 / 解析真实配置
    python3 tools/build_offline_sources.py     # 生成 assets 离线快照与报告
"""

import argparse
import hashlib
import html
import json
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
from check_tvbox_api import run_curl  # noqa: E402
from check_tvbox_batch import tvbox_parse, classify, spider_url  # noqa: E402

JSON_PATH = ROOT / "tools/builtin_sources.json"
JAVA_PATH = ROOT / "app/src/main/java/com/github/tvbox/osc/util/BuiltinSources.java"
REPORT_PATH = ROOT / "tools/builtin_sources_report.html"

# 额外候选：新增待测地址 + 上一轮判定失效地址的回归复核（有效且够快才纳入）
EXTRA_CANDIDATES = [
    ("老虎TV(新)", "http://tv.laohu.cool/tvbox.json"),
    ("4K小盒子", "http://xhztv.top/4k.json"),
    ("原 wex 接口", "https://d.kstore.dev/wex.json?i=114.249.158.163&token=wex&secret=wex"),
]
# 只复核、即使恢复也不纳入内置源（私有带 IP 参数的接口）
REVIEW_ONLY = {"原 wex 接口"}


def norm_url(u):
    """URL 归一化：协议/主机小写、去掉末尾斜杠，保留查询参数。"""
    try:
        p = urlsplit(u.strip())
        path = p.path.rstrip("/") or "/"
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, p.query, ""))
    except Exception:
        return u.strip()


def digest(body):
    return hashlib.md5(body.encode("utf-8", errors="replace")).hexdigest()


def read_candidates():
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    builtin = [(x["name"], x["url"]) for x in data["urls"]]
    return builtin, builtin + EXTRA_CANDIDATES


def one_request(task, proxy):
    name, url, idx = task
    r = run_curl(url, follow=True, proxy=None, timeout=12)
    via = "直连"
    if r["code"] != 200 and proxy:
        alt = run_curl(url, follow=True, proxy=proxy, timeout=12)
        if alt["code"] == 200:
            r, via = alt, "代理"
    try:
        t = float(r["time"])
    except (TypeError, ValueError):
        t = 12.0
    return name, idx, {"code": r["code"], "time": t, "size": r["size"],
                       "ctype": r["ctype"], "via": via,
                       "body": r["body"], "error": r["error"]}


def probe(candidates, proxy, rounds):
    tasks = [(name, url, i) for name, url in candidates for i in range(rounds)]
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(lambda t: one_request(t, proxy), tasks))

    items = {}
    for name, url in candidates:
        items[name] = {"name": name, "url": url, "samples": [], "body": "", "code": 0}
    for name, idx, r in results:
        items[name]["samples"].append((idx, r))
    for it in items.values():
        it["samples"].sort(key=lambda x: x[0])
        ok = [r for _, r in it["samples"] if r["code"] == 200]
        it["code"] = ok[0]["code"] if ok else (it["samples"][0][1]["code"] if it["samples"] else 0)
        it["error"] = next((r["error"] for _, r in it["samples"] if r["error"]), "")
        it["body"] = ok[0]["body"] if ok else ""
        it["times"] = [r["time"] for r in ok] or [r["time"] for _, r in it["samples"]]
        it["median"] = round(statistics.median(it["times"]), 3)
        it["hash"] = digest(it["body"]) if it["body"] else ""
        data, note = tvbox_parse(it["body"]) if it["body"] else (None, "无响应内容")
        it["data"], it["parse_note"] = data, note
        it["kind"] = classify(data)[0] if data else "无效"
        it["sites"] = len(data.get("sites") or []) if isinstance(data, dict) else 0
        it["line_count"] = len(data.get("urls") or []) if isinstance(data, dict) else 0
    return items


def check_resources(items, proxy):
    """单仓验 spider，多仓抽查前 3 条子线路。"""
    jobs = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for it in items.values():
            if it["kind"] == "单仓":
                sp = spider_url(it["data"].get("spider", ""), it["url"])
                it["spider"] = sp
                jobs.append((it, "spider", ex.submit(run_curl, sp, True, None, 15)))
            elif it["kind"] == "多仓":
                subs = []
                for e in (it["data"].get("urls") or [])[:3]:
                    u = (e.get("url") or e.get("api") or "") if isinstance(e, dict) else str(e)
                    if u:
                        subs.append({"url": u, "fut": ex.submit(run_curl, u, True, None, 12)})
                it["subs"] = subs
                for s in subs:
                    jobs.append((it, "sub", s["fut"]))
                # 第一条线路即客户端自动切换的目标，复用上面已提交的请求
                first_fut = subs[0]["fut"] if subs else None
                first_name = subs[0]["url"] if subs else ""
                it["default_line"] = {"url": first_name, "code": 0, "time": 0.0, "valid": False}
                if first_fut:
                    jobs.append((it, "default_line", first_fut))
        for it, kind, fut in jobs:
            r = fut.result()
            if kind == "spider":
                if r["code"] not in (200, 206) and proxy:
                    r = run_curl(it["spider"], True, proxy, 15)
                magic = r["body"][:4] if r["body"] else ""
                it["spider_ok"] = r["code"] in (200, 206) and (
                    magic.startswith("PK") or magic.startswith("dex") or int(r["size"] or 0) > 1000)
                it["spider_info"] = f"HTTP {r['code'] or '失败'}, {r['size']} 字节"
            elif kind == "default_line":
                if r["code"] != 200 and proxy:
                    r = run_curl(it["default_line"]["url"], True, proxy, 12)
                d, _ = tvbox_parse(r["body"]) if r["body"] else (None, "")
                try:
                    t = float(r["time"])
                except (TypeError, ValueError):
                    t = 12.0
                it["default_line"].update(code=r["code"], time=t,
                                          valid=(r["code"] == 200 and d is not None))
            else:
                pass
        # 多仓子线路结果单独汇总
        for it in items.values():
            if "subs" not in it:
                continue
            hits = 0
            for s in it["subs"]:
                r = s["fut"].result()
                good = r["code"] == 200 and "{" in r["body"][:500]
                s["code"], s["ok"] = r["code"], good
                hits += 1 if good else 0
            it["sub_ok"] = hits > 0
            it["sub_info"] = f"{hits}/{len(it['subs'])} 条抽查正常"


def analyze(items, builtin_names):
    """去重 + 有效性判定 + 按端到端加载耗时排序。"""
    for it in items.values():
        # 多仓会再多跳一次到默认子线路，把这一跳计入实际加载耗时
        extra = it.get("default_line", {}).get("time", 0.0) if it["kind"] == "多仓" else 0.0
        it["e2e"] = round(it["median"] + extra, 3)

    seen_url, seen_hash, dup_log = {}, {}, []
    # 先按端到端耗时升序再判重：重复项保留更快的那个
    ordered = sorted(items.values(), key=lambda x: x["e2e"])
    for it in ordered:
        reasons = []
        if it["code"] != 200:
            err = (it.get("error") or "").lower()
            hint = "DNS 解析失败 / NXDOMAIN" if "resolve host" in err else ""
            reasons.append(f"HTTP {it['code'] or '连接失败'}" + (f"（{hint}）" if hint else ""))
        if it["kind"] == "无效":
            reasons.append("内容不是有效 TVBox 配置")
        elif it["kind"] == "单仓" and not it.get("spider_ok"):
            reasons.append("spider 不可下载（" + it.get("spider_info", "-") + "）")
        elif it["kind"] == "多仓":
            dl = it.get("default_line") or {}
            if not dl.get("valid"):
                reasons.append("默认子线路不可用，首次启动会自动切到它")
            elif not it.get("sub_ok"):
                reasons.append("子线路全部不可用（" + it.get("sub_info", "-") + "）")
        if it["name"] in REVIEW_ONLY:
            reasons.append("仅复核，不纳入内置源")

        nu, hd = norm_url(it["url"]), it["hash"]
        if nu in seen_url:
            reasons.append(f"URL 与「{seen_url[nu]}」重复")
            dup_log.append(("URL 重复", it["name"], seen_url[nu]))
        elif hd and hd in seen_hash:
            reasons.append(f"返回内容与「{seen_hash[hd]}」完全一致")
            dup_log.append(("内容重复", it["name"], seen_hash[hd]))
        else:
            seen_url.setdefault(nu, it["name"])
            if hd:
                seen_hash.setdefault(hd, it["name"])

        it["valid"] = not reasons
        it["reason"] = "；".join(reasons) if reasons else "可用"

    valid = [it for it in items.values() if it["valid"]]
    valid.sort(key=lambda x: x["e2e"])
    invalid = [it for it in items.values() if not it["valid"]]

    # 多仓线路内部重复 / 与候选池交叉重复统计
    cross = []
    pool = {norm_url(it["url"]): it["name"] for it in items.values()}
    for it in items.values():
        if it["kind"] != "多仓" or not it["data"]:
            continue
        urls = []
        for e in it["data"].get("urls") or []:
            u = (e.get("url") or e.get("api") or "") if isinstance(e, dict) else str(e)
            if u:
                urls.append(u)
        norms = [norm_url(u) for u in urls]
        inner = len(norms) - len(set(norms))
        overlap = [pool[n] for n in set(norms) if n in pool]
        cross.append((it["name"], len(urls), inner, overlap))
    return valid, invalid, dup_log, cross


def write_outputs(valid, invalid, dup_log, cross, rounds, proxy):
    final = [{"name": it["name"], "url": it["url"]} for it in valid]

    # 1) 聚合 JSON（只保留 TVBox 需要的 name/url）
    JSON_PATH.write_text(json.dumps({"urls": final}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 2) 同步 Java SOURCES
    java = JAVA_PATH.read_text(encoding="utf-8")
    rows = "".join(f'            {{"{x["name"]}", "{x["url"]}"}},\n' for x in final)
    pat = re.compile(r"(public static final String\[\]\[\] SOURCES = \{).*?(\n    \};)", re.S)
    new_java, n = pat.subn(lambda m: m.group(1) + "\n" + rows + "    };", java)
    if n != 1:
        raise RuntimeError("未能定位 BuiltinSources.java 的 SOURCES 定义块")
    JAVA_PATH.write_text(new_java, encoding="utf-8")

    # 3) 报告
    build_report(valid, invalid, dup_log, cross, rounds, proxy)
    return final


def build_report(valid, invalid, dup_log, cross, rounds, proxy):
    css = """
    :root{--bg:#f6f7f9;--card:#fff;--line:#e6e8ec;--text:#1f2329;--muted:#6b7280;
          --green:#067647;--green-bg:#ecfdf3;--red:#d92d20;--red-bg:#fef3f2;--blue:#175cd3;--blue-bg:#eff8ff}
    *{box-sizing:border-box}
    body{margin:0;padding:32px 20px 60px;background:var(--bg);color:var(--text);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.65}
    .wrap{max-width:1000px;margin:0 auto}
    h1{font-size:24px;margin:0 0 6px}h2{font-size:17px;margin:26px 0 12px}
    .sub{color:var(--muted);font-size:13px;margin-bottom:22px}
    .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin-bottom:16px}
    .sum{border-left:4px solid var(--blue);background:var(--blue-bg);border-radius:10px;padding:16px 20px;margin-bottom:18px;font-size:14px}
    .sum b{color:var(--blue)}
    table{width:100%;border-collapse:collapse;font-size:13px}
    th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}
    th{color:var(--muted);font-weight:600;background:#fafbfc}
    .mono,code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
    code{background:#f3f4f6;padding:1px 5px;border-radius:4px;word-break:break-all}
    .tag{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600;white-space:nowrap}
    .t-good{background:var(--green-bg);color:var(--green)}.t-bad{background:var(--red-bg);color:var(--red)}
    .t-info{background:var(--blue-bg);color:var(--blue)}
    ul{margin:8px 0 0;padding-left:20px;font-size:14px}li{margin:7px 0}
    .foot{color:var(--muted);font-size:12px;text-align:center;margin-top:26px}
    .small{color:var(--muted);font-size:12.5px}
    """

    def esc(s):
        return html.escape(str(s), quote=True)

    default_name = valid[0]["name"] if valid else "无"

    def speed_rows():
        rows = []
        for it in valid + invalid:
            times = "/".join(f"{t:.2f}s" for t in it["times"])
            tag = "t-good" if it["valid"] else "t-bad"
            label = "保留" if it["valid"] else "剔除"
            size = (f"{it['sites']} 站点" if it["kind"] == "单仓"
                    else f"{it['line_count']} 线路" if it["kind"] == "多仓" else "-")
            resource = (it.get("spider_info", "-") if it["kind"] == "单仓"
                        else it.get("sub_info", "-") if it["kind"] == "多仓" else "-")
            rows.append(
                f"<tr><td><b>{esc(it['name'])}</b><div class='small mono'>{esc(it['url'])}</div></td>"
                f"<td class='mono'>{esc(times)}</td>"
                f"<td class='mono'><b>{it['e2e']:.3f}s</b><div class='small'>入口 {it['median']:.3f}s</div></td>"
                f"<td>{esc(it['kind'])}<div class='small'>{esc(size)}</div></td>"
                f"<td class='small'>{esc(resource)}</td><td><span class='tag {tag}'>{label}</span>"
                f"<div class='small'>{esc(it['reason'])}</div></td></tr>")
        return "".join(rows)

    final_rows = "".join(
        f"<tr><td>{i}</td><td><b>{esc(it['name'])}</b>"
        + (" <span class='tag t-info'>默认</span>" if i == 1 else "") +
        f"</td><td class='mono'>{esc(it['url'])}</td><td>{esc(it['kind'])}</td>"
        f"<td class='mono'>{it['e2e']:.3f}s</td></tr>"
        for i, it in enumerate(valid, 1))

    invalid_rows = "".join(
        f"<li><b>{esc(it['name'])}</b> <span class='mono'>{esc(it['url'])}</span> —— {esc(it['reason'])}</li>"
        for it in invalid) or "<li>无</li>"

    dup_rows = "".join(
        f"<li>{esc(k)}：「{esc(a)}」与「{esc(b)}」</li>" for k, a, b in dup_log
    ) or "<li>候选池内未发现 URL 重复或返回内容完全相同的地址</li>"

    cross_rows = "".join(
        f"<li><b>{esc(name)}</b>：{total} 条线路，内部重复 {inner} 条"
        + (f"；与内置源候选重叠：{esc('、'.join(overlap))}" if overlap else "；与内置源候选无重叠")
        + "</li>" for name, total, inner, overlap in cross
    ) or "<li>无多仓配置</li>"

    doc = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>内置源测速去重报告</title>
<style>{css}</style></head><body><div class="wrap">
<h1>内置源测速 / 去重 / 失效剔除报告</h1>
<div class="sub">{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · 每地址 {rounds} 次请求取中位数 · {esc('直连优先，失败回退代理 ' + proxy) if proxy else '直连'}</div>
<div class="sum"><b>最终保留 {len(valid)} 条，剔除 {len(invalid)} 条。</b>
按端到端加载耗时升序排列（多仓含自动跳转到默认子线路的那一跳），默认源已更新为最快且直接可用的「{esc(default_name)}」；列表、Java 常量与聚合 JSON 已同步。</div>

<div class="card"><h2 style="margin-top:0">最终内置源顺序（快的优先）</h2>
<table><thead><tr><th style="width:40px">#</th><th style="width:140px">名称</th><th>配置地址</th>
<th style="width:90px">类型</th><th style="width:100px">端到端耗时</th></tr></thead>
<tbody>{final_rows}</tbody></table>
<p class="small" style="margin-top:12px">第一条即客户端首次启动自动加载的源；其余可在「线路选择」「历史配置」里切换。</p></div>

<div class="card"><h2 style="margin-top:0">测速与判定明细</h2>
<table><thead><tr><th style="width:28%">名称 / 地址</th><th style="width:130px">各次耗时</th>
<th style="width:100px">端到端</th><th style="width:100px">类型 / 规模</th>
<th style="width:130px">关键资源</th><th>结论</th></tr></thead>
<tbody>{speed_rows()}</tbody></table></div>

<div class="card"><h2 style="margin-top:0">去重检查</h2><ul>{dup_rows}</ul>
<h2>多仓线路重复 / 交叉重复统计</h2><ul>{cross_rows}</ul>
<p class="small">多仓入口保留为一条线路，不展开成扁平列表，避免把大量第三方子线路混进内置源；线路内部重复仅作统计提示。</p></div>

<div class="card"><h2 style="margin-top:0">剔除明细</h2><ul>{invalid_rows}</ul></div>

<div class="card"><h2 style="margin-top:0">判定规则与同步范围</h2><ul>
<li>每个地址连续请求 {rounds} 次，取成功请求的中位耗时；直连失败才回退代理。多仓额外计入「自动跳转到第一条子线路」的耗时，按端到端耗时排序，避免把多一跳的入口误判成最快。</li>
<li>有效条件：HTTP 200 + 可解析为 TVBox 单仓（sites/spider）或多仓（urls）配置 + 单仓 spider 可下载 / 多仓默认子线路必须有效且至少 1 条抽查线路可达。</li>
<li>去重：URL 归一化（协议与主机小写、去尾斜杠）去重，外加响应体 MD5 内容去重；两者任一命中即剔除较慢者。</li>
<li>已同步：<code>tools/builtin_sources.json</code>、<code>BuiltinSources.java</code> 的 <code>SOURCES</code>、本报告。</li>
<li>本机无 Android SDK，未编译 APK；第三方配置的长期可用性与内容合规性请自行判断。</li>
</ul></div>
<div class="foot">由 tools/optimize_builtin_sources.py 生成 · 可重复运行复测并重新排序</div>
</div></body></html>"""
    REPORT_PATH.write_text(doc, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()

    builtin, candidates = read_candidates()
    items = probe(candidates, args.proxy, args.rounds)
    check_resources(items, args.proxy)
    valid, invalid, dup_log, cross = analyze(items, {n for n, _ in builtin})
    final = write_outputs(valid, invalid, dup_log, cross, args.rounds, args.proxy)

    print(f"内置源优化完成（{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}，每地址 {args.rounds} 次测速）")
    for i, it in enumerate(valid, 1):
        print(f"  {i}. {it['name']:<14} 端到端 {it['e2e']:.3f}s (入口 {it['median']:.3f}s)  {it['kind']}  {it['reason']}")
    for it in invalid:
        print(f"  [剔除] {it['name']:<14} {it['reason']}")
    print(f"默认源: {final[0]['name'] if final else '无'}")
    print(f"已同步: {JSON_PATH.name}, {JAVA_PATH.name}, {REPORT_PATH.name}")


if __name__ == "__main__":
    raise SystemExit(
        "该脚本已废弃：内置源改为本地文件形式，Java 源码不再有 SOURCES 常量。\n"
        "请依次运行：\n"
        "  python3 tools/optimize_offline_sources.py\n"
        "  python3 tools/build_offline_sources.py"
    )
