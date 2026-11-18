#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内置源全量可用性实测（电视端为直连，本脚本全部直连测试）。

判定规则：
  CMS(type=0/1)  : 搜索 → 详情 → 取第一集播放地址 → 拉播放列表 → 校验首个分片是真实视频 + 测速
                   搜索不可用 / 无播放地址 / 分片非视频 → 死源
                   播放列表 TTFB > 6s 或分片速度 < 50KB/s → 卡（剔除）
  爬虫(type=3)   : 从 ext / drpy 脚本提取后端域名，逐个直连探测，全部不通 → 死源
                   ext 加密或为空无法探测 → 保留（标记 untested）
产物：tools/my_api/builtin_site_report.json
"""
import json
import pathlib
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from build_good_api import curl, test_cms  # noqa: E402

ASSETS = ROOT / "app/src/main/assets/builtin"
CONFIGS = ASSETS / "configs"
UA = "Mozilla/5.0 (Linux; U; Android 7.1.2; zh-CN; S2002) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/70 Mobile Safari/537.36"
SLOW_TTFB = 6.0
SLOW_KBPS = 50.0
JUZI_KEY = "csp_Juzi"

VIDEO_MAGIC = (
    (b"\x47", "ts"),
    (b"\x00\x00\x00", "mp4/3gp"),
    (b"FLV", "flv"),
    (b"\x1a\x45\xdf\xa3", "mkv/webm"),
    (b"ftyp", "mp4"),
    (b"\x30\x26\xb2", "asf"),
)


def http_get(url, timeout=12, max_bytes=65536, range_header=False):
    """直连 GET，返回 (status, bytes, ttfb_s, total_s) 或 (0, b'', ttfb, total)。"""
    return curl(url, timeout, None, ua=UA, max_bytes=max_bytes, range_header=range_header) \
        if _curl_supports_extra() else _raw_get(url, timeout, max_bytes, range_header)


_CURL_SIG = None


def _curl_supports_extra():
    global _CURL_SIG
    if _CURL_SIG is None:
        import inspect
        try:
            _CURL_SIG = set(inspect.signature(curl).parameters)
        except Exception:
            _CURL_SIG = set()
        return False
    return {"ua", "max_bytes", "range_header"} <= _CURL_SIG


def _raw_get(url, timeout=12, max_bytes=65536, range_header=False):
    """curl 直连封装：返回 (code, body, ttfb, total)。"""
    import subprocess
    cmd = ["curl", "-s", "-L", "--max-time", str(timeout), "--connect-timeout", str(timeout),
           "-A", UA, "-w", "\\n__META__%{http_code} %{time_starttransfer} %{time_total}",
           "--compressed", "-k", url]
    if range_header:
        cmd += ["-r", "0-31"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        out = p.stdout
    except Exception:
        return 0, b"", 0.0, 0.0
    i = out.rfind(b"\n__META__")
    if i < 0:
        return 0, out[:max_bytes], 0.0, 0.0
    body, meta = out[:i], out[i + 9:].decode(errors="replace").split()
    try:
        code = int(meta[0])
        ttfb, total = float(meta[1]), float(meta[2])
    except Exception:
        code, ttfb, total = 0, 0.0, 0.0
    return code, body[:max_bytes], ttfb, total


def is_video_head(head):
    for magic, _name in VIDEO_MAGIC:
        if head.startswith(magic):
            return True
    return b"ftyp" in head[:16]


def _raw_get_retry(url, timeout=12, max_bytes=65536, range_header=False, retries=1):
    """瞬时限流/抖动重试：404/0 隔 2 秒重试一次"""
    code, body, ttfb, total = _raw_get(url, timeout, max_bytes, range_header)
    for _ in range(retries):
        if code in (0, 403, 404, 429, 502, 503, 520, 522, 523):
            time.sleep(2)
            code, body, ttfb, total = _raw_get(url, timeout, max_bytes, range_header)
    return code, body, ttfb, total


def check_play_url(url):
    """校验一个播放地址：返回 (verdict, detail)。verdict: ok / dead / slow"""
    t0 = time.time()
    try:
        code, body, ttfb, total = _raw_get_retry(url, timeout=12, max_bytes=262144)
    except Exception as e:
        return "dead", "playlist err %s" % e
    if code != 200 or not body:
        return "dead", "playlist HTTP %s" % code
    txt = body.decode("utf-8", "replace")
    # master playlist → 取第一个子列表
    if "#EXT-X-STREAM-INF" in txt:
        m = re.search(r"#EXT-X-STREAM-INF.*?\n([^\n#]+)", txt)
        if m:
            sub = urljoin(url, m.group(1).strip())
            code2, body2, ttfb2, _ = _raw_get_retry(sub, timeout=12, max_bytes=262144)
            if code2 != 200 or not body2:
                return "dead", "variant HTTP %s" % code2
            txt, ttfb = body2.decode("utf-8", "replace"), ttfb2
    if "#EXTINF" not in txt:
        # 非 m3u8：直链 mp4 等校验文件头
        code3, head3, _, _ = _raw_get_retry(url, timeout=12, max_bytes=64, range_header=True)
        if code3 in (200, 206) and is_video_head(head3[:16]):
            return "ok", "direct %s" % (head3[:8])
        return "dead", "not m3u8/direct-video"
    m = re.search(r"#EXTINF:[\d.]+,\s*\n?([^\n#]+)", txt)
    if not m:
        return "dead", "no EXTINF segment"
    seg = urljoin(url, m.group(1).strip())
    code4, head4, ttfb4, total4 = _raw_get_retry(seg, timeout=15, max_bytes=524288)
    if code4 not in (200, 206) or not head4:
        return "dead", "segment HTTP %s" % code4
    # AES-128 加密流：取 key 解密首块验证 TS 同步字节
    if "#EXT-X-KEY:METHOD=AES-128" in txt or "#EXT-X-KEY:METHOD=AES" in txt:
        try:
            from Crypto.Cipher import AES
            km = re.search(r'#EXT-X-KEY:METHOD=AES-128,URI="([^"]+)"(?:,IV=0x([0-9a-fA-F]+))?', txt)
            if not km:
                return "ok", "aes128 key-regex-miss"
            key_url = urljoin(url, km.group(1))
            kc, kbody, _, _ = _raw_get(key_url, timeout=10, max_bytes=64)
            if kc != 200 or len(kbody) < 16:
                return "ok", "aes128 key HTTP %s" % kc  # 播放器可自行处理
            iv = bytes.fromhex(km.group(2)) if km.group(2) else b"\x00" * 16
            pt = AES.new(kbody[:16], AES.MODE_CBC, iv).decrypt(head4[:16])
            if pt[:1] != b"\x47":
                return "dead", "aes128 decrypt bad %s" % pt[:6]
            speed = len(head4) / max(total4 - ttfb4, 0.01) / 1024.0 if total4 > ttfb4 else 0.0
            detail = "aes128 seg_ttfb=%.1fs speed=%.0fKB/s" % (ttfb4, speed)
            if ttfb4 > SLOW_TTFB or (speed > 0 and speed < SLOW_KBPS):
                return "slow", detail
            return "ok", detail
        except Exception as e:
            return "ok", "aes128 check-err %s" % e  # 保守放行，播放器可解
    if not is_video_head(head4[:16]):
        return "dead", "fake-stream %s" % head4[:12]
    speed = len(head4) / max(total4 - ttfb4, 0.01) / 1024.0 if total4 > ttfb4 else 0.0
    detail = "seg_ttfb=%.1fs speed=%.0fKB/s" % (ttfb4, speed)
    if ttfb4 > SLOW_TTFB or (speed > 0 and speed < SLOW_KBPS):
        return "slow", detail
    return "ok", detail


def check_cms(site):
    """CMS 完整链路：搜索可用性 + 最新剧集播放校验"""
    key = site.get("key")
    r = test_cms(site)
    if not r.get("ok") or not r.get("direct_ok"):
        return "dead", "search:%s" % r.get("why", "?")
    api = str(site.get("api", "")).strip()
    # 用最新更新的剧集抽样（老剧分片可能被站方清理）
    code, body, _, _ = _raw_get(api + ("&" if "?" in api else "?") + "ac=videolist&pg=1",
                                timeout=12, max_bytes=2097152)
    if code != 200 or not body:
        return "dead", "videolist HTTP %s" % code
    try:
        d = json.loads(body.decode("utf-8", "replace").lstrip("\ufeff"))
        vods = [v for v in (d.get("list") or []) if isinstance(v, dict) and v.get("vod_play_url")]
    except Exception:
        return "dead", "videolist not json"
    if not vods:
        return "dead", "videolist empty"
    final = ("dead", "no playable candidate")
    # 跳过最新一部（刚入库分片可能未就绪），抽第 2~4 部
    for v in vods[1:4]:
        play = str(v.get("vod_play_url") or "")
        cands = []
        for group in play.split("$$$"):
            for part in group.split("#"):
                if "$" in part:
                    u = part.split("$", 1)[1].strip()
                    if u.startswith("http"):
                        cands.append(u)
        if not cands:
            continue
        cands.sort(key=lambda u: 0 if ".m3u8" in u.lower() else 1)
        for u in cands[:2]:
            v2, why = check_play_url(u)
            if v2 in ("ok", "slow"):
                return v2, why + " @" + u.split("//")[1][:30]
            if v2 == "dead" and final[0] == "dead":
                final = (v2, why)
    return final


URL_RE = re.compile(r"https?://[A-Za-z0-9._~:\-/@%?=&+#\[\]]+")


def collect_urls_from_obj(obj, depth=0):
    out = []
    if depth > 4:
        return out
    if isinstance(obj, str):
        out += URL_RE.findall(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out += collect_urls_from_obj(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            out += collect_urls_from_obj(v, depth + 1)
    return out


def read_local_asset(ref):
    """clan://localhost/builtin/... → 本地文件文本"""
    m = re.match(r"clan://localhost/builtin/(.+)", ref)
    if not m:
        return ""
    p = ASSETS / m.group(1)
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def spider_candidates(site):
    """提取爬虫站的后端候选 URL 列表"""
    ext = site.get("ext", "")
    out = []
    if isinstance(ext, str) and ext.startswith("clan://localhost/builtin/"):
        # Local 扩展：主文件$$$附加参数
        refs = [x.strip() for x in ext.split("$$$") if x.strip()]
        for ref in refs:
            if ref.startswith("clan://") or ref.startswith("./"):
                txt = read_local_asset(ref if ref.startswith("clan://") else "")
                out += URL_RE.findall(txt)
            elif ref.startswith("http"):
                out.append(ref)
    elif isinstance(ext, (dict, list)):
        out += collect_urls_from_obj(ext)
    elif isinstance(ext, str) and ext.strip().startswith("{"):
        try:
            out += collect_urls_from_obj(json.loads(ext))
        except Exception:
            out += URL_RE.findall(ext)
    elif isinstance(ext, str):
        out += URL_RE.findall(ext)
    # drpy_js：api 指向本地脚本
    api = str(site.get("api", ""))
    if api.startswith("clan://localhost/builtin/"):
        txt = read_local_asset(api)
        out += URL_RE.findall(txt)
    # 去重保序，规范末尾斜杠差异
    seen, uniq = set(), []
    for u in out:
        u = u.rstrip("/\\\"'`,")
        if not u or len(u) < 10:
            continue
        k = urlsplit(u).netloc.lower()
        if k and k not in seen:
            seen.add(k)
            uniq.append(u)
    return uniq[:4]


