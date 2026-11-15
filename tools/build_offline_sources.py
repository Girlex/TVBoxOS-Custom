#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线内置源 · 第三步：去重融合成「一个自己的仓库源」并打包进 APK。

读取 tools/offline_sources/optimized.json（已按多轮测速升序），
把 25 份配置的 sites 融合、按 key / (name,api) 去重，产出唯一一份自建配置：

  app/src/main/assets/builtin/
      collection.json       聚合入口（只有 1 条本地线路 = 自建融合源）
      manifest.json         元数据（sources / parts / live / online=[]）
      configs/fused.json    融合后的自建单仓配置
      jars/j1.jar ...       爬虫 jar（按 md5 复用；站点级 jar 也全部本地化）
      live/l1.txt ...       本地直播 M3U 列表

  tools/builtin_sources.json            聚合副本
  tools/builtin_sources_report.html     融合报告

去重与优先级（用户要求）：
  - 站点 key 重复、(name, api) 重复一律剔除，保留源站响应更快的那一份。
  - 站点按来源配置的端到端耗时升序排列，首页默认站点来自最快的源。
  - 每个站点显式带上本地 jar（TVBox 的 SourceBean.jar 支持站点级爬虫包），
    避免融合后不同来源的站点用错爬虫。
  - 聚合入口不含任何 http(s) 在线地址，在线备用线路数为 0。
