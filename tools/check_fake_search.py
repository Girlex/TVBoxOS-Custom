#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检测融合源里的 CMS 站点是否为「假搜索」。

假搜索站：搜索接口忽略 wd 关键词，无论搜什么词都返回同一批首页推荐流。
判别：用两个差异极大的词各搜一次——
  词A = 真实剧名「灵魂摆渡」，词B = 绝不可能存在的乱码词。
  · 词B 仍返回 >= 4 条，且与词A 的标题高度重合（>=3 条相同）→ 判定假搜索
  · 词A 结果里没有任何标题含「灵魂」或「摆渡」，且词B 也返回一堆 → 高度可疑
真搜索站：词B 应返回空或极少，词A 返回含关键词的结果，两者几乎不重合。

产物：tools/my_api/fake_search_report.json（供后续脚本读取剔除）
用法：python3 tools/check_fake_search.py
"""
import json
import pathlib
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urlsplit, urlunsplit, parse_qsl, urlencode

ROOT = pathlib.Path(__file__).resolve().parents[1]
FUSED = ROOT / "app/src/main/assets/builtin/configs/fused.json"
OUT = ROOT / "tools/my_api/fake_search_report.json"
PROXY = "socks5h://127.0.0.1:10808"
UA = "okhttp/3.15"
WORD_A = "灵魂摆渡"
WORD_B = "zzzzqqqq不存在987654321"

# 判据阈值
OVERLAP_MIN = 3       # 两次结果前若干条里相同标题数 >= 此值 → 假搜索
B_RESULT_MIN = 4      # 乱码词返回条数 >= 此值才纳入判定


def curl(url, timeout=12, proxy=None):
    cmd = ["curl", "-sS", "-L", "--compressed", "--max-time", str(timeout),
           "-A", UA, "-H", "Accept: */*"]
    cmd += (["--proxy", proxy] if proxy else ["--noproxy", "*"])
    cmd += ["-w", "\n__M__%{http_code}", url]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 4)
    except subprocess.TimeoutExpired:
        return 0, b""
    out = r.stdout or b""
    if b"__M__" not in out:
        return 0, b""
    body, meta = out.rsplit(b"__M__", 1)
    body = body[:-1] if body.endswith(b"\n") else body
    try:
        code = int(meta.decode("utf-8", "replace").strip() or 0)
    except Exception:
        code = 0
    return code, body


def search_url(api, word):
    sp = urlsplit(api)
    q = dict(parse_qsl(sp.query, keep_blank_values=True))
    q.pop("ac", None)
    q["ac"] = "videolist"
    q["wd"] = word
    return urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(q), ""))


def parse_titles(body):
    try:
        txt = body.decode("utf-8", "replace").lstrip("\ufeff").strip()
        d = json.loads(txt)
    except Exception:
        try:
            txt = body.decode("utf-8", "replace")
            i, j = txt.find("{"), txt.rfind("}")
            d = json.loads(txt[i:j + 1]) if 0 <= i < j else None
        except Exception:
            d = None
    if not isinstance(d, dict):
        return [], 0
    lst = d.get("list")
    if not isinstance(lst, list):
        return [], 0
    titles = [str(v.get("vod_name", "")) for v in lst if isinstance(v, dict)]
    try:
        total = int(d.get("total", len(lst)))
    except Exception:
        total = len(lst)
    return titles, total


def fetch_titles(api, word, timeout=12):
    u = search_url(api, word)
    for proxy in (None, PROXY):
        code, body = curl(u, timeout, proxy)
        if code in (200, 206) and body:
            titles, total = parse_titles(body)
            if titles:
                return titles, total, ("直连" if proxy is None else "代理")
    return [], 0, ""


def check_site(idx_site):
    idx, site = idx_site
    name = str(site.get("name", ""))
    api = str(site.get("api", ""))
    res = {"idx": idx, "name": name, "api": api, "fake": False,
           "reason": "", "a_n": 0, "b_n": 0, "overlap": 0, "via": ""}
    if not api.startswith("http"):
        res["reason"] = "api 非 URL"
        return res
    ta, total_a, via = fetch_titles(api, WORD_A)
    tb, total_b, _ = fetch_titles(api, WORD_B)
    res["a_n"] = len(ta)
    res["b_n"] = len(tb)
    res["via"] = via
    if not ta:
        res["reason"] = "词A 无结果（源本身可能已失效）"
        return res
    set_a = set(t.strip() for t in ta[:20])
    set_b = set(t.strip() for t in tb[:20])
    overlap = len(set_a & set_b)
    res["overlap"] = overlap
    # 词B（乱码）也返回一堆 -> 基本可判假搜索
    if len(tb) >= B_RESULT_MIN and overlap >= OVERLAP_MIN:
        res["fake"] = True
        res["reason"] = "乱码词也返回%d条且重合%d条" % (len(tb), overlap)
        return res
    # 词A 完全不含关键词，且词B 返回与词A 数量相当 -> 高度可疑
    hit_kw = any(("灵魂" in t) or ("摆渡" in t) for t in ta)
    if not hit_kw and len(tb) >= B_RESULT_MIN and len(tb) >= len(ta) * 0.6:
        res["fake"] = True
        res["reason"] = "结果不含关键词且乱码词同样返回%d条" % len(tb)
        return res
    res["reason"] = "OK" if hit_kw else "结果不含关键词但乱码词未返回同批"
    return res


def main():
    data = json.load(open(FUSED, encoding="utf-8"))
    sites = data.get("sites", [])
    cms = [(i, s) for i, s in enumerate(sites) if str(s.get("type")) == "1"]
    print("检测 %d 个 CMS 站点（两词对比法）..." % len(cms))
    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(check_site, item): item for item in cms}
        for fut in as_completed(futs):
            results.append(fut.result())
    results.sort(key=lambda r: r["idx"])
    fakes = [r for r in results if r["fake"]]
    print("\n=== 判定为假搜索的站点（%d 个）===" % len(fakes))
    for r in fakes:
        print("  [%d] %-18s A=%d B=%d 重合=%d | %s" %
              (r["idx"], r["name"][:18], r["a_n"], r["b_n"], r["overlap"], r["reason"]))
    print("\n=== 正常站点（%d 个）===" % (len(results) - len(fakes)))
    for r in results:
        if not r["fake"]:
            print("  [%d] %-18s A=%d B=%d 重合=%d | %s" %
                  (r["idx"], r["name"][:18], r["a_n"], r["b_n"], r["overlap"], r["reason"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    json.dump({"word_a": WORD_A, "word_b": WORD_B,
               "fake_idx": [r["idx"] for r in fakes],
               "fake_names": [r["name"] for r in fakes],
               "results": results},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\n报告写入:", OUT)


if __name__ == "__main__":
    main()
