#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线内置源 · 第一步：把筛出的可用链接原始内容保存到本地。

输入：tools/repo_sources_valid.json（三个仓库的检测结果）
      tools/offline_sources/optimized.json（旧 5 条内置源的来源信息）

输出：tools/offline_sources/raw/repo_*.txt   新点播配置的原始快照
      tools/offline_sources/raw/live_*.txt   直播 M3U 的原始快照
      tools/offline_sources/candidates.json  合并后的统一候选清单（供 optimize 使用）

筛选口径（用户要求的「过筛」）：
  - 保留：单仓且 spider jar 验证通过；直播列表单独一组。
  - 剔除：spider 404 的配置（采集站不可用）、内容重复项、失效地址、
          以及标注为 18+ 的多仓线路（内容合规）。
  - 原在线备用线路不再保留，全部走本地文件。
"""
import hashlib
import json
import pathlib
import subprocess
from datetime import datetime
from encodings import idna
from urllib.parse import quote, urlsplit, urlunsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = ROOT / "tools" / "offline_sources"
RAW = BASE / "raw"
PROXY = "socks5h://127.0.0.1:10808"
UA = "okhttp/3.15"
PROXY_FIRST = ("raw.githubusercontent.com", "github.com", "gh-proxy.com",
               "cdn.jsdelivr.net", "jihulab.com", "agit.ai")


def normalize(url):
    """中文域名转 punycode、路径做 URL 编码，保证 curl 能直接请求。"""
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
    """返回 {code, body, time}；境外域名优先代理，其余直连优先。"""
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


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    valid = json.loads((ROOT / "tools" / "repo_sources_valid.json").read_text(encoding="utf-8"))

    point, live, excluded = [], [], []

    # 旧 5 条：沿用已保存的原始快照，不重新下载
    old_opt = BASE / "optimized.json"
    if old_opt.exists():
        for i, it in enumerate(json.loads(old_opt.read_text(encoding="utf-8"))["kept"], 1):
            point.append({
                "key": "o%d" % i,
                "name": it["name"],
                "url": it["origin"],
                "fetch_url": normalize(it["origin"]),
                "raw": it["raw_file"],
                "group": "旧·内置源",
            })

    for x in valid["valid"] + valid["duplicates"] + valid["invalid"]:
        if x["kind"] == "单仓" and x.get("spider_ok") is True:
            if x["level"] == "dup":
                excluded.append({"name": x["name"], "url": x["url"], "why": "内容重复，已保留更快的入口"})
                continue
            idx = len(point) - 4  # 新点播配置从 repo_1 开始编号
            key = "r%d" % idx
            raw = "raw/repo_%d.txt" % idx
            body_file = RAW / ("repo_%d.txt" % idx)
            r = fetch(x["url"])
            if r["code"] != 200 or not r["body"]:
                excluded.append({"name": x["name"], "url": x["url"],
                                 "why": "快照下载失败 HTTP %s" % r["code"]})
                continue
            body_file.write_bytes(r["body"])
            point.append({
                "key": key,
                "name": x["name"],
                "url": x["url"],
                "fetch_url": normalize(x["url"]),
                "raw": raw,
                "group": "新·三仓库",
                "sites": x.get("sites", 0),
                "ms": x.get("ms", 0),
                "md5": hashlib.md5(r["body"]).hexdigest(),
                "size": len(r["body"]),
            })
            print("[点播] %-14s %6d B -> %s" % (x["name"], len(r["body"]), raw), flush=True)
        elif x["kind"] == "直播列表":
            idx = len(live) + 1
            raw = "raw/live_%d.txt" % idx
            r = fetch(x["url"])
            if r["code"] != 200 or not r["body"]:
                excluded.append({"name": x["name"], "url": x["url"],
                                 "why": "直播快照下载失败 HTTP %s" % r["code"]})
                continue
            (RAW / ("live_%d.txt" % idx)).write_bytes(r["body"])
            live.append({
                "key": "l%d" % idx,
                "name": x["name"],
                "url": x["url"],
                "fetch_url": normalize(x["url"]),
                "raw": raw,
                "local": "clan://localhost/builtin/live/l%d.txt" % idx,
                "ms": x.get("ms", 0),
                "size": len(r["body"]),
                "md5": hashlib.md5(r["body"]).hexdigest(),
            })
            print("[直播] %-14s %6d B -> %s" % (x["name"], len(r["body"]), raw), flush=True)
        else:
            why = ("spider jar 已失效（%s）" % x.get("spider_detail", "404")
                   if x["kind"] == "单仓" else
                   "多仓线路内容标注为 18+，按内容合规剔除" if x["kind"] == "多仓" else
                   x.get("detail", "失效"))
            excluded.append({"name": x["name"], "url": x["url"], "why": why})

    # 直播按检测耗时升序，第一条会作为默认直播地址
    live.sort(key=lambda x: x["ms"])
    for i, x in enumerate(live, 1):
        x["local"] = "clan://localhost/builtin/live/l%d.txt" % i

    out = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "point": point,
        "live": live,
        "excluded": excluded,
    }
    (BASE / "candidates.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n候选：点播 %d 条（旧 %d + 新 %d）、直播 %d 条、剔除 %d 条" % (
        len(point), len(point) - max(0, len(point) - 5) if len(point) <= 5 else 5,
        len(point) - 5, len(live), len(excluded)))
    print("产出：tools/offline_sources/candidates.json + raw/ 快照")


if __name__ == "__main__":
    main()
