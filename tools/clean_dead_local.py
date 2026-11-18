#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理内置本地源里的失效站点：
  1) CMS(type=1)：真实搜索词 + 假搜索探测实测，直连不可用/无结果/假搜索 → 剔除
     （电视端多为直连，仅代理可用视为失效）
  2) 爬虫(type=3)及其它：静态校验引用的本地 jar / scripts 文件存在，缺失 → 剔除
  3) 重写 4 档配置 + juzi.json 之外的档位，清理无引用的 jar/脚本，更新 manifest
产物：tools/my_api/clean_dead_report.json
"""
import json
import pathlib
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from build_good_api import test_cms  # noqa: E402

ASSETS = ROOT / "app/src/main/assets/builtin"
COPY = ROOT / "tools/my_api/local_builtin/builtin"
JUZI_KEY = "csp_Juzi"

all_cfg = json.load(open(ASSETS / "configs/all.json"))
sites = all_cfg["sites"]
print("输入站点：%d" % len(sites))

cms = [s for s in sites if str(s.get("type")) == "1"]
others = [s for s in sites if str(s.get("type")) != "1"]
print("  CMS(type=1)：%d，其余：%d" % (len(cms), len(others)))

# ---------- 1) CMS 实测 ----------
results = {}
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = {ex.submit(test_cms, s): s for s in cms}
    for f in as_completed(futs):
        s = futs[f]
        try:
            results[s.get("key")] = f.result()
        except Exception as e:
            results[s.get("key")] = {"ok": False, "why": "测试异常:%s" % e}

kept_cms, dropped = [], []
for s in cms:
    r = results.get(s.get("key"), {})
    if r.get("ok") and r.get("direct_ok"):
        kept_cms.append(s)
    else:
        dropped.append({"name": s.get("name"), "key": s.get("key"),
                        "why": r.get("why", "无结果"), "direct_ok": r.get("direct_ok")})

print("\nCMS 实测：存活 %d / 剔除 %d" % (len(kept_cms), len(dropped)))
for d in dropped:
    print("  ✗ %-18s %s" % (d["name"], d["why"]))

# ---------- 2) 其余站点静态校验（本地 jar/scripts 文件存在） ----------
file_re = re.compile(r"clan://localhost/builtin/(jars|scripts)/([A-Za-z0-9_.\-]+)")
kept_others = []
for s in others:
    blob = json.dumps(s, ensure_ascii=False)
    refs = file_re.findall(blob)
    missing = [p for p in refs if not (ASSETS / p[0] / p[1]).is_file()]
    if missing:
        dropped.append({"name": s.get("name"), "key": s.get("key"),
                        "why": "本地资源缺失:%s" % missing[0][1]})
        print("  ✗ %-18s 本地资源缺失 %s" % (s.get("name"), missing[0][1]))
    else:
        kept_others.append(s)

local_sites = kept_cms + kept_others
print("\n合计保留：%d（CMS %d + 其它 %d），剔除 %d"
      % (len(local_sites), len(kept_cms), len(kept_others), len(dropped)))

# ---------- 3) 重写档位 ----------
spider_sites = [s for s in local_sites if str(s.get("type")) == "3"]
lite_sites = kept_cms + spider_sites[:20]
tiers = [
    ("configs/fused.json", lite_sites),
    ("configs/cms.json", kept_cms),
    ("configs/spider.json", spider_sites),
    ("configs/all.json", local_sites),
]

# 记录本轮实际引用的 jar/脚本
used = set()
def collect(v):
    if isinstance(v, str):
        for m in file_re.finditer(v):
            used.add(m.group(1) + "/" + m.group(2))
    elif isinstance(v, dict):
        for vv in v.values():
            collect(vv)
    elif isinstance(v, list):
        for vv in v:
            collect(vv)
for s in local_sites:
    collect(s)
# 顶层 spider（jars/j1.jar 兜底）也从 all.json 顶层拿
top_spider = all_cfg.get("spider", "")
if top_spider:
    collect(top_spider)

for base in (ASSETS, COPY):
    if not base.is_dir():
        continue
    jz = json.load(open(base / "configs/juzi.json"))["sites"][0]
    for rel, tset in tiers:
        cfg = json.load(open(base / rel))
        body = [s for s in tset if s.get("key") != JUZI_KEY]
        cfg["sites"] = body + [jz]          # 每档固定带橘汁
        (base / rel).write_text(json.dumps(cfg, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    # 清理无引用 jar / 脚本
    for sub in ("jars", "scripts"):
        d = base / sub
        if not d.is_dir():
            continue
        for p in list(d.iterdir()):
            if p.is_file() and (sub + "/" + p.name) not in used:
                p.unlink()
                print("  清理 %s/%s" % (sub, p.name))
    # manifest
    mf = base / "manifest.json"
    if mf.is_file():
        m = json.load(open(mf))
        m["sites"] = len(local_sites)
        for t in m.get("tiers", []):
            for rel, tset in tiers:
                if t.get("file", "").endswith(rel.split("/")[-1]):
                    t["sites"] = len(tset)
        m["cleaned"] = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        m["dropped"] = len(dropped)
        (base / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=2),
                                            encoding="utf-8")

# 报告
rep = {
    "generated": datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S"),
    "input": len(sites), "kept": len(local_sites), "dropped": len(dropped),
    "cms_kept": len(kept_cms), "cms_total": len(cms),
    "dropped_list": dropped,
}
(ROOT / "tools/my_api/clean_dead_report.json").write_text(
    json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
print("\n报告 → tools/my_api/clean_dead_report.json")
