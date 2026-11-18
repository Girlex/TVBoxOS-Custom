#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 personal.json（1282 站点）整理成一份「真正好用」的精简接口。

「好用」= 打开就能搜到片 + 不卡。做法：
  1) CMS 站点（type=1，api 是 provide/vod）：用热门词「庆余年」真实搜索实测，
     记录能否返回带播放源的结果、结果数、耗时，按质量打分排序。
  2) csp / 爬虫站点（type=3）：无法离线跑 JS，按「影视 vs 工具/低质」分类，
     剔除音乐/推送/商店/教学/体育/动漫/女团/设置/文件/片单/直播等非影视源，
     影视源再按 csp 类名归类、同名类名限量，避免几百个重复马甲。
  3) 按来源接口可信度 + 实测质量排序，生成精简 good.json + good_report.html。

产物：tools/my_api/good.json、tools/my_api/good_report.html
用法：
    python3 tools/build_good_api.py                    # 默认实测 + 精简
    python3 tools/build_good_api.py --no-test          # 跳过实测（快，仅按规则分类）
    python3 tools/build_good_api.py --max-cms 60 --max-spider 120
"""
import argparse
import html
import json
import pathlib
import re
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit, parse_qsl, urlencode

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "tools" / "my_api"
PROXY = "socks5h://127.0.0.1:10808"
UA = "okhttp/3.15"
TEST_WORD = "庆余年"
# 假搜索校验词：绝不可能存在的乱码。正常 CMS 搜它应返回空；
# 若某站点用它仍返回与真实词相同的一批结果 → 该站忽略 wd，是"假搜索"站，必须剔除。
FAKE_PROBE_WORD = "zzzzqqqq不存在987654321"

# ---------- 分类规则 ----------
# 名称里出现这些 → 非影视 / 低质 / 工具站，剔除
BAD_NAME = re.compile(
    r"(音乐|无损|推送|接收|商店|市场|教学|家庭|体育|飞球|女团|热舞|跳舞|动漫|漫画|小说|"
    r"设置|中心|配置|文件|浏览|本地|片单|书单|豆瓣榜|榜单|工具|说明|教程|直播|卫视|"
    r"儿童英语|少儿英语|启蒙|教育|课程| Kuruluş|动态|朋友圈|短句|朋友圈文案)", re.I)
# 明确「影视正向」词，命中则优先保留（即使名字里有其它符号）
GOOD_NAME = re.compile(r"(影视|电影|电视剧|剧集|影院|高清|秒播|资源|采集|云播|大片|连续剧|追剧|网剧|院线|4K|蓝光|超清)", re.I)
# csp 类名 → 明确非影视的爬虫类
BAD_CSP = re.compile(
    r"(Music|Push|Market|FirstAid|Bili|Douban|PianDan|Local|Config|Setting|File|"
    r"Sport|Anime|Dongman|Manhua|Novel|Book|Jiaoyu|Edu|Kids|Child|English|Live|"
    r"Wogg|Whitelist|Tool|Test|Demo|Update|Help)", re.I)
# csp 类名 → 影视采集类（保留）
GOOD_CSP = re.compile(r"(XBPQ|HBPQ|YsV2|Ys|AppYs|AppGet|So|Sou|Search|Pan|Yun|Vod|PVod|Guard|Jpys|Csp|Kuai|收藏|影视)", re.I)

KEEP_TYPE = (1, 3)  # 只保留这两类做影视源；0/4(直播/其它)单独处理


def esc(s):
    return html.escape(str(s), quote=True)


def curl(url, timeout=10, proxy=None):
    cmd = ["curl", "-sS", "-L", "--compressed", "--max-time", str(timeout),
           "-A", UA, "-H", "Accept: */*"]
    cmd += (["--proxy", proxy] if proxy else ["--noproxy", "*"])
    cmd += ["-w", "\n__M__%{http_code}|%{time_starttransfer}|%{time_total}", url]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 4)
    except subprocess.TimeoutExpired:
        return 0, b"", float(timeout), float(timeout)
    out = r.stdout or b""
    if b"__M__" not in out:
        return 0, b"", 0.0, 0.0
    body, meta = out.rsplit(b"__M__", 1)
    body = body[:-1] if body.endswith(b"\n") else body
    p = meta.decode("utf-8", "replace").strip().split("|")
    try:
        code = int(p[0] or 0)
    except Exception:
        code = 0
    try:
        ttfb = float(p[1]) if len(p) > 1 else 0.0
    except Exception:
        ttfb = 0.0
    try:
        total = float(p[2]) if len(p) > 2 else 0.0
    except Exception:
        total = 0.0
    return code, body, ttfb, total


def build_search_url(api, word):
    """构造 CMS 搜索 URL：保留原路径，强制 ac=videolist&wd=<词>。"""
    sp = urlsplit(api)
    q = dict(parse_qsl(sp.query, keep_blank_values=True))
    q.pop("ac", None)
    q["ac"] = "videolist"
    q["wd"] = word
    return urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(q), ""))


def test_cms(site, word=TEST_WORD, timeout=10):
    """实测一个 CMS 站点的搜索能力。返回 dict。"""
    api = str(site.get("api", "")).strip()
    if not api.startswith("http"):
        return {"ok": False, "why": "api 非 URL"}
    search_url = build_search_url(api, word)

    # 直连和代理各测一次：电视端多为直连，直连可用才算真正好用
    direct = curl(search_url, timeout, None)
    proxy_r = curl(search_url, timeout, PROXY)
    def usable(r):
        return r[0] in (200, 206) and bool(r[1])
    if usable(direct):
        code, body, ttfb, total, via = direct[0], direct[1], direct[2], direct[3], "直连"
    elif usable(proxy_r):
        code, body, ttfb, total, via = proxy_r[0], proxy_r[1], proxy_r[2], proxy_r[3], "代理"
    else:
        bad = direct if direct[0] else proxy_r
        return {"ok": False, "why": "HTTP %s" % (bad[0] or 0),
                "ttfb": bad[2], "via": "直连", "direct_ok": False, "proxy_ok": False}
    direct_ok = usable(direct)
    proxy_ok = usable(proxy_r)

    txt = body.decode("utf-8", "replace").lstrip("\ufeff").strip()
    try:
        d = json.loads(txt)
    except Exception:
        # 容错：截取第一个 { 到最后一个 }
        i, j = txt.find("{"), txt.rfind("}")
        try:
            d = json.loads(txt[i:j + 1]) if 0 <= i < j else None
        except Exception:
            d = None
    if not isinstance(d, dict):
        return {"ok": False, "why": "非 JSON", "ttfb": ttfb, "via": via,
                "direct_ok": direct_ok, "proxy_ok": proxy_ok}
    lst = d.get("list")
    if not isinstance(lst, list) or not lst:
        return {"ok": False, "why": "搜索无结果", "ttfb": ttfb, "via": via,
                "direct_ok": direct_ok, "proxy_ok": proxy_ok}
    # 计算有播放源的条目数
    with_play = 0
    for v in lst:
        if not isinstance(v, dict):
            continue
        pu = str(v.get("vod_play_url") or v.get("vod_play_from") or "")
        if pu and pu.strip() not in ("", "[]", "{}"):
            with_play += 1
    total_cnt = d.get("total")
    try:
        total_cnt = int(total_cnt)
    except Exception:
        total_cnt = len(lst)
    if with_play == 0 and total_cnt == 0:
        return {"ok": False, "why": "结果无播放源", "ttfb": ttfb, "via": via,
                "direct_ok": direct_ok, "proxy_ok": proxy_ok}

    # ---- 假搜索校验 ----
    # 真 CMS 站用乱码词搜索应返回空；若仍返回与真实词高度重合的一批结果，
    # 说明该站忽略 wd 参数、永远吐首页推荐流，会把无关内容混进搜索页，必须剔除。
    titles_a = set(str(v.get("vod_name", "")).strip()
                   for v in lst if isinstance(v, dict)) if isinstance(lst, list) else set()
    fake_url = build_search_url(api, FAKE_PROBE_WORD)
    fake_resp = curl(fake_url, timeout, None)
    if not (fake_resp[0] in (200, 206) and fake_resp[1]):
        fake_resp = curl(fake_url, timeout, PROXY)
    if fake_resp[0] in (200, 206) and fake_resp[1]:
        try:
            fd = json.loads(fake_resp[1].decode("utf-8", "replace").lstrip("\ufeff").strip())
        except Exception:
            fd = None
        if isinstance(fd, dict) and isinstance(fd.get("list"), list):
            fl = fd["list"]
            titles_b = set(str(v.get("vod_name", "")).strip()
                           for v in fl if isinstance(v, dict))
            overlap = len(titles_a & titles_b)
            # 乱码词也返回 >=4 条，且与真实词重合 >=3 条 → 判定为假搜索
            if len(fl) >= 4 and overlap >= 3:
                return {"ok": False, "why": "假搜索(忽略wd)", "ttfb": ttfb, "via": via,
                        "direct_ok": direct_ok, "proxy_ok": proxy_ok, "fake": True}

    # 打分：有播放源的结果越多越好，耗时越短越好；直连额外加 30 分（电视端多为直连）
    score = min(with_play, 20) * 4 + min(total_cnt, 500) / 10.0 - ttfb * 6
    if direct_ok:
        score += 30
    return {"ok": True, "hits": len(lst), "with_play": with_play, "total": total_cnt,
            "ttfb": round(ttfb, 3), "via": via, "score": round(score, 2),
            "direct_ok": direct_ok, "proxy_ok": proxy_ok}


def classify_spider(site):
    """判断爬虫站点是否为可用影视源。返回 (keep, reason, category)。"""
    name = str(site.get("name", ""))
    api = str(site.get("api", ""))
    csp = api[4:] if api.startswith("csp_") else ""

    # 1) 非影视关键词（名称）
    if BAD_NAME.search(name):
        return False, "非影视/工具站", "工具"
    # 2) csp 类名明确非影视
    if csp and BAD_CSP.search(csp) and not GOOD_NAME.search(name):
        return False, "爬虫类非影视", "工具"
    # 3) 影视正向命中
    if GOOD_NAME.search(name) or (csp and GOOD_CSP.search(csp)):
        return True, "影视源", "影视"
    # 4) api 是 JS URL 的无法分类，按名称中性保留为「其它」
    if api.startswith("http"):
        return True, "脚本站点(未实测)", "其它"
    # 5) csp 类但名字中性：保留（多数是影视马甲）
    if csp:
        return True, "爬虫影视马甲", "影视"
    return False, "无法分类", "工具"


def parse_args():
    ap = argparse.ArgumentParser(description="整理一份真正好用的精简影视接口")
    ap.add_argument("--input", default=str(OUT / "personal.json"), help="输入配置")
    ap.add_argument("--out-dir", default=str(OUT), help="输出目录")
    ap.add_argument("--no-test", action="store_true", help="跳过 CMS 实测（快速模式）")
    ap.add_argument("--test-word", default=TEST_WORD, help="实测搜索词")
    ap.add_argument("--timeout", type=int, default=10, help="单次请求超时秒数")
    ap.add_argument("--workers", type=int, default=12, help="实测并发数")
    ap.add_argument("--max-cms", type=int, default=80, help="最多保留多少个 CMS 站点")
    ap.add_argument("--max-spider", type=int, default=180, help="最多保留多少个爬虫影视站点")
    ap.add_argument("--per-class", type=int, default=12, help="同一 csp 类最多保留几个马甲")
    ap.add_argument("--keep-proxy-only", action="store_true",
                    help="保留仅代理可用的 CMS 站点（默认剔除，电视端通常无代理）")
    return ap.parse_args()


def main():
    args = parse_args()
    src = pathlib.Path(args.input)
    outdir = pathlib.Path(args.out_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(src.read_text(encoding="utf-8"))
    sites = cfg.get("sites", [])
    print("=" * 70)
    print("好用接口整理 ｜ 输入 %d 个站点 ｜ 搜索词「%s」" % (len(sites), args.test_word))
    print("=" * 70)

    cms_sites = [s for s in sites if s.get("type") == 1 and
                 str(s.get("api", "")).startswith("http")]
    spider_sites = [s for s in sites if s.get("type") == 3]
    other_sites = [s for s in sites if s.get("type") not in (1, 3)]
    print("分类：CMS %d / 爬虫 %d / 其它 %d" % (len(cms_sites), len(spider_sites), len(other_sites)))

    # ---- 1) CMS 实测 ----
    cms_results = []
    if args.no_test:
        for s in cms_sites:
            cms_results.append((s, {"ok": True, "hits": 0, "with_play": 0,
                                    "total": 0, "ttfb": 0.0, "via": "-", "score": 0,
                                    "why": "跳过实测"}))
        print("跳过实测（--no-test）")
    else:
        print("\nCMS 站点实测搜索（并发 %d）..." % args.workers)
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(test_cms, s, args.test_word, args.timeout): s for s in cms_sites}
            done = 0
            for f in as_completed(futs):
                s = futs[f]
                try:
                    res = f.result()
                except Exception as e:
                    res = {"ok": False, "why": "异常:%s" % str(e)[:30]}
                cms_results.append((s, res))
                done += 1
                if done % 20 == 0:
                    print("  实测 %d/%d ..." % (done, len(cms_sites)))
    ok_cms = [(s, r) for s, r in cms_results if r.get("ok")]
    bad_cms = [(s, r) for s, r in cms_results if not r.get("ok")]
    # 仅代理可用的站点电视端直连会用不了，默认移到剔除列表（除非显式保留）
    if not args.keep_proxy_only:
        proxy_only = [(s, r) for s, r in ok_cms if not r.get("direct_ok", True)]
        if proxy_only:
            ok_cms = [(s, r) for s, r in ok_cms if r.get("direct_ok", True)]
            bad_cms += [(s, dict(r, why="仅代理可用(电视直连不可用)")) for s, r in proxy_only]
            print("  剔除仅代理可用站点 %d 个（电视端多为直连）" % len(proxy_only))
    ok_cms.sort(key=lambda x: -x[1].get("score", 0))
    print("  CMS 实测通过 %d / 剔除 %d" % (len(ok_cms), len(bad_cms)))

    # ---- 2) 爬虫站点分类 ----
    spider_ok, spider_bad, cat_cnt = [], [], Counter()
    for s in spider_sites:
        keep, why, cat = classify_spider(s)
        cat_cnt[cat] += 1
        if keep:
            spider_ok.append(s)
        else:
            spider_bad.append((s, why))
    # 同一 csp 类限量
    by_class = defaultdict(list)
    for s in spider_ok:
        api = str(s.get("api", ""))
        cls = api if api.startswith("csp_") else "JS:" + api[:60]
        by_class[cls].append(s)
    spider_final = []
    for cls, group in by_class.items():
        # 组内按名称长度短、无乱码优先（粗略质量排序）
        group.sort(key=lambda s: (len(str(s.get("name", ""))), str(s.get("name", ""))))
        spider_final.extend(group[:args.per_class])
    # 影视类优先，再按类名频次
    def spider_rank(s):
        api = str(s.get("api", ""))
        cls = api if api.startswith("csp_") else "JS:" + api[:60]
        name = str(s.get("name", ""))
        tier = 0 if GOOD_NAME.search(name) else 1
        return (tier, -len(by_class.get(cls, [])), name)
    spider_final.sort(key=spider_rank)
    spider_final = spider_final[:args.max_spider]
    print("爬虫影视站点：保留 %d / 剔除 %d（分类 %s）" %
          (len(spider_final), len(spider_bad), dict(cat_cnt)))

    # ---- 3) 融合 ----
    kept = [dict(s) for s, _ in ok_cms[:args.max_cms]] + [dict(s) for s in spider_final]
    # 去重（key / name+api）
    seen_key, seen_pair, final_sites = set(), set(), []
    for s in kept:
        k = str(s.get("key", ""))
        pair = (str(s.get("name", "")), str(s.get("api", "")).rstrip("/"))
        if (k and k in seen_key) or pair in seen_pair:
            continue
        if k:
            seen_key.add(k)
        seen_pair.add(pair)
        s.pop("_src", None)
        final_sites.append(s)

    out = {
        "spider": cfg.get("spider", ""),
        "wallpaper": cfg.get("wallpaper", ""),
        "logo": cfg.get("logo", ""),
        "lives": cfg.get("lives", []),
        "sites": final_sites,
        "_meta": {
            "name": "好用精简接口",
            "generated": datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S"),
            "source_sites": len(sites),
            "cms_tested": len(cms_results), "cms_ok": len(ok_cms),
            "spider_kept": len(spider_final), "spider_dropped": len(spider_bad),
            "final_sites": len(final_sites),
            "test_word": args.test_word,
        },
    }
    (outdir / "good.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n产出：%s/good.json（%d 个影视站点，%d 条直播）" %
          (outdir, len(final_sites), len(out["lives"])))

    write_report(outdir, args, cfg, cms_results, ok_cms, bad_cms,
                 spider_ok, spider_bad, spider_final, cat_cnt, final_sites)
    print("报告：%s/good_report.html" % outdir)
    return 0


def write_report(outdir, args, cfg, cms_results, ok_cms, bad_cms,
                 spider_ok, spider_bad, spider_final, cat_cnt, final_sites):
    def rows(items, klass=""):
        out = []
        for s, r in items:
            out.append("<tr class='%s'><td>%s</td><td class='mono'>%s</td>"
                       "<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                           klass, esc(s.get("name", "")), esc(str(s.get("api", ""))[:80]),
                           esc(r.get("why", "")), esc(r.get("via", "-")),
                           esc(r.get("ttfb", "-")), esc(r.get("score", "-"))))
        return "".join(out)

    cms_ok_shown = ok_cms[:args.max_cms]
    cms_bad_shown = bad_cms
    spider_bad_shown = spider_bad[:200]
    doc = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>好用接口整理报告</title><style>
*{box-sizing:border-box}body{margin:0;padding:32px 20px 60px;background:#f5f6f8;color:#1f2329;
 font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.6}
.wrap{max-width:1120px;margin:0 auto}h1{font-size:23px;margin:0 0 4px}
h2{font-size:16px;margin:24px 0 10px}p.sub{color:#6b7280;font-size:13px;margin-top:0}
.card{background:#fff;border:1px solid #e6e8ec;border-radius:12px;padding:16px 18px;margin-bottom:16px}
table{width:100%%;border-collapse:collapse;font-size:12.5px}
th,td{padding:7px 8px;border-bottom:1px solid #eef0f3;text-align:left;vertical-align:top}
th{background:#fafbfc;color:#6b7280;font-weight:600;position:sticky;top:0}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;word-break:break-all;font-size:11.5px}
tr.drop td{color:#98a2b3}.stat{display:inline-block;background:#eff8ff;color:#175cd3;
 border-radius:8px;padding:6px 12px;margin:3px 6px 3px 0;font-size:13px;font-weight:600}
.box{border-left:4px solid #175cd3;background:#eff8ff;border-radius:10px;
 padding:14px 18px;font-size:13.5px;margin-bottom:18px}
.warn{border-left-color:#d92d20;background:#fef3f2;color:#b42318}
</style></head><body><div class="wrap">
<h1>好用接口 · 整理报告</h1>
<p class="sub">%s ｜ 原始 %d 站点 → 精简 <b>%d</b> 站点 ｜ 搜索词「%s」</p>
<div class="box warn"><b>为什么精简：</b>1282 个站点里大量是马甲、工具站（音乐/推送/商店/教学/片单/设置）
和失效采集源，全部塞给电视会导致搜索慢、结果乱。本版只保留实测能搜到片的 CMS 站 + 影视类爬虫源，
并限制同一类马甲数量，做到「打开就能用、搜片快」。</div>
<div class="card">
<span class="stat">CMS 实测 %d</span><span class="stat">实测通过 %d</span>
<span class="stat">CMS 剔除 %d</span><span class="stat">爬虫保留 %d</span>
<span class="stat">爬虫剔除 %d</span><span class="stat">最终 %d</span>
</div>
<h2>CMS 站点（真实搜索实测，按质量排序）</h2>
<div class="card" style="padding:0;overflow:auto;max-height:520px"><table>
<thead><tr><th>名称</th><th>接口</th><th>结论</th><th>线路</th><th>首字节s</th><th>得分</th></tr></thead>
<tbody>%s%s</tbody></table></div>
<h2>爬虫影视站点分类统计</h2>
<div class="card">%s</div>
<h2>最终保留的爬虫影视站点（前 80）</h2>
<div class="card" style="padding:0;overflow:auto;max-height:520px"><table>
<thead><tr><th>名称</th><th>api/类</th></tr></thead><tbody>%s</tbody></table></div>
<h2>被剔除的爬虫站点（前 200）</h2>
<div class="card" style="padding:0;overflow:auto;max-height:360px"><table>
<thead><tr><th>名称</th><th>api/类</th><th>原因</th></tr></thead><tbody>%s</tbody></table></div>
<div class="box"><b>怎么用：</b>把 <code>good.json</code> 配到 TVBox 即是精简接口。
不能直接填 <code>/sdcard/</code> 路径（客户端会补 <code>http://</code>），
用 <code>python3 tools/make_my_api.py --serve 8000</code> 起局域网服务，或传到静态托管填 URL。
直播在 <code>lives</code> 字段里，客户端一般会自动读取。</div>
</div></body></html>""" % (
        esc(datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M")),
        len(cfg.get("sites", [])), len(final_sites), esc(args.test_word),
        len(cms_results), len(ok_cms), len(bad_cms), len(spider_final), len(spider_bad),
        len(final_sites),
        rows(cms_ok_shown), rows(cms_bad_shown, "drop"),
        "".join("<span class='stat'>%s %d</span>" % (esc(k), v) for k, v in sorted(cat_cnt.items())),
        "".join("<tr><td>%s</td><td class='mono'>%s</td></tr>" %
                (esc(s.get("name", "")), esc(str(s.get("api", ""))[:90])) for s in spider_final[:80]),
        "".join("<tr class='drop'><td>%s</td><td class='mono'>%s</td><td>%s</td></tr>" %
                (esc(s.get("name", "")), esc(str(s.get("api", ""))[:70]), esc(why))
                for s, why in spider_bad_shown),
    )
    (outdir / "good_report.html").write_text(doc, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