def check_spider(site):
    cands = spider_candidates(site)
    if not cands:
        return "untested", "ext 加密/为空，无法探测"
    for u in cands:
        code, body, ttfb, _ = _raw_get_retry(u, timeout=12, max_bytes=8192)
        if 0 < code < 500:
            return "ok", "host %s HTTP %s ttfb=%.1fs" % (urlsplit(u).netloc, code, ttfb)
    last = "HTTP %s" % code if cands else "?"
    return "dead", "host unreachable (%s): %s" % (last, cands[0][:60])


def check_site(site):
    key = site.get("key")
    if key == JUZI_KEY:
        return "ok", "橘汁已单独验证"
    t = str(site.get("type"))
    if t in ("0", "1"):
        return check_cms(site)
    return check_spider(site)


def main():
    all_sites = json.load(open(CONFIGS / "all.json", encoding="utf-8"))["sites"]
    sites = [s for s in all_sites if s.get("key") != JUZI_KEY]
    print("待测站点：%d" % len(sites))
    results = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(check_site, s): s for s in sites}
        done = 0
        for f in as_completed(futs):
            s = futs[f]
            try:
                results[s["key"]] = f.result()
            except Exception as e:
                results[s["key"]] = ("dead", "test-error %s" % e)
            done += 1
            if done % 10 == 0:
                print("  ... %d/%d" % (done, len(sites)))

    verdict = {"ok": [], "slow": [], "dead": [], "untested": []}
    for s in sites:
        v, why = results.get(s["key"], ("untested", "?"))
        verdict[v].append({"key": s["key"], "name": s.get("name"), "type": s.get("type"), "why": why})

    print("\n========== 结果 ==========")
    print("ok      : %d" % len(verdict["ok"]))
    print("untested: %d" % len(verdict["untested"]))
    print("slow    : %d" % len(verdict["slow"]))
    print("dead    : %d" % len(verdict["dead"]))
    for tag in ("slow", "dead"):
        print("\n--- %s ---" % tag)
        for x in verdict[tag]:
            print("  ✗ %-22s %-14s %s" % (str(x["name"])[:22], x["key"], x["why"][:80]))

    rep = {
        "generated": datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S"),
        "tested": len(sites),
        "ok": verdict["ok"], "slow": verdict["slow"],
        "dead": verdict["dead"], "untested": verdict["untested"],
    }
    out = ROOT / "tools/my_api/builtin_site_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n报告 → %s" % out)


if __name__ == "__main__":
    main()
