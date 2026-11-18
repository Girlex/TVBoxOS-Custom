#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""个人专属 TVBox 接口生成器：筛选 → 去重 → 留优。

输入：一堆接口地址（配置文件 / 多仓 / 直播 M3U 都可以，混着丢进来）
输出：一份「只留能用的、去重过的、按快慢排好序」的个人单仓配置。

做的事：
  1. 多轮测速（默认 2 轮，取中位 TTFB），直连/代理自动切换，慢于阈值直接判卡
  2. 识别类型：单仓 JSON / 多仓 JSON / 直播 M3U·TXT / 无效
  3. 多仓递归展开到全部子线路（不是只取第一条），子线路单独测速计分
  4. 校验 spider（爬虫 jar）能否下载、是否为有效 ZIP/JAR，记录下载耗时
  5. 接口级去重：解析后真实地址 + 配置内容 md5，重复保留更快的那份
  6. 站点级去重：key 与 (name, api) 双重判重，同站点保留来自更快接口的那份
  7. 站点存活检查：按域名聚合探测，域名不可达的站点直接剔除（不误杀 403/404）
  8. 生成 personal.json（每个站点显式绑定自己来源的 jar，融合后不会用错爬虫）

用法：
    python3 tools/make_my_api.py                 # 读 tools/my_api/urls.txt
    python3 tools/make_my_api.py -i 地址.txt
    python3 tools/make_my_api.py https://a.json https://b.txt
    python3 tools/make_my_api.py --rounds 3 --max-ms 4000 --top-sources 8
    python3 tools/make_my_api.py --selftest      # 不联网，只验证去重/融合逻辑

地址文件每行一条，支持三种写法（名称可省略）：
    https://example.com/config.json
    老刘备|https://raw.liucn.cc/box/m.json
    小马,https://szyyds.cn/tv/x.json
