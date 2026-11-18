#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 good.json（213 个精选影视站点）本地化成 APK 内置源。

产出与现有 builtin 完全一致的结构：
  app/src/main/assets/builtin/
      collection.json       入口（只 1 条本地线路）
      manifest.json         元数据
      configs/fused.json    213 站点的本地配置（jar 全部改成 clan 本地地址）
      jars/jN.jar           站点依赖的 jar（按 md5 复用下载）
      live/lN.txt           本地直播列表

同时：
  - 生成可直接 adb push 的副本 tools/my_api/local_builtin/builtin/...
  - jar 下载失败/非 PK 的站点剔除（不留在线地址，保证完全本地）
  - 顶层 spider 取覆盖站点最多的那个本地 jar

用法：
    python3 tools/build_local_good.py                 # 从 good.json 生成本地源
    python3 tools/build_local_good.py --dry-run       # 只分析不写文件
    python3 tools/build_local_good.py --no-download   # 复用已下载 jar（调试）
"""
import argparse
import hashlib
import html
import json
import pathlib
import re
import subprocess
import zipfile
from collections import Counter
from datetime import datetime, timezone
from encodings import idna
from urllib.parse import urlsplit, urlunsplit, quote

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "my_api" / "good.json"
ASSETS = ROOT / "app" / "src" / "main" / "assets" / "builtin"
COPY = ROOT / "tools" / "my_api" / "local_builtin" / "builtin"
LOCAL_PREFIX = "clan://localhost/builtin/"
PROXY = "socks5h://127.0.0.1:10808"
UA = "okhttp/3.15"
PROXY_FIRST = ("raw.githubusercontent.com", "github.com", "gh-proxy.com",
               "cdn.jsdelivr.net", "gitee.com", "jihulab.com", "jsdelivr",
               "ghfast.top", "catbox.moe")
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
# 可作为脚本本地化的后缀（JS/py 爬虫，以及 ext 里的 json/js 规则）
SCRIPT_EXT = (".js", ".py", ".json")


def esc(s):
    return html.escape(str(s), quote=True)


def norm(s):
    return str(s or "").strip()


def valid_jar(body):
    """jar 必须是能读出 classes.dex 的有效 zip（有些下载回来的 PK 开头但中央目录损坏）。"""
    if not body or body[:2] != b"PK":
        return False
    try:
        import io
        z = zipfile.ZipFile(io.BytesIO(body))
        names = z.namelist()
        return any(n.endswith(".dex") for n in names) or any(n.endswith(".class") for n in names)
    except Exception:
        return False


def script_ext_of(url):
    """返回脚本 URL 的真实后缀 .js/.py/.json，非脚本返回空串。"""
    head = url.split("$$$")[0].split("?")[0].lower()
    for e in (".json", ".js", ".py"):
        if head.endswith(e):
            return e
    return ""


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


def fetch(url, timeout=25):
    """境外域名优先代理；返回 {code, body}。图片隐写 jar 也按原始字节取。"""
    real = normalize(url)
    order = [True, False] if any(h in url for h in PROXY_FIRST) else [False, True]
    best = None
    for use_proxy in order:
        cmd = ["curl", "-sS", "-L", "--compressed", "--max-time", str(timeout), "-A", UA]
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


def parse_args():
    ap = argparse.ArgumentParser(description="把 good.json 本地化成 APK 内置源")
    ap.add_argument("--src", default=str(SRC), help="输入 good.json")
    ap.add_argument("--assets", default=str(ASSETS), help="assets/builtin 目录")
    ap.add_argument("--copy", default=str(COPY), help="adb 覆盖副本目录")
    ap.add_argument("--dry-run", action="store_true", help="只分析不写文件")
    ap.add_argument("--no-download", action="store_true", help="复用已存在的 jar（调试）")
    ap.add_argument("--max-jar-mb", type=float, default=20, help="单个 jar 最大 MB，超过视为异常")
    return ap.parse_args()


def main():
    args = parse_args()
    src = pathlib.Path(args.src)
    assets = pathlib.Path(args.assets)
    copy = pathlib.Path(args.copy)
    cfg = json.loads(src.read_text(encoding="utf-8"))
    sites = cfg.get("sites", [])
    print("=" * 72)
    print("本地化好用接口 ｜ 输入 %d 站点 ｜ %s" % (len(sites), src.name))
    print("=" * 72)

    # 收集所有 jar URL（站点级 + 顶层 spider），按 URL 去重，下载后按 md5 再复用
    jar_urls = set()
    top_spider = norm(cfg.get("spider"))
    if top_spider.startswith("http"):
        jar_urls.add(top_spider)
    for s in sites:
        j = norm(s.get("jar"))
        if j.startswith("img+"):
            j = j[4:]
        if j.startswith("http") and not j.lower().endswith(IMG_EXT):
            jar_urls.add(j)
    print("待下载 jar URL：%d 种（已跳过图片隐写 jar）" % len(jar_urls))

    assets_jars = assets / "jars"
    jar_cache = ROOT / "tools" / "my_api" / "jar_cache"  # 下载缓存放 tools，不污染 APK
    jar_cache.mkdir(parents=True, exist_ok=True)
    jar_local = {}       # url -> clan 地址
    jar_by_md5 = {}      # md5 -> 文件名
    failed = {}          # url -> 原因
    max_bytes = int(args.max_jar_mb * 1024 * 1024)

    for i, url in enumerate(sorted(jar_urls), 1):
        md5_name = hashlib.md5(url.encode()).hexdigest()[:8]
        cached = jar_cache / ("cached_%s.jar" % md5_name)
        # 缓存命中也要校验内容有效性；坏缓存删除后强制重下
        if cached.exists() and not valid_jar(cached.read_bytes()):
            print("  [%2d/%d] 坏缓存删除 %s" % (i, len(jar_urls), cached.name), flush=True)
            cached.unlink()
        if args.no_download and cached.exists():
            body = cached.read_bytes()
            code = 200
        else:
            r = fetch(url)
            code, body = r["code"], r["body"]
            if code == 200 and body:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(body)
        if code != 200 or not body:
            failed[url] = "HTTP %s" % code
            print("  [%2d/%d] FAIL HTTP %-4s %s" % (i, len(jar_urls), code, url[:70]), flush=True)
            continue
        if not valid_jar(body):
            failed[url] = "无效jar（%d 字节，dex不可读）" % len(body)
            print("  [%2d/%d] FAIL 坏jar %-7d %s" % (i, len(jar_urls), len(body), url[:70]), flush=True)
            continue
        if len(body) > max_bytes:
            failed[url] = "过大 %.1fMB" % (len(body) / 1048576)
            print("  [%2d/%d] FAIL 过大 %.1fMB %s" % (i, len(jar_urls), len(body) / 1048576, url[:70]), flush=True)
            continue
        md5 = hashlib.md5(body).hexdigest()
        if md5 in jar_by_md5:
            name = jar_by_md5[md5]
        else:
            name = "j%d.jar" % (len(jar_by_md5) + 1)
            jar_by_md5[md5] = name
        jar_local[url] = LOCAL_PREFIX + "jars/" + name + ";md5;" + md5
        print("  [%2d/%d] OK   %5d KB  %-9s %s" % (
            i, len(jar_urls), len(body) // 1024, name, url[:60]), flush=True)

    # ---------- 在线 JS/py 脚本本地化 ----------
    # 客户端 FileUtils.loadModule 支持 clan://localhost/，脚本可落地到本地文件服务
    script_urls = set()
    for s in sites:
        a = norm(s.get("api"))
        if s.get("type") == 3 and a.startswith("http") and a.lower().split("?")[0].endswith(SCRIPT_EXT):
            script_urls.add(a)
        e = s.get("ext")
        vals = []
        if isinstance(e, str):
            vals = [e]
        elif isinstance(e, dict):
            vals = [str(v) for v in e.values()]
        for v in vals:
            v = norm(v)
            # ext 里带 $$$ 的是「地址$$$参数$$$开关」复合格式，只处理首个 URL 部分
            head = v.split("$$$")[0]
            if head.startswith("http") and head.lower().split("?")[0].endswith(SCRIPT_EXT):
                script_urls.add(head)
    print("\n待下载在线脚本：%d 种" % len(script_urls))
    scripts_dir = assets / "scripts"
    script_local = {}   # 原 URL -> 本地文件名（含扩展名）
    script_failed = set()
    for i, url in enumerate(sorted(script_urls), 1):
        # 后缀必须与源 URL 一致，否则 getCSP 按后缀分流会选错 loader
        # （.js→jsLoader / .py→pyLoader / .json→其它）；绝不改成 .txt
        ext = script_ext_of(url) or ".txt"
        name = hashlib.md5(url.encode()).hexdigest()[:10] + ext
        cached = scripts_dir / name
        if args.no_download and cached.exists():
            body = cached.read_bytes()
            code = 200
        else:
            r = fetch(url)
            code, body = r["code"], r["body"]
            if code == 200 and body:
                scripts_dir.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(body)
        if code == 200 and body and len(body) < 5 * 1024 * 1024:
            script_local[url] = name
            print("  [%2d/%d] OK   %6d B  %s" % (i, len(script_urls), len(body), url[:65]), flush=True)
        else:
            script_failed.add(url)
            print("  [%2d/%d] FAIL HTTP %s %s" % (i, len(script_urls), code, url[:65]), flush=True)

    def local_script(v):
        """把一个字符串里的脚本 URL 替换为本地 clan 地址，复合 $$$ 格式只换首段。"""
        if not isinstance(v, str):
            return v
        parts = v.split("$$$")
        head = norm(parts[0])
        if head in script_local:
            parts[0] = LOCAL_PREFIX + "scripts/" + script_local[head]
            return "$$$".join(parts)
        return v

    # 选顶层 spider：覆盖站点最多的本地 jar
    jar_users = Counter()
    for s in sites:
        j = norm(s.get("jar")).replace("img+", "")
        if j in jar_local:
            jar_users[jar_local[j]] += 1
    top_jar = jar_users.most_common(1)[0][0] if jar_users else ""

    # 逐站点处理：能本地化 jar 的保留，图片隐写/失败的剔除
    local_sites, drop = [], Counter()
    for s in sites:
        site = dict(s)
        # api：在线脚本 URL → 本地 clan 地址
        api_val = norm(site.get("api"))
        api_head = api_val.split("?")[0]
        # java flavor APK 不含 Python 运行环境：api 是 .py 的爬虫站点必然加载失败，直接剔除
        # （ext 里的 .py 由 drpy.js 引擎解释，不经过 pyLoader，不受此限制）
        if api_val.startswith("http") and script_ext_of(api_val) == ".py":
            drop["py脚本站点剔除(java版不支持Python)"] += 1
            continue
        if api_head in script_local:
            site["api"] = LOCAL_PREFIX + "scripts/" + script_local[api_head] + (
                "?" + api_val.split("?", 1)[1] if "?" in api_val else "")
        # ext：字符串或字典里的脚本 URL → 本地 clan 地址
        if isinstance(site.get("ext"), str):
            site["ext"] = local_script(site["ext"])
        elif isinstance(site.get("ext"), dict):
            site["ext"] = {k: local_script(v) for k, v in site["ext"].items()}
        raw = norm(site.get("jar"))
        is_img = raw.lower().endswith(IMG_EXT)
        img_prefix = raw.startswith("img+")
        j = raw[4:] if img_prefix else raw
        if j in jar_local:
            site["jar"] = jar_local[j]
            if img_prefix:
                site["jar"] = "img+" + site["jar"]
        elif is_img and site.get("type") == 1:
            # CMS 站点不需要爬虫 jar（api 直接返回 JSON），去掉失效的图片 jar 即可保留
            site.pop("jar", None)
            drop["CMS去图片jar保留"] += 1
        elif is_img:
            # type=3 爬虫依赖图片隐写 jar，本地化会失败 → 剔除
            drop["图片jar剔除"] += 1
            continue
        elif j in failed:
            drop["jar下载失败剔除"] += 1
            continue
        else:
            drop["无jar剔除"] += 1
            continue
        local_sites.append(site)
    print("\n站点处理：保留 %d / 剔除 %d（%s）" % (len(local_sites), sum(drop.values()), dict(drop)))

    # 顶层配置：保留原元数据，spider 取本地 jar
    out = {k: cfg[k] for k in ("wallpaper", "logo", "doh", "ads", "ijk", "proxy", "rules", "hosts")
           if k in cfg}
    out["spider"] = top_jar
    out["sites"] = local_sites
    for k in ("parses", "flags"):
        if k in cfg:
            out[k] = cfg[k]

    # 直播：下载到本地
    lives_meta = []
    for i, lv in enumerate(cfg.get("lives", []), 1):
        url = norm(lv.get("url"))
        if not url.startswith("http"):
            continue
        r = fetch(url, timeout=30)
        if r["code"] != 200 or not r["body"]:
            print("  直播 FAIL HTTP %s %s" % (r["code"], url[:60]), flush=True)
            continue
        name = "l%d.txt" % i
        lives_meta.append({"name": norm(lv.get("name")) or name,
                           "origin": url, "local": LOCAL_PREFIX + "live/" + name,
                           "file": "live/" + name, "size": len(r["body"]),
                           "md5": hashlib.md5(r["body"]).hexdigest()})
        print("  直播 OK   %6d KB  %s  %s" % (len(r["body"]) // 1024, name, url[:60]), flush=True)
    out["lives"] = lives_meta

    if args.dry_run:
        print("\n[dry-run] 未写文件。将生成 %d 站点 / %d jar / %d 脚本 / %d 直播" % (
            len(local_sites), len(jar_by_md5), len(script_local), len(lives_meta)))
        return 0

    # 写文件：assets + adb 覆盖副本（内容一致）
    all_used_scripts = set()
    for base in (assets, copy):
        (base / "configs").mkdir(parents=True, exist_ok=True)
        (base / "jars").mkdir(parents=True, exist_ok=True)
        (base / "live").mkdir(parents=True, exist_ok=True)
        sd = base / "scripts"
        sd.mkdir(parents=True, exist_ok=True)
        # 复制本轮实际被配置引用的脚本（只写 local_sites 用到的，避免下载了但站点被剔除的脚本占空间）
        used = set()
        for site in local_sites:
            def collect(v):
                if isinstance(v, str):
                    for m in re.finditer(r"clan://localhost/builtin/scripts/([A-Za-z0-9_.\-]+)", v):
                        used.add(m.group(1))
                elif isinstance(v, dict):
                    for vv in v.values():
                        collect(vv)
            collect(site.get("api"))
            collect(site.get("ext"))
        if base == assets:
            all_used_scripts = set(used)
        for name in used:
            src_f = scripts_dir / name
            if src_f.exists():
                (base / "scripts" / name).write_bytes(src_f.read_bytes())
        # 清理本目录里本轮未引用的残留（旧后缀 .txt / 站点被剔的脚本）；逐个删，不整目录清空
        for old in list(sd.iterdir()):
            if old.is_file() and old.name not in used:
                old.unlink()
        (base / "configs" / "fused.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        (base / "collection.json").write_text(json.dumps({
            "urls": [{"name": "本地·好用精简源", "url": "clan://localhost/builtin/configs/fused.json"}]
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        # jar：从 cached 复制成 jN.jar
        cache_by_md5 = {}
        for url, clan in jar_local.items():
            md5 = clan.rsplit(";md5;", 1)[-1]
            cache_by_md5[md5] = jar_cache / ("cached_%s.jar" % hashlib.md5(url.encode()).hexdigest()[:8])
        for md5, name in jar_by_md5.items():
            c = cache_by_md5.get(md5)
            if c and c.exists():
                (base / "jars" / name).write_bytes(c.read_bytes())
        for lm in lives_meta:
            url = lm["origin"]
            r = fetch(url, timeout=30)  # 直播文本较小，重取一次写文件
            if r["code"] == 200 and r["body"]:
                (base / "live" / lm["file"].split("/")[-1]).write_bytes(r["body"])

    manifest = {
        "version": 4,
        "generated": datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S"),
        "default": "clan://localhost/builtin.json",
        "default_live": lives_meta[0]["local"] if lives_meta else "",
        "name": "本地·好用精简源",
        "source": str(src.relative_to(ROOT)),
        "sites": len(local_sites),
        "jars": len(jar_by_md5),
        "scripts": len(all_used_scripts),
        "live": lives_meta,
        "online": [],
        "dropped": dict(drop),
        "failed_jars": {u: v for u, v in failed.items()},
    }
    for base in (assets, copy):
        (base / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # 清理旧的 jN.jar（本轮不引用的）
    keep = set(jar_by_md5.values())
    for base in (assets, copy):
        for p in (base / "jars").glob("*.jar"):
            if p.name not in keep and not p.name.startswith("cached_"):
                p.unlink()

    report = {
        "generated": manifest["generated"], "input_sites": len(sites),
        "output_sites": len(local_sites), "jars": len(jar_by_md5),
        "scripts": len(all_used_scripts), "scripts_failed": sorted(script_failed),
        "live": len(lives_meta), "dropped": dict(drop), "failed_jars": failed,
        "top_spider": top_jar,
    }
    (ROOT / "tools" / "my_api" / "local_good_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n完成：%d 站点 / %d jar / %d 直播 → %s" % (
        len(local_sites), len(jar_by_md5), len(lives_meta), assets))
    print("adb 覆盖副本：%s" % copy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