"""
import hashlib
import html
import json
import pathlib
import shutil
import subprocess
from datetime import datetime
from encodings import idna
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = ROOT / "tools" / "offline_sources"
ASSETS = ROOT / "app" / "src" / "main" / "assets" / "builtin"

LOCAL_PREFIX = "clan://localhost/builtin/"
PROXY = "socks5h://127.0.0.1:10808"
UA = "okhttp/3.15"
PROXY_FIRST = ("raw.githubusercontent.com", "github.com", "gh-proxy.com",
               "cdn.jsdelivr.net", "gitee.com", "jihulab.com", "jsdelivr")


def esc(s):
    return html.escape(str(s), quote=True)


def normalize(url):
    p = urlsplit(url)
    labels = []
    for part in (p.hostname or "").split("."):
        if any(ord(c) > 127 for c in part):
            part = idna.ToASCII(part).decode("ascii")
        labels.append(part)
    host = ".".join(labels) + (":%d" % p.port if p.port else "")
    return urlunsplit((p.scheme, host, quote(p.path, safe="/%:=&?+,;@!$'()*-._~"),
                       p.query, ""))


def fetch(url, timeout=20):
    """境外域名优先代理；返回 {code, body}。"""
    real = normalize(url)
    order = [True, False] if any(h in url for h in PROXY_FIRST) else [False, True]
    best = None
    for use_proxy in order:
        cmd = ["curl", "-sS", "-L", "--max-time", str(timeout), "-A", UA]
        cmd += (["--proxy", PROXY] if use_proxy else ["--noproxy", "*"])
        cmd += ["-w", "\n__META__%{http_code}", real]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        except subprocess.TimeoutExpired:
            continue
        if b"__META__" not in r.stdout:
            continue
        body, meta = r.stdout.rsplit(b"\n__META__", 1)
        cur = {"code": int(meta.decode(errors="replace").strip() or 0), "body": body}
        if cur["code"] == 200 and body:
            return cur
        if best is None or cur["code"] > best["code"]:
            best = cur
    return best or {"code": 0, "body": b""}


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


def norm(s):
    return str(s or "").strip()


def merge_unique(list_of_lists, key_fields):
    seen, out = set(), []
    for lst in list_of_lists:
        for item in lst or []:
            if not isinstance(item, dict):
                continue
            k = tuple(norm(item.get(f)) for f in key_fields)
            if k in seen:
                continue
            seen.add(k)
            out.append(item)
    return out


def main():
    opt_path = BASE / "optimized.json"
    if not opt_path.exists():
        raise SystemExit("缺少 %s，请先运行 tools/optimize_offline_sources.py" % opt_path)
    opt = json.loads(opt_path.read_text(encoding="utf-8"))
    kept = opt["kept"]
    if not kept:
        raise SystemExit("optimized.json 中没有可用源")

    # 不整目录删除（避免批量删除保护）：先覆盖写需要的文件，最后只清理多余的旧文件
    (ASSETS / "configs").mkdir(parents=True, exist_ok=True)
    (ASSETS / "jars").mkdir(parents=True, exist_ok=True)
    (ASSETS / "live").mkdir(parents=True, exist_ok=True)

    jar_by_md5, jar_reuse = {}, 0

    def local_jar(jar_bytes):
        """按内容 md5 复用，返回 (本地 clan 地址, 文件名)。"""
        nonlocal jar_reuse
        md5 = hashlib.md5(jar_bytes).hexdigest()
        if md5 in jar_by_md5:
            jar_reuse += 1
            name = jar_by_md5[md5]
        else:
            name = "j%d.jar" % (len(jar_by_md5) + 1)
            jar_by_md5[md5] = name
            (ASSETS / "jars" / name).write_bytes(jar_bytes)
        return LOCAL_PREFIX + "jars/" + name + ";md5;" + md5, name

    # ---------- 1. 每份来源配置的主 jar 先落地 ----------
    parts = []
    for it in kept:
        cfg = json.loads((BASE / it["resolved_file"]).read_text(encoding="utf-8"))
        jar_bytes = (BASE / it["spider_file"]).read_bytes()
        if jar_bytes[:2] != b"PK":
            raise SystemExit("%s 的 spider 不是有效 jar/zip" % it["name"])
        main_jar, jar_name = local_jar(jar_bytes)
        parts.append({
            "name": it["name"], "origin": it["origin"], "group": it.get("group", ""),
            "resolved_url": it["resolved_url"], "cfg": cfg,
            "main_jar": main_jar, "jar_name": jar_name,
            "sites": len(cfg.get("sites") or []), "total_ms": it["total_ms"],
            "contributed": 0, "duplicated": 0, "skipped": 0,
        })
        print("[来源 %2d] %-16s %.3fs %4d 站点  jar %s" % (
            len(parts), it["name"], it["total_ms"], parts[-1]["sites"], jar_name), flush=True)

    # ---------- 2. 站点自带 jar（相对/在线）也下载并本地化 ----------
    special_urls = {}
    for p in parts:
        for s in p["cfg"].get("sites") or []:
            raw = norm(s.get("jar")).replace("img+", "")
            if raw:
                u = urljoin(p["resolved_url"], raw)
                # 图片隐写 jar 需要客户端专门解包，直接当 dex 加载会失败
                if urlsplit(u).path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                    continue
                special_urls.setdefault(u, None)
    special_local, special_failed = {}, 0
    for i, u in enumerate(sorted(special_urls), 1):
        r = fetch(u, timeout=20)
        if r["code"] == 200 and r["body"][:2] == b"PK":
            special_local[u] = local_jar(r["body"])[0]
            print("[站点jar %2d/%2d] OK   %6d KB  %s" % (
                i, len(special_urls), len(r["body"]) // 1024, u[:90]), flush=True)
        else:
            special_failed += 1
            print("[站点jar %2d/%2d] 剔除 HTTP %s  %s" % (
                i, len(special_urls), r["code"], u[:90]), flush=True)

    # ---------- 3. 融合去重：按测速升序遍历，快者优先 ----------
    fused_sites, seen_key, seen_pair = [], set(), set()
    for p in parts:
        for s in p["cfg"].get("sites") or []:
            if not isinstance(s, dict) or not all(k in s for k in ("key", "type", "api")):
                p["skipped"] += 1  # 与 ApiConfig 一致：缺 key/type/api 的站点客户端也会跳过
                continue
            key, name, api = norm(s.get("key")), norm(s.get("name")), norm(s.get("api"))
            pair = (name, api.rstrip("/"))
            if key in seen_key or pair in seen_pair:
                p["duplicated"] += 1
                continue
            seen_key.add(key)
            seen_pair.add(pair)
            site = rewrite_relative(dict(s), p["resolved_url"])
            raw_jar = norm(s.get("jar")).replace("img+", "")
            jar_url = urljoin(p["resolved_url"], raw_jar) if raw_jar else ""
            # 每个站点显式绑定自己来源的本地 jar，融合后不会用错爬虫
            site["jar"] = special_local.get(jar_url, p["main_jar"])
            fused_sites.append(site)
            p["contributed"] += 1

    # ---------- 4. 顶层元数据：以最快来源为底，合并解析/标识 ----------
    base_cfg = parts[0]["cfg"]
    meta_keys = ("wallpaper", "logo", "doh", "ads", "ijk", "proxy", "rules", "hosts")
    fused = {k: base_cfg[k] for k in meta_keys if k in base_cfg}
    fused["spider"] = parts[0]["main_jar"]
    fused["sites"] = fused_sites
    parses = merge_unique([p["cfg"].get("parses") for p in parts], ("name", "url"))
    if parses:
        fused["parses"] = parses
    flags = []
    for p in parts:
        for f in p["cfg"].get("flags") or []:
            if f not in flags:
                flags.append(f)
    if flags:
        fused["flags"] = flags
    fused = rewrite_relative(fused, parts[0]["resolved_url"], skip_key="jar")
    (ASSETS / "configs" / "fused.json").write_text(
        json.dumps(fused, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n融合完成：%d 个站点 → 保留 %d 个，剔除重复 %d 个" % (
        sum(p["sites"] for p in parts), len(fused_sites),
        sum(p["duplicated"] for p in parts)), flush=True)

    # ---------- 5. 直播列表本地化 ----------
    live = []
    for x in opt.get("live", []):
        body = (BASE / x["raw"]).read_bytes()
        filename = x["local"].split("/")[-1]
        (ASSETS / "live" / filename).write_bytes(body)
        live.append({
            "name": x["name"], "origin": x["url"], "local": x["local"],
            "file": "live/" + filename, "size": len(body),
            "ms": x.get("ms", 0), "md5": hashlib.md5(body).hexdigest(),
        })

    # 清掉上一轮残留、但本轮不再引用的文件（数量很少，不做整目录删除）
    keep_jars = set(jar_by_md5.values())
    for p in (ASSETS / "jars").glob("*.jar"):
        if p.name not in keep_jars:
            p.unlink()
    for p in (ASSETS / "configs").glob("*.json"):
        if p.name != "fused.json":
            p.unlink()
    keep_live = {x["file"].split("/")[-1] for x in live}
    for p in (ASSETS / "live").glob("*"):
        if p.is_file() and p.name not in keep_live:
            p.unlink()

    # ---------- 6. 聚合入口：唯一一条本地融合源 ----------
    collection = {"urls": [
        {"name": "本地·自建融合源", "url": LOCAL_PREFIX + "configs/fused.json"}
    ]}
    collection_text = json.dumps(collection, ensure_ascii=False, indent=2)
    (ASSETS / "collection.json").write_text(collection_text, encoding="utf-8")
    (ROOT / "tools" / "builtin_sources.json").write_text(collection_text, encoding="utf-8")

    manifest = {
        "version": 3,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "default": "clan://localhost/builtin.json",
        "default_live": live[0]["local"] if live else "clan://localhost/builtin.json",
        "sources": [{
            "name": "自建融合源",
            "origin": "融合 %d 份本地快照" % len(parts),
            "local": LOCAL_PREFIX + "configs/fused.json",
            "config": "configs/fused.json",
            "sites": len(fused_sites),
            "total_ms": parts[0]["total_ms"],
        }],
        "parts": [{k: v for k, v in p.items() if k != "cfg"} for p in parts],
        "live": live,
        "online": [],
        "dedup": {
            "raw_sites": sum(p["sites"] for p in parts),
            "kept_sites": len(fused_sites),
            "removed": sum(p["duplicated"] for p in parts),
            "skipped": sum(p["skipped"] for p in parts),
            "site_jars_local": len(special_local),
            "site_jars_dropped": special_failed,
        },
    }
    (ASSETS / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 7. 校验 ----------
    problems = []
    for u in collection["urls"]:
        if not u["url"].startswith(LOCAL_PREFIX):
            problems.append("聚合入口仍含在线地址：%s" % u["url"])
        if not (ASSETS / u["url"][len(LOCAL_PREFIX):]).is_file():
            problems.append("融合配置不存在")
    cfg = json.loads((ASSETS / "configs/fused.json").read_text(encoding="utf-8"))
    if not str(cfg.get("spider", "")).startswith(LOCAL_PREFIX):
        problems.append("顶层 spider 未本地化")
    site_jars, keys, pairs = set(), set(), set()
    for s in cfg["sites"]:
        k = norm(s.get("key"))
        pair = (norm(s.get("name")), norm(s.get("api")).rstrip("/"))
        if k in keys or pair in pairs:
            problems.append("融合配置仍有重复站点：%s" % k)
        keys.add(k)
        pairs.add(pair)
        j = str(s.get("jar", ""))
        if not j.startswith(LOCAL_PREFIX):
            problems.append("站点 %s 的 jar 未本地化" % k)
        site_jars.add(j)
        jar_rel, _, declared = j[len(LOCAL_PREFIX):].partition(";md5;")
        jar = ASSETS / jar_rel
        if not jar.is_file() or jar.read_bytes()[:2] != b"PK" \
                or hashlib.md5(jar.read_bytes()).hexdigest() != declared:
            problems.append("站点 %s 的 jar 无效" % k)
    for x in live:
        if not (ASSETS / x["file"]).is_file():
            problems.append("直播文件不存在：%s" % x["file"])
    if manifest["online"]:
        problems.append("manifest 仍含在线备用线路")
    if len(collection["urls"]) != 1:
        problems.append("聚合入口不是唯一一条融合源")

    files = [p for p in ASSETS.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    print("assets 共 %d 个文件，%.2f MB；唯一 jar %d 个（复用 %d 次）" % (
        len(files), total / 1048576, len(jar_by_md5), jar_reuse))
    print("校验:", "全部通过" if not problems else "发现问题 -> " + "; ".join(problems[:6]), flush=True)

    # ---------- 8. 报告 ----------
    rows = "".join(
        "<tr><td>%d</td><td><b>%s</b></td><td class='mono'>%s</td>"
        "<td class='mono'>%.3fs</td><td>%d</td><td class='ok'>+%d</td>"
        "<td class='del'>-%d</td></tr>" % (
            i, esc(p["name"]), esc(p["group"]), p["total_ms"], p["sites"],
            p["contributed"], p["duplicated"])
        for i, p in enumerate(parts, 1))
    live_rows = "".join(
        "<tr><td>%d%s</td><td>%s</td><td class='mono'>%s</td>"
        "<td class='mono'>%d KB</td><td class='mono'>%.3fs</td></tr>" % (
            i, " <b>(默认)</b>" if i == 1 else "", esc(x["name"]),
            esc(x["local"]), x["size"] // 1024, x["ms"])
        for i, x in enumerate(live, 1))
    report = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>TVBox 自建融合源</title><style>
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f6f7f9;color:#1f2329;margin:0;padding:32px}
.wrap{max-width:1180px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:28px 32px}
h1{font-size:22px;margin:0 0 6px}h2{font-size:16px;margin:26px 0 10px}
p,li{color:#5f6672;font-size:13px;line-height:1.85}
table{width:100%%;border-collapse:collapse;font-size:13px}th,td{padding:9px 10px;border-bottom:1px solid #eef0f3;text-align:left}
th{background:#f3f5f8;font-weight:600;color:#42474f}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:#4a5160;word-break:break-all}
.ok{color:#0a8f4a}.del{color:#c0392b}
code{background:#f2f4f7;padding:2px 5px;border-radius:4px;font-size:12px}
.box{background:#f7f9fc;border-left:4px solid #3b7cff;padding:12px 16px;border-radius:0 8px 8px 0;margin:14px 0}
</style></head><body><div class="wrap">
<h1>TVBox 自建融合源 · 全本地</h1>
<p>生成时间 %s ｜ %d 份来源配置 → <b>1 份自建融合源</b>（%d 个站点）｜ %d 个本地直播列表 ｜ <b>在线备用 0 条</b> ｜ assets %.2f MB</p>
<div class="box"><b>本版变化：</b>把过筛后的 %d 份配置<strong>融合成一个自己的仓库源</strong>：
按 <code>key</code> 和 <code>(name, api)</code> 双重去重，剔除重复站点 <b>%d 个</b>、跳过缺字段站点 %d 个（%d → %d）；
来源按多轮测速升序遍历，<strong>同站点保留响应最快的那份</strong>，融合配置内站点也按此顺序排列，首页默认站点来自最快的源。
聚合入口只剩一条 <code>clan://localhost/builtin/configs/fused.json</code>，不含任何 http(s) 地址。</div>
<h2>来源贡献明细（按端到端耗时升序 = 融合时的优先级）</h2>
<table><thead><tr><th>#</th><th>来源</th><th>组别</th><th>端到端</th><th>原站点</th><th>融入</th><th>重复剔除</th></tr></thead>
<tbody>%s</tbody></table>
<h2>直播列表（本地文件，默认第一条）</h2>
<table><thead><tr><th>#</th><th>名称</th><th>本地地址</th><th>大小</th><th>源站耗时</th></tr></thead>
<tbody>%s</tbody></table>
<h2>实现细节</h2>
<ul>
<li>TVBox 的 <code>SourceBean.jar</code> 支持站点级爬虫包，因此每个融合站点都显式绑定<strong>自己来源的本地 jar</strong>，不会用错爬虫；顶层 <code>spider</code> 取最快来源的 jar 作为兜底。</li>
<li>原本写在站点里的在线/相对 jar 已尝试下载并本地化：成功 %d 个，无法下载（非 jar）已移除 %d 个，移除后回退到来源主 jar；jar 按内容 md5 复用，唯一 %d 个、复用 %d 次。</li>
<li>本地服务返回文本时把 <code>clan://localhost/</code> 替换成真实的 <code>http://127.0.0.1:&lt;port&gt;/file/</code>；<code>.txt/.m3u</code> 按纯文本返回。</li>
<li><code>parses</code> / <code>flags</code> 按名称去重合并，其余顶层元数据取最快来源；配置里的 <code>./</code> 相对资源改写成原站绝对地址。</li>
<li>默认点播 <code>clan://localhost/builtin.json</code>，默认直播 <code>%s</code>。点播内容本身需要联网，这里解决的是「配置接口 / 线路列表失效」。</li>
<li>更新快照：依次运行 <code>snapshot_repo_sources.py</code> → <code>optimize_offline_sources.py</code> → <code>build_offline_sources.py</code> 再编译；也可把同名文件放到 <code>/sdcard/builtin/</code> 覆盖。</li>
</ul>
<h2>校验结果</h2><p>%s</p>
</div></body></html>""" % (
        esc(manifest["generated"]), len(parts), len(fused_sites), len(live), total / 1048576,
        len(parts), manifest["dedup"]["removed"], manifest["dedup"]["skipped"],
        manifest["dedup"]["raw_sites"], len(fused_sites), rows, live_rows,
        len(special_local), special_failed, len(jar_by_md5), jar_reuse,
        esc(manifest["default_live"]),
        esc("全部通过" if not problems else "；".join(problems)))
    (ROOT / "tools" / "builtin_sources_report.html").write_text(report, encoding="utf-8")
    print("产出：app/src/main/assets/builtin/、tools/builtin_sources.json、tools/builtin_sources_report.html")
    if problems:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
