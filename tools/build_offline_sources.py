#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把整理好的配置合并成随 APK 打包的「本地文件」内置源。

读取 tools/offline_sources/optimized.json，产出：

  app/src/main/assets/builtin/
      collection.json       聚合入口（本地线路 + 在线备用线路）
      manifest.json         元数据（供 App 读取、供报告使用）
      configs/c1.json ...   规范化配置，spider 已指向本地 jar
      jars/j1.jar ...       爬虫 jar（已下载校验）

  tools/builtin_sources.json            可直接托管的聚合副本
  tools/builtin_sources_report.html     离线内置源报告

设计要点：客户端只访问 clan://localhost/builtin/...，由 RemoteServer 从
assets（或外置覆盖文件）返回，因此外部配置 URL 失效仍能正常加载。
站点内部的 ./xxx 相对资源改写成原站绝对地址，避免指向不存在的本地路径。
"""
import hashlib
import html
import json
import pathlib
import shutil
from datetime import datetime
from urllib.parse import urljoin

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = ROOT / "tools" / "offline_sources"
ASSETS = ROOT / "app" / "src" / "main" / "assets" / "builtin"

LOCAL_PREFIX = "clan://localhost/builtin/"


def esc(s):
    return html.escape(str(s), quote=True)


def rewrite_relative(node, base_url, skip_key=None):
    """把以 ./ 或 ../ 开头的字符串资源改写成基于原站的绝对地址。"""
    if isinstance(node, dict):
        return {k: (v if k == skip_key else rewrite_relative(v, base_url, skip_key))
                for k, v in node.items()}
    if isinstance(node, list):
        return [rewrite_relative(v, base_url, skip_key) for v in node]
    if isinstance(node, str):
        s = node.strip()
        if s.startswith("./") or s.startswith("../"):
            return urljoin(base_url, s)
    return node


def main():
    opt_path = BASE / "optimized.json"
    if not opt_path.exists():
        raise SystemExit("缺少 %s，请先运行 tools/optimize_offline_sources.py" % opt_path)
    opt = json.loads(opt_path.read_text(encoding="utf-8"))
    kept = opt["kept"]
    if not kept:
        raise SystemExit("optimized.json 中没有可用源")

    shutil.rmtree(ASSETS, ignore_errors=True)
    (ASSETS / "configs").mkdir(parents=True, exist_ok=True)
    (ASSETS / "jars").mkdir(parents=True, exist_ok=True)

    sources = []
    for i, it in enumerate(kept, 1):
        cfg = json.loads((BASE / it["resolved_file"]).read_text(encoding="utf-8"))
        jar_bytes = (BASE / it["spider_file"]).read_bytes()
        if jar_bytes[:2] != b"PK":
            raise SystemExit("%s 的 spider 不是有效 jar/zip" % it["name"])

        jar_name = "j%d.jar" % i
        cfg_name = "c%d.json" % i
        jar_md5 = hashlib.md5(jar_bytes).hexdigest()

        # spider 指向本地文件；RemoteServer 返回配置时会把 clan://localhost/
        # 动态替换成真实的 http://127.0.0.1:<port>/file/ 前缀。
        cfg["spider"] = LOCAL_PREFIX + "jars/" + jar_name + ";md5;" + jar_md5
        # 其余 ./xxx 相对资源改写成原站绝对地址（保持在线时站点功能完整）
        cfg = rewrite_relative(cfg, it["resolved_url"], skip_key="spider")

        (ASSETS / "configs" / cfg_name).write_text(
            json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
        (ASSETS / "jars" / jar_name).write_bytes(jar_bytes)

        sources.append({
            "name": it["name"],
            "origin": it["origin"],
            "resolved_url": it["resolved_url"],
            "base_url": it["resolved_url"],
            "local": LOCAL_PREFIX + "configs/" + cfg_name,
            "config": "configs/" + cfg_name,
            "jar": "jars/" + jar_name,
            "jar_md5": jar_md5,
            "jar_size": len(jar_bytes),
            "sites": it["sites"],
            "total_ms": it["total_ms"],
        })
        print("[%d] %-14s %-16s %4d 站点  jar %6d KB  -> %s" % (
            i, it["name"], "%.3fs" % it["total_ms"], it["sites"],
            len(jar_bytes) // 1024, cfg_name))

    online = [{"name": o["name"], "url": o["url"], "total_ms": o["total_ms"]}
              for o in opt["online"]]

    collection = {"urls": (
        [{"name": "本地·" + s["name"], "url": s["local"]} for s in sources] +
        [{"name": o["name"], "url": o["url"]} for o in online]
    )}
    collection_text = json.dumps(collection, ensure_ascii=False, indent=2)
    (ASSETS / "collection.json").write_text(collection_text, encoding="utf-8")
    (ROOT / "tools" / "builtin_sources.json").write_text(collection_text, encoding="utf-8")

    manifest = {
        "version": 1,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "default": "clan://localhost/builtin.json",
        "sources": sources,
        "online": online,
    }
    (ASSETS / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 校验 ----------
    problems = []
    for s in sources:
        cfg = json.loads((ASSETS / s["config"]).read_text(encoding="utf-8"))
        if not str(cfg.get("spider", "")).startswith(LOCAL_PREFIX):
            problems.append("%s 的 spider 未本地化" % s["name"])
        if "sites" not in cfg:
            problems.append("%s 缺少 sites" % s["name"])
        if (ASSETS / s["jar"]).read_bytes()[:2] != b"PK":
            problems.append("%s 的 jar 损坏" % s["name"])
        text = (ASSETS / s["config"]).read_text(encoding="utf-8")
        if '"' + "./" in text or '"' + "../" in text:
            problems.append("%s 仍残留相对路径" % s["name"])
    if len(collection["urls"]) != len(sources) + len(online):
        problems.append("聚合线路数量不符")

    total = sum(p.stat().st_size for p in ASSETS.rglob("*") if p.is_file())
    print("\nassets 共 %d 个文件，%.2f MB" % (
        len([p for p in ASSETS.rglob("*") if p.is_file()]), total / 1048576))
    print("校验:", "全部通过" if not problems else "发现问题 -> " + "; ".join(problems))

    # ---------- 报告 ----------
    rows = []
    for i, s in enumerate(sources, 1):
        rows.append(
            "<tr><td>%d</td><td><b>本地·%s</b></td><td class='mono'>%s</td>"
            "<td class='mono'>%s</td><td>%d</td><td class='mono'>%.3fs</td>"
            "<td class='mono'>%d KB</td><td class='ok'>离线可用</td></tr>" % (
                i, esc(s["name"]), esc(s["local"]), esc(s["config"]),
                s["sites"], s["total_ms"], s["jar_size"] // 1024))
    for o in online:
        rows.append(
            "<tr class='online'><td>—</td><td>%s</td><td class='mono'>%s</td>"
            "<td class='mono'>—</td><td>—</td><td class='mono'>%.3fs</td>"
            "<td class='mono'>—</td><td>在线备用</td></tr>" % (
                esc(o["name"]), esc(o["url"]), o["total_ms"]))
    report = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>TVBox 离线内置源</title><style>
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f6f7f9;color:#1f2329;margin:0;padding:32px}
.wrap{max-width:1150px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:28px 32px}
h1{font-size:22px;margin:0 0 6px}h2{font-size:16px;margin:26px 0 10px}
p,li{color:#5f6672;font-size:13px;line-height:1.85}
table{width:100%%;border-collapse:collapse;font-size:13px}th,td{padding:9px 10px;border-bottom:1px solid #eef0f3;text-align:left}
th{background:#f3f5f8;font-weight:600;color:#42474f}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:#4a5160;word-break:break-all}
.ok{color:#0a8f4a}.online{background:#fbfcfe}
code{background:#f2f4f7;padding:2px 5px;border-radius:4px;font-size:12px}
.box{background:#f7f9fc;border-left:4px solid #3b7cff;padding:12px 16px;border-radius:0 8px 8px 0;margin:14px 0}
</style></head><body><div class="wrap">
<h1>TVBox 离线内置源</h1>
<p>生成时间 %s ｜ %d 条本地线路 + %d 条在线备用 ｜ assets 合计 %.2f MB</p>
<div class="box"><b>和之前的区别：</b>不是内置一串外网 URL，而是把每个配置的<strong>完整内容</strong>与
<strong>爬虫 jar</strong>一起打进 APK。客户端访问 <code>clan://localhost/builtin/…</code>，
由 App 自带本地服务从 assets 返回，<strong>原配置地址失效也能正常加载</strong>。</div>
<h2>线路列表（默认第一条）</h2>
<table><thead><tr><th>#</th><th>名称</th><th>线路地址</th><th>本地文件</th><th>站点</th><th>端到端</th><th>spider</th><th>说明</th></tr></thead>
<tbody>%s</tbody></table>
<h2>实现细节</h2>
<ul>
<li>多仓入口已展开到默认子线路，全部落地成可直接加载的单仓配置（含 <code>sites</code>）。</li>
<li>每条配置的 <code>spider</code> 指向 assets 内的 jar；本地服务返回配置时把 <code>clan://localhost/</code> 替换成真实的 <code>http://127.0.0.1:&lt;port&gt;/file/</code>，端口变化也不会失效。</li>
<li>配置里原有的 <code>./libs/…</code> 等相对资源已改写成原站绝对地址，在线时站点功能完整；点播源本身需要联网，所以这里解决的是「配置接口失效」而非完全断网可用。</li>
<li>配置里保留的 <code>file://</code> 引用（如小马的可选 <code>TV/alist.json</code>）是 TVBox 自带的本地文件协议，属原配置合法写法，不影响其它站点。</li>
<li>想更新快照：重跑 <code>optimize_offline_sources.py</code> + <code>build_offline_sources.py</code> 再编译；也可把同名文件放到手机存储 <code>/sdcard/builtin/</code> 下覆盖，无需重新编译。</li>
</ul>
<h2>校验结果</h2><p>%s</p>
</div></body></html>""" % (
        esc(manifest["generated"]), len(sources), len(online), total / 1048576,
        "".join(rows), esc("全部通过" if not problems else "；".join(problems)))
    (ROOT / "tools" / "builtin_sources_report.html").write_text(report, encoding="utf-8")
    print("产出：app/src/main/assets/builtin/、tools/builtin_sources.json、tools/builtin_sources_report.html")
    if problems:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