"""
import argparse
import hashlib
import html
import json
import pathlib
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from encodings import idna
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from check_tvbox_batch import tvbox_parse  # noqa: E402

OUT = ROOT / "tools" / "my_api"
PROXY = "socks5h://127.0.0.1:10808"
UA = "okhttp/3.15"
PROXY_FIRST = ("raw.githubusercontent.com", "github.com", "gh-proxy.com",
               "cdn.jsdelivr.net", "jihulab.com", "jsdelivr", "gitlab.com")

# 等级阈值（秒）：接口端到端 + jar 下载
LEVEL_FAST, LEVEL_OK = 1.0, 2.5


# ============================ 基础工具 ============================
def esc(s):
    return html.escape(str(s), quote=True)


def norm(s):
    return str(s or "").strip()


def normalize(url):
    """统一大小写域名 / 中文域名转 punycode / 路径转义，便于去重比对。"""
    p = urlsplit(url)
    labels = []
    for part in (p.hostname or "").split("."):
        if any(ord(c) > 127 for c in part):
            part = idna.ToASCII(part).decode("ascii")
        labels.append(part)
    host = ".".join(labels) + (":%d" % p.port if p.port else "")
    path = quote(p.path, safe="/%:=&?+,;@!$'()*-._~")
    return urlunsplit((p.scheme.lower() or "http", host, path, p.query, ""))


def url_key(url):
    """去重用的地址指纹：去掉 query 与尾部斜杠。"""
    p = urlsplit(normalize(url))
    return (p.netloc + p.path).rstrip("/").lower()


def median(values):
    values = sorted(v for v in values if v is not None and v > 0)
    if not values:
        return 0.0
    n = len(values)
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2.0


def md5_bytes(b):
    return hashlib.md5(b).hexdigest()


def rewrite_relative(node, base_url, skip_keys=("jar",)):
    """把以 ./ 或 ../ 开头的资源路径改写成基于原站的绝对地址。"""
    if isinstance(node, dict):
        return {k: (v if k in skip_keys else rewrite_relative(v, base_url, skip_keys))
                for k, v in node.items()}
    if isinstance(node, list):
        return [rewrite_relative(v, base_url, skip_keys) for v in node]
    if isinstance(node, str):
        s = node.strip()
        if s.startswith("./") or s.startswith("../"):
            return urljoin(base_url, s)
    return node


def abs_url(base, raw):
    raw = norm(raw)
    if not raw:
        return ""
    low = raw.lower()
    if low.startswith(("http://", "https://", "clan://", "proxy://")):
        return raw
    try:
        return urljoin(base, raw)
    except Exception:
        return raw


# ============================ 输入解析 ============================
NAME_SEP = re.compile(r"\s*(?:\||,|，|\t|\s{2,}|\s-\s|：|:)\s*")


def parse_input_text(text):
    """每行一条：URL / 名称|URL / 名称,URL / 名称 URL。"""
    items, seen = [], set()
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        m = re.search(r"https?://[^\s'\"<>]+", line)
        if not m:
            continue
        url = m.group(0).rstrip('.,;)"\'')
        name = line[:m.start()].strip(" \t-—|:：,，[]()（）\"'")
        if not name:
            host = urlsplit(url).hostname or ""
            name = host.replace("www.", "") or url
        k = url_key(url)
        if k in seen:
            continue
        seen.add(k)
        items.append({"name": name, "url": url, "raw": raw.strip()})
    return items


def load_inputs(args):
    items = []
    if args.input:
        p = pathlib.Path(args.input)
        if not p.is_file():
            raise SystemExit("找不到地址文件：%s" % p)
        items += parse_input_text(p.read_text(encoding="utf-8", errors="replace"))
    default_file = OUT / "urls.txt"
    if not args.input and default_file.is_file():
        items += parse_input_text(default_file.read_text(encoding="utf-8", errors="replace"))
    if args.urls:
        items += parse_input_text("\n".join(args.urls))
    if not sys.stdin.isatty() and not args.selftest:
        piped = sys.stdin.read()
        if piped.strip():
            items += parse_input_text(piped)
    # 二次去重（多来源合并后）
    out, seen = [], set()
    for it in items:
        k = url_key(it["url"])
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


# ============================ 抓取 / 测速 ============================
def fetch_once(url, timeout=15, proxy=None, head=None):
    cmd = ["curl", "-sS", "-L", "--compressed", "--max-time", str(timeout),
           "-A", UA, "-H", "Accept: */*"]
    if head:
        cmd += ["-r", "0-%d" % (head - 1)]
    cmd += (["--proxy", proxy] if proxy else ["--noproxy", "*"])
    cmd += ["-w", "\n__META__%{http_code}|%{time_starttransfer}|%{time_total}|%{size_download}", url]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return {"code": 0, "body": b"", "ttfb": float(timeout), "total": float(timeout),
                "size": 0, "proxy": bool(proxy)}
    out = r.stdout or b""
    if b"__META__" not in out:
        return {"code": 0, "body": b"", "ttfb": 0.0, "total": 0.0, "size": 0,
                "proxy": bool(proxy)}
    body, meta = out.rsplit(b"__META__", 1)
    body = body[:-1] if body.endswith(b"\n") else body
    p = meta.decode("utf-8", "replace").strip().split("|")
    try:
        code = int(p[0] or 0)
    except Exception:
        code = 0
    ttfb = float(p[1]) if len(p) > 1 and p[1] else 0.0
    total = float(p[2]) if len(p) > 2 and p[2] else 0.0
    size = int(float(p[3])) if len(p) > 3 and p[3] else 0
    return {"code": code, "body": body, "ttfb": ttfb, "total": total,
            "size": size, "proxy": bool(proxy)}


def fetch_rank(url, rounds=2, timeout=12, head=None):
    """多轮测速。第 1 轮取完整内容，后续轮只取前 64KB 测速，省流量。"""
    real = normalize(url)
    order = [PROXY, None] if any(h in real for h in PROXY_FIRST) else [None, PROXY]
    tries, full = [], None
    for i in range(max(1, rounds)):
        got = None
        for px in order:
            r = fetch_once(real, timeout, px, head=None if (i == 0 or full is None) else head)
            if r["code"] in (200, 206) and r["body"]:
                got = r
                break
            if got is None:
                got = r
        if got is None:
            got = {"code": 0, "body": b"", "ttfb": 0.0, "total": 0.0,
                   "size": 0, "proxy": False}
        tries.append(got)
        if got["code"] in (200, 206) and got["body"] and full is None:
            full = got

    oks = [t for t in tries if t["code"] in (200, 206) and t["body"]]
    if not oks or full is None:
        bad = max(tries, key=lambda t: t["code"]) if tries else {"code": 0}
        return {"ok": False, "code": bad.get("code", 0), "body": b"", "ttfb": 0.0,
                "total": 0.0, "proxy": bool(bad.get("proxy")), "tries": len(tries)}
    return {"ok": True, "code": full["code"], "body": full["body"],
            "ttfb": median([t["ttfb"] for t in oks]),
            "total": median([t["total"] for t in oks]),
            "proxy": full["proxy"], "tries": len(oks)}


# ============================ 内容识别 ============================
LIVE_HINT = re.compile(r"(#EXTM3U|,#genre#|#EXTINF)", re.I)


def parse_kind(body_bytes):
    """返回 (kind, payload)。kind ∈ 单仓 / 多仓 / 直播 / 无效。"""
    txt = body_bytes.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
    if not txt:
        return "无效", None
    # 这些配置常见「开头一大段 // 注释」「尾部拼 HTML」，只要前面不是 HTML 文档，
    # 就交给 tvbox_parse 按客户端同样的规则截取 JSON 片段。
    probe_at = 0 if txt[0] in "{[" else txt.find("{")
    json_hint = probe_at == 0
    if not json_hint and 0 <= probe_at < 8192:
        head = txt[:probe_at].lower()
        json_hint = not any(t in head for t in ("<html", "<!doctype", "<?xml", "<head", "<body"))
    if json_hint:
        cfg, note = tvbox_parse(txt)
        if isinstance(cfg, list):
            return "无效", "顶层是数组（%s）" % note
        if isinstance(cfg, dict):
            if "sites" in cfg or "spider" in cfg:
                if isinstance(cfg.get("sites"), list) or isinstance(cfg.get("urls"), list):
                    return ("多仓" if "sites" not in cfg else "单仓"), cfg
                return "单仓", cfg
            if isinstance(cfg.get("urls"), list) and cfg["urls"]:
                return "多仓", cfg
            return "无效", "JSON 里没有 sites / urls"
        return "无效", note
    if LIVE_HINT.search(txt[:2000]):
        return "直播", txt
    return "无效", "内容不是 TVBox 配置，也不是直播列表"


def count_live_channels(txt):
    """数频道条数：兼容 M3U(#EXTINF) 与 TVBox 常用的「频道名,地址」txt 格式。"""
    n = len(re.findall(r"#EXTINF", txt, re.I))
    if n:
        return n
    n = 0
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line and re.search(r"(https?://|rtmp|rtsp|m3u8)", line, re.I):
            n += 1
    return n


def collection_entries(cfg):
    """多仓 urls 数组 → [(name, url)]，兼容 dict / 字符串两种写法。"""
    out = []
    for e in cfg.get("urls") or []:
        if isinstance(e, dict):
            u = norm(e.get("url") or e.get("api") or e.get("api_url"))
            name = norm(e.get("name") or e.get("title") or "")
        else:
            u, name = norm(e), ""
        if u:
            out.append((name, u))
    return out


# ============================ 来源评估 ============================
def base_record(name, url, seed, chain=None):
    return {
        "seed": seed, "name": name or (urlsplit(url).hostname or url), "url": url,
        "resolved_url": url, "chain": chain or [],
        "kind": "无效", "cfg": None, "sites": 0, "parses": 0, "lives": 0,
        "ms_entry": 0.0, "ms_chain": 0.0, "ms_jar": 0.0, "ms": 0.0,
        "ttfb": 0.0, "via": "直连", "byte": 0,
        "jar_url": "", "jar_md5": "", "jar_ok": None, "jar_size": 0,
        "live_channels": 0, "live_ok": False,
        "level": "dead", "keep": False, "reasons": [],
    }


def evaluate_source(name, url, seed, rounds, max_ms, timeout, chain=None, depth=0):
    """评估一个单仓配置地址（含 spider 校验），返回 record。多仓由 expand_seed 展开。"""
    rec = base_record(name, url, seed, chain)
    r = fetch_rank(url, rounds=rounds, timeout=timeout, head=65536)
    rec["ttfb"], rec["via"], rec["byte"] = round(r["ttfb"], 3), ("代理" if r["proxy"] else "直连"), len(r["body"])
    if not r["ok"]:
        rec["reasons"].append("无法获取（HTTP %s）" % (r["code"] or "连接失败"))
        rec["ms"] = 99.0
        return rec

    rec["ms_entry"] = round(r["ttfb"], 3)
    kind, payload = parse_kind(r["body"])

    if kind == "直播":
        rec["kind"] = "直播"
        rec["resolved_url"] = url
        rec["live_channels"] = count_live_channels(payload)
        rec["live_ok"] = rec["live_channels"] >= 5
        rec["ms"] = rec["ms_entry"]
        if not rec["live_ok"]:
            rec["reasons"].append("直播列表频道过少（%d）" % rec["live_channels"])
        else:
            rec["keep"] = True
            rec["level"] = "fast" if rec["ms"] <= LEVEL_FAST else "ok"
        rec["live_text"] = payload
        return rec

    if kind == "多仓":
        # 多仓本身不直接融合，交给 expand_seed 展开成子线路逐个评估
        rec["kind"] = "多仓"
        rec["cfg"] = rewrite_relative(payload, url)
        rec["resolved_url"] = url
        rec["ms"] = rec["ms_entry"]
        rec["sub_count"] = len(collection_entries(payload))
        if not rec["sub_count"]:
            rec["reasons"].append("多仓配置的 urls 列表为空")
            rec["ms"] = 99.0
        else:
            rec["keep"] = True
            rec["level"] = "ok"
        return rec

    if kind != "单仓":
        rec["kind"] = kind
        rec["reasons"].append("不是单仓配置：%s" % (payload if isinstance(payload, str) else kind))
        rec["ms"] = 99.0
        return rec

    cfg = payload
    rec["kind"] = "单仓"
    rec["cfg"] = rewrite_relative(cfg, url)
    rec["sites"] = len([s for s in (cfg.get("sites") or []) if isinstance(s, dict)])
    rec["parses"] = len(cfg.get("parses") or [])
    rec["lives"] = len(cfg.get("lives") or [])

    # spider：相对路径按配置地址补全后真实下载校验
    jar_raw = norm(cfg.get("spider"))
    jar_url, jar_md5 = "", ""
    if jar_raw:
        parts = jar_raw.split(";md5;")
        jar_url = abs_url(url, parts[0].strip())
        jar_md5 = parts[1].strip() if len(parts) > 1 else ""
    rec["jar_url"], rec["jar_md5"] = jar_url, jar_md5
    if jar_url:
        jr = fetch_once(jar_url, timeout=min(30, timeout * 2), proxy=None)
        if jr["code"] not in (200, 206) or not jr["body"]:
            jr2 = fetch_once(jar_url, timeout=min(30, timeout * 2), proxy=PROXY)
            if jr2["code"] in (200, 206) and jr2["body"]:
                jr = jr2
        rec["ms_jar"] = round(jr["ttfb"], 3)
        rec["jar_size"] = len(jr["body"])
        rec["jar_ok"] = jr["code"] in (200, 206) and jr["body"][:2] == b"PK"
        if not rec["jar_ok"]:
            rec["reasons"].append("spider 不可用（HTTP %s，%d 字节，非 jar）" % (
                jr["code"] or "连接失败", rec["jar_size"]))
    elif rec["sites"]:
        rec["reasons"].append("单仓配置缺少 spider，type=3 站点无法加载")

    if not rec["sites"]:
        rec["reasons"].append("配置里没有有效站点")

    total = rec["ms_entry"] + rec["ms_chain"] + rec["ms_jar"]
    rec["ms"] = round(total, 3)
    if rec["reasons"]:
        return rec
    if total > max_ms:
        rec["reasons"].append("接口响应过慢（%.2fs > 阈值 %.2fs）" % (total, max_ms))
        return rec
    rec["keep"] = True
    rec["level"] = "fast" if total <= LEVEL_FAST else ("ok" if total <= LEVEL_OK else "slow")
    return rec


def expand_seed(item, args, pool):
    """一个输入地址 → 若干来源（多仓会展开全部子线路）。"""
    rec = evaluate_source(item["name"], item["url"], item["url"], args.rounds,
                          args.max_ms, args.timeout)
    rec["seed"] = item["url"]
    if rec["kind"] != "多仓" or rec["cfg"] is None or not rec.get("sub_count"):
        return [rec]

    subs = collection_entries(rec["cfg"])
    results = [rec]
    jobs = {}
    for name, u in subs[: args.max_sub]:
        u_abs = abs_url(item["url"], u)
        jobs[u_abs] = pool.submit(evaluate_source, name, u_abs, item["url"], args.rounds,
                                  args.max_ms, args.timeout, [u_abs], 1)
    for u_abs, job in jobs.items():
        sub = job.result()
        sub["chain"] = [item["url"], u_abs]
        sub["is_sub"] = True
        # 多仓场景客户端要先拿多仓入口再拿子线路，端到端要算上入口耗时
        if sub["ms_chain"] == 0.0:
            sub["ms_chain"] = round(rec["ms_entry"], 3)
            sub["ms"] = round(sub["ms_entry"] + sub["ms_chain"] + sub["ms_jar"], 3)
            # 补上入口耗时后要重新判定，否则慢链路会被误判为通过
            if sub["keep"] and sub["ms"] > args.max_ms:
                sub["keep"] = False
                sub["level"] = "dead"
                sub["reasons"].append("多仓链路整体过慢（%.2fs > 阈值 %.2fs）" % (
                    sub["ms"], args.max_ms))
            elif sub["keep"]:
                sub["level"] = ("fast" if sub["ms"] <= LEVEL_FAST
                                else ("ok" if sub["ms"] <= LEVEL_OK else "slow"))
        results.append(sub)
    return results


# ============================ 站点存活探测 ============================
def host_of(url):
    try:
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.netloc:
            return ""
        return "%s://%s/" % (p.scheme, p.netloc)
    except Exception:
        return ""


def probe_hosts(hosts, timeout=8, jobs=16, proxy_jobs=4):
    """域名级存活探测，三阶段避免误杀：
       ① 直连并发探（快）→ ② 失败的走本地代理低并发补测（并发高了会压垮代理，把活域名测成死的）
       → ③ 仍失败的单点再复查一次。
    只要有一轮拿到 HTTP 响应（哪怕 403/404/500）就算活——那只是站点不欢迎裸请求。"""
    hosts = list(hosts)
    out, pending = {}, hosts

    def direct(h):
        return h, fetch_once(h, timeout=timeout, proxy=None)

    def via_proxy(h):
        return h, fetch_once(h, timeout=timeout + 4, proxy=PROXY)

    with ThreadPoolExecutor(max_workers=jobs) as ex:
        for h, r in ex.map(direct, pending):
            if r["code"] != 0:
                out[h] = {"code": r["code"], "ms": round(r["ttfb"], 3),
                          "proxy": False, "phase": "直连"}
    pending = [h for h in hosts if h not in out]

    if pending:
        with ThreadPoolExecutor(max_workers=proxy_jobs) as ex:
            for h, r in ex.map(via_proxy, pending):
                if r["code"] != 0:
                    out[h] = {"code": r["code"], "ms": round(r["ttfb"], 3),
                              "proxy": True, "phase": "代理"}
    pending = [h for h in pending if h not in out]

    for h in pending:
        r = fetch_once(h, timeout=timeout, proxy=None)
        out[h] = {"code": r["code"], "ms": round(r["ttfb"], 3),
                  "proxy": False, "phase": "复查"}
    return out


# ============================ 融合去重 ============================
def merge_sites(entries):
    """按来源速度升序遍历，key / (name, api) 双重去重，保留更快来源的站点。"""
    kept, stats = [], {"raw": 0, "skipped": 0, "dup_key": 0, "dup_pair": 0,
                       "dead_host": 0, "kept": 0}
    seen_key, seen_pair = set(), set()
    dropped = []
    for ent in entries:
        cfg = ent.get("cfg") or {}
        for s in cfg.get("sites") or []:
            if not isinstance(s, dict):
                stats["skipped"] += 1
                continue
            if not all(k in s for k in ("key", "type", "api")):
                stats["skipped"] += 1
                continue
            stats["raw"] += 1
            key, name, api = norm(s.get("key")), norm(s.get("name")), norm(s.get("api"))
            pair = (name, api.rstrip("/"))
            if key in seen_key:
                stats["dup_key"] += 1
                dropped.append((key, name, "key 重复"))
                continue
            if pair in seen_pair:
                stats["dup_pair"] += 1
                dropped.append((key, name, "名称+地址重复"))
                continue
            site = dict(s)
            # 站点资源绝对化；jar 单独处理，稍后绑定（img+ 前缀必须保留，
            # 客户端 JarLoader 靠它区分「图片隐写 jar」和普通 jar）
            site = rewrite_relative(site, ent["resolved_url"], skip_keys=("jar",))
            raw_jar = norm(s.get("jar"))
            if raw_jar.startswith("img+"):
                site["jar"] = "img+" + abs_url(ent["resolved_url"], raw_jar[4:])
            elif raw_jar:
                site["jar"] = abs_url(ent["resolved_url"], raw_jar)
            else:
                site["jar"] = ent["jar_url"]
            site["_src"] = ent["name"]
            seen_key.add(key)
            seen_pair.add(pair)
            kept.append(site)
    stats["kept"] = len(kept)
    return kept, stats, dropped


def apply_host_filter(sites, host_state):
    """域名不可达 → 剔除；返回 (保留的站点, 统计)。"""
    alive, dead, dead_hosts = [], 0, set()
    for s in sites:
        h = host_of(norm(s.get("api")))
        st = host_state.get(h) if h else None
        if st is not None and st["code"] == 0:
            dead += 1
            dead_hosts.add(h)
            continue
        alive.append(s)
    return alive, {"dead_sites": dead, "dead_hosts": sorted(dead_hosts)}


# ============================ 输出 ============================
META_KEYS = ("wallpaper", "logo", "doh", "ads", "ijk", "proxy", "rules", "hosts")


def build_personal(entries, sites, lives, name, keep_hosts=False):
    base = entries[0]
    cfg = base.get("cfg") or {}
    out = {"spider": base.get("jar_url") or ""}
    # hosts 会改写域名解析，多来源融合时只可能服务其中一家、污染其余，默认不带
    meta_keys = META_KEYS if (len(entries) == 1 or keep_hosts) else \
        tuple(k for k in META_KEYS if k != "hosts")
    for k in meta_keys:
        for e in entries:
            v = (e.get("cfg") or {}).get(k)
            if v not in (None, "", [], {}):
                out[k] = v
                break
    clean_sites = []
    for s in sites:
        d = {k: v for k, v in s.items() if not k.startswith("_")}
        clean_sites.append(d)
    out["sites"] = clean_sites

    parses, seen = [], set()
    for e in entries:
        for p in (e.get("cfg") or {}).get("parses") or []:
            if not isinstance(p, dict):
                continue
            k = (norm(p.get("name")), norm(p.get("url")))
            if k in seen:
                continue
            seen.add(k)
            parses.append(p)
    if parses:
        out["parses"] = parses

    flags = []
    for e in entries:
        for f in (e.get("cfg") or {}).get("flags") or []:
            if f not in flags:
                flags.append(f)
    if flags:
        out["flags"] = flags
    if lives:
        out["lives"] = lives
    if not out["spider"] and clean_sites:
        # 兜底：站点自带 jar 取第一个可用的当顶层爬虫
        for s in clean_sites:
            j = norm(s.get("jar")).replace("img+", "")
            if j.startswith("http"):
                out["spider"] = j
                break
    return out


def write_outputs(args, inputs, records, entries, personal, lives, stats, host_state):
    OUT.mkdir(parents=True, exist_ok=True)
    for fn in ("personal.json", "collection.json", "sources.json", "report.html", "kept_urls.txt"):
        p = OUT / fn
        if p.exists():
            p.unlink()

    personal_text = json.dumps(personal, ensure_ascii=False, indent=1)
    (OUT / "personal.json").write_text(personal_text, encoding="utf-8")

    kept_urls = [r["resolved_url"] for r in records
                 if r["keep"] and r["kind"] == "单仓"]
    (OUT / "kept_urls.txt").write_text(
        "# 本次通过筛选的接口地址（按快慢排序），下次可直接丢回 make_my_api.py\n"
        + "\n".join(kept_urls) + "\n", encoding="utf-8")

    if args.public_base:
        base = args.public_base.rstrip("/")
        collection = {"urls": [{"name": "%s（融合去重）" % args.name,
                                "url": base + "/personal.json"}]}
        (OUT / "collection.json").write_text(
            json.dumps(collection, ensure_ascii=False, indent=2), encoding="utf-8")

    (OUT / "sources.json").write_text(json.dumps({
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": {"rounds": args.rounds, "max_ms": args.max_ms,
                   "top_sources": args.top_sources, "probe": args.probe},
        "inputs": [{"name": i["name"], "url": i["url"]} for i in inputs],
        "records": [{k: v for k, v in r.items() if k not in ("cfg", "live_text")} for r in records],
        "stats": stats,
        "hosts": host_state,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    (OUT / "report.html").write_text(
        render_report(args, inputs, records, personal, lives, stats, host_state), encoding="utf-8")


def level_tag(rec):
    if rec["kind"] == "直播":
        return ("fast", "直播可用") if rec.get("live_ok") else ("dead", "直播不可用")
    if rec["kind"] == "多仓":
        return (("ok", "已展开 %d 条" % rec.get("sub_count", 0)) if rec["keep"]
                else ("dead", "剔除"))
    if not rec["keep"]:
        return "dead", "剔除"
    return rec["level"], {"fast": "优", "ok": "良", "slow": "可用·偏慢"}[rec["level"]]


def render_report(args, inputs, records, personal, lives, stats, host_state):
    css = """
    :root{--bg:#f6f7f9;--card:#fff;--line:#e6e8ec;--text:#1f2329;--muted:#6b7280;
          --red:#d92d20;--red-bg:#fef3f2;--green:#067647;--green-bg:#ecfdf3;
          --amber:#b54708;--amber-bg:#fffaeb;--blue:#175cd3;--blue-bg:#eff8ff}
    *{box-sizing:border-box}
    body{margin:0;padding:32px 20px 60px;background:var(--bg);color:var(--text);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
         line-height:1.6;-webkit-font-smoothing:antialiased}
    .wrap{max-width:1120px;margin:0 auto}
    h1{font-size:24px;margin:0 0 6px;letter-spacing:-.2px}
    h2{font-size:17px;margin:30px 0 12px}
    .sub{color:var(--muted);font-size:13px;margin-bottom:20px}
    .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin-bottom:16px}
    .kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}
    .kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
    .kpi b{display:block;font-size:24px;line-height:1.2}
    .kpi span{color:var(--muted);font-size:12px}
    table{width:100%;border-collapse:collapse;font-size:13px}
    th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}
    th{color:var(--muted);font-weight:600;background:#fafbfc;font-size:12px}
    .mono,code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
    code{background:#f3f4f6;padding:1px 5px;border-radius:4px;word-break:break-all}
    .tag{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600;white-space:nowrap}
    .t-fast{background:var(--green-bg);color:var(--green)}
    .t-ok{background:var(--blue-bg);color:var(--blue)}
    .t-slow{background:var(--amber-bg);color:var(--amber)}
    .t-dead{background:var(--red-bg);color:var(--red)}
    .sum{border-left:4px solid var(--blue);background:var(--blue-bg);border-radius:10px;padding:14px 18px;
         margin-bottom:18px;font-size:14px}
    .warn{border-left:4px solid var(--amber);background:var(--amber-bg);border-radius:10px;padding:14px 18px;
          margin-bottom:18px;font-size:14px}
    ul{margin:8px 0 0;padding-left:20px;font-size:14px} li{margin:6px 0}
    .muted{color:var(--muted)} .small{color:var(--muted);font-size:12px}
    details{margin-top:10px} summary{cursor:pointer;color:var(--blue);font-size:13px}
    .foot{color:var(--muted);font-size:12px;text-align:center;margin-top:26px}
    tr.drop td{color:#98a2b3}
    """
    kept = [r for r in records if r["keep"] and r["kind"] == "单仓"]
    live_ok = [r for r in records if r["kind"] == "直播" and r.get("live_ok")]
    dropped = [r for r in records if not (r["keep"] and r["kind"] == "单仓")]

    rows = []
    for r in sorted(records, key=lambda x: (x["kind"] != "单仓", x["ms"])):
        tag, label = level_tag(r)
        scope = ("%d 站点" % r["sites"]) if r["kind"] == "单仓" else (
            ("%d 条线路" % r.get("sub_count", 0)) if r["kind"] == "多仓" else (
                ("%d 频道" % r["live_channels"]) if r["kind"] == "直播" else "—"))
        jar = "%.2fs / %dKB" % (r["ms_jar"], r["jar_size"] // 1024) if r.get("jar_ok") else (
            "失败" if r.get("jar_url") else "—")
        why = "；".join(r["reasons"]) or "通过"
        rows.append(
            "<tr class='%s'><td><b>%s</b><div class='small mono'>%s</div>%s</td>"
            "<td>%s</td><td class='mono'>%.2fs</td><td class='mono'>%s</td>"
            "<td>%s</td><td>%s</td><td><span class='tag t-%s'>%s</span><div class='small'>%s</div></td></tr>" % (
                "" if r["keep"] else "drop", esc(r["name"]), esc(r["resolved_url"]),
                ("<div class='small'>来自 %s</div>" % esc(r["seed"])) if r.get("is_sub") else "",
                esc(r["kind"]), r["ms"], esc(r["via"]), esc(jar), esc(scope),
                tag, esc(label), esc(why)))

    site_rows = []
    for i, s in enumerate(personal.get("sites") or [], 1):
        if i > 300:
            break
        site_rows.append("<tr><td>%d</td><td>%s</td><td>%s</td><td class='mono'>%s</td>"
                         "<td class='mono'>%s</td></tr>" % (
                             i, esc(s.get("name")), esc(s.get("type")),
                             esc(str(s.get("api"))[:110]), esc(str(s.get("jar"))[:90])))

    live_rows = "".join(
        "<tr><td>%s</td><td class='mono'>%s</td><td>%d</td><td class='mono'>%.2fs</td></tr>" % (
            esc(l["name"]), esc(l["url"]), l.get("_channels", 0), l.get("_ms", 0))
        for l in lives)

    keep_ratio = (len(personal.get("sites") or []) / stats["raw"] * 100) if stats["raw"] else 0
    doc = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>个人专属接口 · 筛选去重报告</title><style>%s</style></head><body><div class="wrap">
<h1>个人专属接口 · 筛选去重报告</h1>
<div class="sub">%s ｜ 测速 %d 轮取中位 TTFB ｜ 卡顿阈值 %.1fs ｜ 生成 tools/my_api/personal.json</div>

<div class="kpis">
<div class="kpi"><b>%d</b><span>输入地址</span></div>
<div class="kpi"><b>%d</b><span>通过筛选的接口</span></div>
<div class="kpi"><b>%d</b><span>融合后站点</span></div>
<div class="kpi"><b>%d%%</b><span>站点留存率</span></div>
</div>

<div class="sum"><b>一句话结论：</b>%d 个地址里留下 <b>%d 个干净接口</b>，
把它们的站点融合成 <b>%d 个不重复站点</b>（原始 %d 个，去掉重复 %d 个、缺字段 %d 个、域名挂掉 %d 个），
并已按响应速度排好序——TVBox 首页默认站点就来自最快的那个接口。
%s</div>

%s

<h2>逐个接口判定</h2>
<div class="card" style="padding:8px 10px">
<table><thead><tr><th style="width:34%%">名称 / 地址</th><th style="width:66px">类型</th>
<th style="width:66px">端到端</th><th style="width:80px">通道</th><th style="width:110px">spider</th>
<th style="width:70px">规模</th><th style="width:130px">结论</th></tr></thead>
<tbody>%s</tbody></table>
<p class="small" style="margin:10px 4px 2px">端到端 = 配置响应 TTFB + 多仓子线路 TTFB + 爬虫 jar 下载 TTFB。
任何一项超时或 jar 不是有效 ZIP/JAR，该接口直接判掉——这类接口在电视上就是「点进去转圈」。</p></div>

<h2>融合后的站点清单<span class="small">（前 300 个，按来源快慢排列）</span></h2>
<div class="card" style="padding:8px 10px">
<table><thead><tr><th style="width:36px">#</th><th style="width:150px">站点</th><th style="width:48px">type</th>
<th>api</th><th style="width:220px">绑定 jar</th></tr></thead><tbody>%s</tbody></table></div>

%s

<div class="card"><h2 style="margin-top:0">怎么用</h2>
<ul>
<li><b>同一局域网（最快）：</b>在电脑上跑 <code>python3 tools/make_my_api.py --serve 8000</code>，
窗口里会打印形如 <code>http://192.168.x.x:8000/personal.json</code> 的地址，把它填进电视的配置地址即可。
注意 TVBox 的 <code>configUrl()</code> 会给非 http 地址强行补 <code>http://</code>，
所以<strong>不能直接填 <code>/sdcard/personal.json</code> 这类本地路径</strong>。</li>
<li><b>长期使用：</b>把 <code>personal.json</code> 上传到任意能直连的静态托管（GitHub raw / Gitee / 自己的 HTTP 目录），
配置地址填该 URL；加 <code>--public-base &lt;该目录前缀&gt;</code> 重跑还会生成 <code>collection.json</code>。</li>
<li><b>内置进 APK：</b>把 <code>personal.json</code> 放到 <code>app/src/main/assets/builtin/configs/</code>，
配置地址填 <code>clan://localhost/builtin/configs/personal.json</code>，重编译后离线可用。</li>
<li><b>只想要单个接口：</b><code>kept_urls.txt</code> 第一行就是实测最快的原始接口地址。</li>
<li><b>定期复测：</b>重跑脚本会覆盖输出；把新地址追加进 <code>tools/my_api/urls.txt</code> 一起过筛，
重复的会自动合并，老地址失效的会被自动剔除。</li>
</ul></div>

<div class="card"><h2 style="margin-top:0">判定口径</h2>
<ul>
<li><b>去重</b>：接口层按「解析后真实地址 + 配置内容 md5」判重，重复保留响应更快的那份；站点层按 <code>key</code> 与
<code>(name, api)</code> 双重判重，同名同地址只留最快接口的版本。共用同一个 spider jar 不算重复。</li>
<li><b>卡不卡</b>：只看可测的网络层指标（TTFB、jar 下载），不猜站点内容质量。等级：优 ≤%.1fs、良 ≤%.1fs、
可用·偏慢 ≤%.1fs、超过阈值剔除。</li>
<li><b>站点存活</b>：按 <code>api</code> 的域名聚合探测，连不上（DNS 失败 / 拒绝 / 超时）才剔除；
返回 403/404 仍保留——很多站点接口对裸请求就是这个反应，不代表不能用。</li>
<li><b>jar 绑定</b>：融合后每个站点显式写自己的 <code>jar</code>（绝对地址），避免不同来源的站点互相串用爬虫。</li>
<li>只验证「配置能不能拿到 / 爬虫能不能下 / 域名通不通」，不验证版权与内容合规，也不替代设备端实际播放测试。</li>
</ul></div>
<div class="foot">由 tools/make_my_api.py 生成 · 可随时重跑复测</div>
</div></body></html>""" % (
        css, esc(datetime.now().strftime("%Y-%m-%d %H:%M:%S")), args.rounds, args.max_ms,
        len(inputs), len(kept), len(personal.get("sites") or []), keep_ratio,
        len(inputs), len(kept), len(personal.get("sites") or []), stats["raw"],
        stats["dup_key"] + stats["dup_pair"], stats["skipped"], stats["dead_host"],
        ("另有 %d 条直播源可用。" % len(live_ok)) if live_ok else "",
        "" if kept else "<div class='warn'><b>没有接口通过筛选。</b>"
        "要么全部失效，要么都超过了 %.1fs 的卡顿阈值——可以加 <code>--max-ms 8000</code> 放宽再跑一次。</div>" % args.max_ms,
        "".join(rows), "".join(site_rows),
        ("<h2>直播源（已写入 personal.json 的 lives）</h2><div class='card' style='padding:8px 10px'>"
         "<table><thead><tr><th>名称</th><th>地址</th><th>频道数</th><th>响应</th></tr></thead>"
         "<tbody>%s</tbody></table></div>" % live_rows) if lives else "",
        LEVEL_FAST, LEVEL_OK, args.max_ms)
    return doc


# ============================ 自检 ============================
def selftest():
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("  [OK] " if cond else "  [FAIL] ") + msg)
        ok = ok and bool(cond)

    print("自检 1：输入解析（名称/地址多种写法 + 去重）")
    items = parse_input_text("""
    # 注释行
    https://a.com/1.json
    老刘备|https://a.com/2.json
    小马,https://a.com/3.json
    4K盒子   https://a.com/1.json
    """)
    check(len(items) == 3, "4 行输入（含 1 条重复）应为 3 条，实际 %d" % len(items))
    check(items[1]["name"] == "老刘备", "竖线分隔取到名称：%s" % items[1]["name"])
    check(items[2]["name"] == "小马", "逗号分隔取到名称：%s" % items[2]["name"])

    print("自检 2：多仓展开 + 站点去重（key 与 name+api 双口径）")
    cfg_a = {"spider": "https://a.com/s.jar;md5;abc", "sites": [
        {"key": "k1", "name": "甲", "type": 3, "api": "http://x.com/api.php/provide/vod/"},
        {"key": "k2", "name": "乙", "type": 0, "api": "http://y.com/api.php/provide/vod/"},
        {"key": "k3", "name": "丙", "type": 3, "api": "./api3"},
        {"name": "缺key", "type": 0, "api": "http://z.com"},
    ], "parses": [{"name": "p1", "url": "http://p.com/1"}], "flags": ["youku", "qq"]}
    cfg_b = {"spider": "https://b.com/s.jar", "sites": [
        {"key": "k1", "name": "甲", "type": 3, "api": "http://x.com/api.php/provide/vod/"},
        {"key": "k9", "name": "乙", "type": 0, "api": "http://y.com/api.php/provide/vod/"},
        {"key": "k10", "name": "丁", "type": 3, "api": "http://w.com/api.php/provide/vod/"},
    ], "parses": [{"name": "p1", "url": "http://p.com/1"}, {"name": "p2", "url": "http://p.com/2"}]}
    entries = [
        {"name": "A", "resolved_url": "https://a.com/1.json", "jar_url": "https://a.com/s.jar", "cfg": cfg_a},
        {"name": "B", "resolved_url": "https://b.com/2.json", "jar_url": "https://b.com/s.jar", "cfg": cfg_b},
    ]
    sites, stats, dropped = merge_sites(entries)
    check(stats["raw"] == 6, "参与比对的站点 6 个（7 个里 1 个缺字段），实际 %d" % stats["raw"])
    check(stats["skipped"] == 1, "缺 key/type/api 跳过 1 个，实际 %d" % stats["skipped"])
    check(stats["dup_key"] == 1, "key 重复(B 的 k1) 剔除 1 个，实际 %d" % stats["dup_key"])
    check(stats["dup_pair"] == 1, "name+api 重复(B 的乙) 剔除 1 个，实际 %d" % stats["dup_pair"])
    check(len(sites) == 4, "去重后应 4 个站点，实际 %d" % len(sites))
    check(sites[0]["_src"] == "A", "同站点保留更快来源 A 的版本")
    check(sites[2]["api"] == "https://a.com/api3", "相对 api 已绝对化：%s" % sites[2]["api"])
    check(sites[0]["jar"] == "https://a.com/s.jar", "A 站点绑定 A 的 jar")
    check(sites[3]["jar"] == "https://b.com/s.jar", "B 站点绑定 B 的 jar，不串用")

    print("自检 3：域名不可达过滤")
    alive, st = apply_host_filter(sites, {"https://a.com/": {"code": 0}, "http://x.com/": {"code": 404}})
    check(len(alive) == 3, "a.com 域名挂掉，剔除 1 个站点后剩 3，实际 %d" % len(alive))
    check("https://a.com/" in st["dead_hosts"], "记录了挂掉的域名")

    print("自检 4：个人配置生成（去重 parses / flags / 顶层 spider）")
    personal = build_personal(entries, sites, [], "测试接口")
    check(personal["spider"] == "https://a.com/s.jar", "顶层 spider 取最快来源")
    check(len(personal["parses"]) == 2, "parses 去重合并 2 条，实际 %d" % len(personal["parses"]))
    check(personal["flags"] == ["youku", "qq"], "flags 合并去重")
    check("_src" not in personal["sites"][0], "内部标记 _src 已清除")
    check(all("jar" in s for s in personal["sites"]), "每个站点都有 jar")

    print("自检 5：配置类型识别")
    k, _ = parse_kind(b'{"spider":"x","sites":[{"key":"a","type":0,"api":"http://a.com"}]}')
    check(k == "单仓", "单仓识别：%s" % k)
    k, _ = parse_kind('{"urls":[{"name":"a","url":"http://a.com/1.json"}]}'.encode())
    check(k == "多仓", "多仓识别：%s" % k)
    k, _ = parse_kind(b"#EXTM3U\n#EXTINF:-1,CCTV1\nhttp://a.com/1.m3u8\n")
    check(k == "直播", "M3U 识别：%s" % k)
    k, _ = parse_kind(b"<html>404</html>")
    check(k == "无效", "HTML 识别为无效：%s" % k)

    print("\n自检结果：%s" % ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


def lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def serve_dir(port):
    """把输出目录挂成局域网 HTTP 服务，电视上直接填这个地址即可。"""
    import functools
    import http.server
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(OUT))
    ip = lan_ip()
    print("\n" + "=" * 74)
    print("局域网服务已启动，电视/手机配置地址填：")
    print("    http://%s:%d/personal.json" % (ip, port))
    print("（设备需与本机同一局域网；保持窗口不要关，Ctrl+C 结束）")
    print("=" * 74)
    try:
        http.server.ThreadingHTTPServer(("0.0.0.0", port), handler).serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")


# ============================ 主流程 ============================
def main():
    global OUT
    ap = argparse.ArgumentParser(description="个人专属 TVBox 接口：筛选 / 去重 / 留优")
    ap.add_argument("urls", nargs="*", help="接口地址（可多个，也支持粘贴到 stdin）")
    ap.add_argument("-i", "--input", help="地址文件，每行一条（默认 tools/my_api/urls.txt）")
    ap.add_argument("--out-dir", default=None, help="输出目录（默认 tools/my_api）")
    ap.add_argument("--rounds", type=int, default=2, help="每个地址测速轮数（默认 2）")
    ap.add_argument("--max-ms", type=float, default=5.0, help="卡顿阈值，秒（默认 5.0）")
    ap.add_argument("--timeout", type=int, default=12, help="单次请求超时，秒（默认 12）")
    ap.add_argument("--top-sources", type=int, default=0,
                    help="只融合最快的 N 个接口（0 = 全部通过筛选的都融合）")
    ap.add_argument("--max-sub", type=int, default=40, help="单个多仓最多展开多少条子线路")
    ap.add_argument("--max-sites", type=int, default=2500, help="融合站点数上限")
    ap.add_argument("--probe", choices=["host", "off", "api"], default="host",
                    help="站点存活检查粒度：host(默认) / api(更严格,会剔 404) / off")
    ap.add_argument("--live-top", type=int, default=5, help="写入配置的直播源条数（默认 5）")
    ap.add_argument("--public-base", default="", help="公网前缀，给出则额外生成 collection.json")
    ap.add_argument("--serve", type=int, default=0,
                    help="生成后用局域网 HTTP 服务对外提供（如 8000），电视直接填该地址")
    ap.add_argument("--keep-hosts", action="store_true",
                    help="融合多个来源时也继承 hosts 域名改写（默认只在单来源时继承）")
    ap.add_argument("--name", default="我的专属接口", help="配置名称（写进报告）")
    ap.add_argument("--selftest", action="store_true", help="不联网，只验证内部逻辑")
    args = ap.parse_args()

    if args.out_dir:
        OUT = pathlib.Path(args.out_dir)
    if args.selftest:
        sys.exit(selftest())

    inputs = load_inputs(args)
    if not inputs:
        print("没有读到任何接口地址。\n"
              "用法：python3 tools/make_my_api.py -i 地址.txt  （每行一条 URL）\n"
              "  或：python3 tools/make_my_api.py https://a.json https://b.txt\n"
              "  或：cat 地址.txt | python3 tools/make_my_api.py")
        sys.exit(2)

    print("=" * 74)
    print("个人专属接口 · 筛选去重留优")
    print("输入 %d 个地址 ｜ 测速 %d 轮 ｜ 卡顿阈值 %.1fs ｜ 存活检查 %s" % (
        len(inputs), args.rounds, args.max_ms, args.probe))
    print("=" * 74)

    records = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = [pool.submit(expand_seed, it, args, pool) for it in inputs]
        for i, job in enumerate(jobs, 1):
            rs = job.result()
            records.extend(rs)
            for r in rs:
                mark = "OK  " if r["keep"] else "DROP"
                print("[%2d/%2d] %s %-22s %-12s %6.2fs  %s" % (
                    i, len(inputs), mark, r["name"][:22], r["kind"], r["ms"],
                    ("；".join(r["reasons"])[:60] or "通过")))

    # 只融合通过筛选的单仓来源，按速度升序（快的站点排前面）
    entries = [r for r in records if r["keep"] and r["kind"] == "单仓" and r.get("cfg")]
    entries.sort(key=lambda r: r["ms"])

    # 接口级去重：同一配置换个 scheme / 换个镜像地址的，保留更快的那个。
    # 判重口径 = 归一化地址指纹（忽略 scheme 与 query）+ 配置内容 md5。
    seen_url, seen_hash, uniq = {}, {}, []
    for r in entries:
        uk = url_key(r["resolved_url"])
        chash = md5_bytes(json.dumps(r["cfg"], ensure_ascii=False,
                                     sort_keys=True).encode()) if r.get("cfg") else ""
        dup = ""
        if uk in seen_url:
            dup = "与「%s」是同一个地址" % seen_url[uk]
        elif chash and chash in seen_hash:
            dup = "与「%s」配置内容完全相同" % seen_hash[chash]
        if dup:
            r["keep"] = False
            r["level"] = "dead"
            r["reasons"].append(dup + "，保留响应更快的那个")
            continue
        seen_url[uk] = r["name"]
        if chash:
            seen_hash[chash] = r["name"]
        uniq.append(r)
    if len(uniq) != len(entries):
        print("接口级去重：%d → %d 个（同一配置的不同地址已合并）" % (len(entries), len(uniq)))
    entries = uniq

    if args.top_sources > 0:
        for r in entries[args.top_sources:]:
            r["keep"] = False
            r["reasons"].append("超出 --top-sources %d 的限制" % args.top_sources)
        entries = entries[:args.top_sources]

    if not entries:
        print("\n没有任何接口通过筛选，未生成配置。可尝试 --max-ms 8000 放宽阈值后重跑。")
        sys.exit(1)

    sites, stats, dropped = merge_sites(entries)
    stats["dead_host"] = 0
    if len(sites) > args.max_sites:
        print("站点数 %d 超过上限，截断到 %d" % (len(sites), args.max_sites))
        sites = sites[:args.max_sites]

    host_state = {}
    if args.probe != "off":
        hosts = sorted({host_of(norm(s.get("api"))) for s in sites} - {""})
        print("\n站点存活检查：%d 个域名（粒度 %s）..." % (len(hosts), args.probe))
        host_state = probe_hosts(hosts)
        dead = [h for h, v in host_state.items() if v["code"] == 0]
        sites, fstats = apply_host_filter(sites, host_state)
        print("  域名不可达 %d 个，剔除站点 %d 个" % (len(dead), fstats["dead_sites"]))
        if args.probe == "api":
            # 更严格：完整 api 地址返回 404/410 也算失效（默认不做，避免误杀需参数的接口）
            api_map = {}
            for s in sites:
                u = norm(s.get("api"))
                if u.startswith("http"):
                    api_map.setdefault(normalize(u), []).append(s)
            urls = sorted(api_map)[:300]
            print("  逐个 api 探测：%d 个地址..." % len(urls))
            api_state = probe_hosts(urls, timeout=6)
            bad = {u for u, v in api_state.items() if v["code"] in (0, 404, 410)}
            if bad:
                removed = sum(len(api_map[u]) for u in bad)
                sites = [s for s in sites
                         if not (norm(s.get("api")).startswith("http")
                                 and normalize(norm(s.get("api"))) in bad)]
                print("  api 级失效 %d 个，剔除站点 %d 个" % (len(bad), removed))
                stats["dead_host"] += removed
        stats["dead_host"] = stats.get("dead_host", 0) + fstats["dead_sites"]

    # 直播源：可用且最快的几条写进 lives
    live_recs = sorted([r for r in records if r["kind"] == "直播" and r.get("live_ok")],
                       key=lambda r: r["ms"])[: max(0, args.live_top)]
    lives = []
    for r in live_recs:
        lives.append({"name": r["name"][:24], "url": r["resolved_url"],
                      "_channels": r["live_channels"], "_ms": round(r["ms"], 2)})
    for l in lives:
        l.pop("_channels", None)
        l.pop("_ms", None)
    # 报告要显示频道数，重新取一份
    show_lives = [{"name": r["name"][:24], "url": r["resolved_url"],
                   "_channels": r["live_channels"], "_ms": round(r["ms"], 2)} for r in live_recs]

    personal = build_personal(entries, sites, lives, args.name, args.keep_hosts)
    stats["live_written"] = len(lives)
    write_outputs(args, inputs, records, entries, personal, show_lives, stats, host_state)

    kept_n = len([r for r in records if r["keep"] and r["kind"] == "单仓"])
    print("\n" + "-" * 74)
    print("通过筛选：%d 个接口 ｜ 融合站点：%d 个（原始 %d，去重 %d，缺字段 %d，域名挂 %d）" % (
        kept_n, len(personal["sites"]), stats["raw"],
        stats["dup_key"] + stats["dup_pair"], stats["skipped"], stats["dead_host"]))
    print("顶层 spider：%s" % (personal["spider"] or "无"))
    print("直播源：%d 条" % len(lives))
    print("\n最快的前 5 个接口：")
    for i, r in enumerate(entries[:5], 1):
        print("  %d. %-24s %5.2fs  %4d 站点  %s" % (i, r["name"][:24], r["ms"], r["sites"],
                                                   r["resolved_url"][:70]))
    print("\n产出：%s/personal.json（配到 TVBox 即是你的专属接口）" % OUT)
    print("      %s/report.html（逐个接口的筛选结论）" % OUT)
    print("      %s/kept_urls.txt（存活地址清单，下次复测直接复用）" % OUT)

    if args.serve:
        serve_dir(args.serve)


if __name__ == "__main__":
    main()
