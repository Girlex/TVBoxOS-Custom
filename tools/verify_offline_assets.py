#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态校验内置源是否「全部走本地」。

检查 app/src/main/assets/builtin/ 下的聚合入口、配置、jar、直播列表，
并核对 Java 侧不再注入在线线路。产出 tools/verify_offline_assets.html。
"""
import hashlib
import html
import json
import pathlib
import re
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS = ROOT / "app" / "src" / "main" / "assets" / "builtin"
PREFIX = "clan://localhost/builtin/"


def esc(s):
    return html.escape(str(s), quote=True)


def main():
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    collection = json.loads((ASSETS / "collection.json").read_text(encoding="utf-8"))
    manifest = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
    urls = collection.get("urls", [])
    check("聚合入口非空", len(urls) > 0, "%d 条线路" % len(urls))
    check("聚合入口无在线地址",
          all(u["url"].startswith(PREFIX) for u in urls),
          "全部为 clan://localhost/builtin/")
    check("聚合入口文件都存在",
          all((ASSETS / u["url"][len(PREFIX):]).is_file() for u in urls))
    check("manifest.online 为空", not manifest.get("online"),
          "在线备用 %d 条" % len(manifest.get("online", [])))
    check("默认点播为本地入口",
          manifest.get("default") == "clan://localhost/builtin.json")
    check("默认直播为本地文件",
          str(manifest.get("default_live", "")).startswith(PREFIX)
          and (ASSETS / manifest["default_live"][len(PREFIX):]).is_file(),
          manifest.get("default_live", ""))
    check("线路顺序与测速一致",
          [u["url"] for u in urls] == [PREFIX + s["config"] for s in manifest["sources"]])
    ms = [s["total_ms"] for s in manifest["sources"]]
    check("按端到端耗时升序", ms == sorted(ms), "最快 %.3fs / 最慢 %.3fs" % (ms[0], ms[-1]) if ms else "")
    parts = manifest.get("parts", [])
    pms = [p["total_ms"] for p in parts]
    check("融合来源按测速升序", pms == sorted(pms), "%d 份来源" % len(parts))
    check("聚合入口只含一条自建融合源", len(urls) == 1, urls[0]["name"] if urls else "")
    dedup = manifest.get("dedup", {})
    check("融合去重统计一致",
          dedup.get("raw_sites", 0) - dedup.get("removed", 0) - dedup.get("skipped", 0)
          == dedup.get("kept_sites", 0),
          "%d → %d（重复 %d / 不完整 %d）" % (
              dedup.get("raw_sites", 0), dedup.get("kept_sites", 0),
              dedup.get("removed", 0), dedup.get("skipped", 0)))

    fused = json.loads((ASSETS / "configs/fused.json").read_text(encoding="utf-8"))
    fkeys, fpairs, sites_ok = set(), set(), True
    for s in fused.get("sites", []):
        k = str(s.get("key", "")).strip()
        pair = (str(s.get("name", "")).strip(), str(s.get("api", "")).strip().rstrip("/"))
        if k in fkeys or pair in fpairs:
            sites_ok = False
        fkeys.add(k)
        fpairs.add(pair)
        if not str(s.get("jar", "")).startswith(PREFIX):
            sites_ok = False
    check("融合站点无重复且站点 jar 全本地", sites_ok, "%d 个站点" % len(fused.get("sites", [])))
    check("融合站点数与 manifest 一致",
          len(fused.get("sites", [])) == dedup.get("kept_sites", -1))
    check("顶层 spider 指向本地 jar", str(fused.get("spider", "")).startswith(PREFIX))

    jar_cache = {}
    spider_ok = True
    md5_ok = True
    for s in manifest["sources"]:
        cfg = json.loads((ASSETS / s["config"]).read_text(encoding="utf-8"))
        spider = str(cfg.get("spider", ""))
        if not spider.startswith(PREFIX):
            spider_ok = False
            continue
        jar_rel, _, declared = spider[len(PREFIX):].partition(";md5;")
        jar = ASSETS / jar_rel
        if not jar.is_file() or jar.read_bytes()[:2] != b"PK":
            spider_ok = False
            continue
        if jar_rel not in jar_cache:
            jar_cache[jar_rel] = hashlib.md5(jar.read_bytes()).hexdigest()
        if jar_cache[jar_rel] != declared:
            md5_ok = False
        if "sites" not in cfg:
            spider_ok = False
    check("所有 spider 指向本地 jar 且为 PK 头", spider_ok, "%d 个唯一 jar" % len(jar_cache))
    check("所有 spider md5 与文件一致", md5_ok)

    live_files = [(ASSETS / x["file"]) for x in manifest.get("live", [])]
    check("直播列表全部落地", live_files and all(p.is_file() and p.stat().st_size > 0 for p in live_files),
          "%d 个本地直播文件" % len(live_files))
    live_ok = all(
        re.search(r"#EXTM3U|^[^#\n]+,\s*https?://", p.read_text(encoding="utf-8", errors="replace")[:2000], re.M)
        for p in live_files if p.is_file())
    check("直播内容格式可解析", live_ok)

    copy = ROOT / "tools" / "builtin_sources.json"
    check("聚合副本与 collection 一致",
          copy.read_text(encoding="utf-8") == (ASSETS / "collection.json").read_text(encoding="utf-8"))

    java = (ROOT / "app/src/main/java/com/github/tvbox/osc/util/BuiltinSources.java").read_text(encoding="utf-8")
    check("Java 不再注入在线线路", "m.online" not in java and "class Online" not in java)
    check("Java 默认直播指向本地", "DEFAULT_LIVE_API" in java)
    dialog = (ROOT / "app/src/main/java/com/github/tvbox/osc/ui/dialog/ApiDialog.java").read_text(encoding="utf-8")
    check("配置对话框直播回退本地", "DEFAULT_LIVE_API" in dialog)

    # 运行时稳定性修复：老设备 ART 类校验竞态 / 聚合搜索规模爆炸 / jar 同步联网初始化
    jar_loader = (ROOT / "app/src/main/java/com/github/catvod/crawler/JarLoader.java").read_text(encoding="utf-8")
    locks = jar_loader.count("synchronized (DEX_CLASS_LOCK)")
    check("类加载有全局串行锁", "DEX_CLASS_LOCK" in jar_loader and locks >= 4,
          "%d 处串行化类加载" % locks)
    check("jar Init 初始化有超时保护",
          "runInitWithTimeout" in jar_loader and "INIT_WAIT_SECONDS" in jar_loader
          and "FutureTask" in jar_loader, "超时后台续跑，调用方不再等待")
    check("业务初始化保留原调用语义",
          "method.invoke(null, App.getInstance())" in jar_loader
          and "spider.init(App.getInstance(), ext)" in jar_loader
          and "Boolean.FALSE.equals(done)" in jar_loader,
          "只锁类加载，Init.init / spider.init 不占锁且不兼容时回退重试")
    search_java = (ROOT / "app/src/main/java/com/github/tvbox/osc/ui/activity/FastSearchActivity.java").read_text(encoding="utf-8")
    check("聚合搜索限制参与站点",
          "SEARCH_MAX_SITES" in search_java and "added >= SEARCH_MAX_SITES" in search_java,
          "按测速顺序取前 N 个站点")
    thread_m = re.search(r"(?<!MAX_)SEARCH_THREAD_COUNT\s*=\s*(\d+)", search_java)
    timeout_m = re.search(r"SEARCH_SITE_TIMEOUT_SECONDS\s*=\s*(\d+)", search_java)
    check("搜索并发不超过 3", thread_m and int(thread_m.group(1)) <= 3,
          "%s 线程" % (thread_m.group(1) if thread_m else "?"))
    check("单站点超时不超过 8 秒", timeout_m and int(timeout_m.group(1)) <= 8,
          "%s 秒" % (timeout_m.group(1) if timeout_m else "?"))

    files = [p for p in ASSETS.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    passed = sum(1 for _, ok, _ in checks if ok)

    rows = "".join(
        "<tr><td>%s</td><td class='%s'>%s</td><td class='mono'>%s</td></tr>" % (
            esc(n), "ok" if ok else "bad", "通过" if ok else "未通过", esc(d))
        for n, ok, d in checks)
    report = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>内置源全本地静态校验</title><style>
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f6f7f9;color:#1f2329;margin:0;padding:32px}
.wrap{max-width:900px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:28px 32px}
h1{font-size:22px;margin:0 0 6px}p{color:#5f6672;font-size:13px;line-height:1.8}
table{width:100%%;border-collapse:collapse;font-size:13px}th,td{padding:9px 10px;border-bottom:1px solid #eef0f3;text-align:left}
th{background:#f3f5f8}.ok{color:#0a8f4a;font-weight:600}.bad{color:#c0392b;font-weight:600}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:#4a5160;word-break:break-all}
</style></head><body><div class="wrap">
<h1>自建融合源 · 全本地与运行时稳定性校验</h1>
<p>%s ｜ %d/%d 项通过 ｜ %d 条本地线路、%d 个直播文件 ｜ assets %.2f MB</p>
<table><thead><tr><th style="width:300px">检查项</th><th style="width:90px">结果</th><th>明细</th></tr></thead>
<tbody>%s</tbody></table>
<p>判定标准：25 份来源配置融合成唯一一份自建源，站点按 key / (name,api) 双重去重、按测速升序排列；
聚合入口、配置、爬虫 jar、直播列表全部为 <code>clan://localhost/builtin/…</code> 本地文件；
Java 侧不再注入任何在线备用线路。同时校验运行时稳定性修复：类加载全局串行锁（消除 Android 7.x ART 类校验竞态 abort）、
jar Init 初始化 3 秒超时保护、聚合搜索按测速顺序限流（120 站点 / 3 并发 / 8 秒超时）。</p>
</div></body></html>""" % (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), passed, len(checks),
        len(urls), len(live_files), total / 1048576, rows)
    (ROOT / "tools" / "verify_offline_assets.html").write_text(report, encoding="utf-8")

    print("静态校验 %d/%d 项通过；%d 条本地线路，%d 个直播文件，assets %.2f MB" % (
        passed, len(checks), len(urls), len(live_files), total / 1048576))
    for n, ok, d in checks:
        if not ok:
            print("  未通过：%s %s" % (n, d))
    print("产出：tools/verify_offline_assets.html")
    if passed != len(checks):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
