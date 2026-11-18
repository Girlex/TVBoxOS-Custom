#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""根据 builtin_site_report.json 重建内置源：
  1) 剔除 dead + slow 站点，all.json 重写（不含橘汁）
  2) 每个接口生成独立配置 configs/single/<key>.json
  3) collection.json = 橘汁 + CMS合集(搜索) + 每接口独立线路 + 全部源(搜索)
  4) 删除旧聚合档 fused/cms/spider/spider_a/spider_b，更新 manifest
"""
import json
import pathlib
import re
import shutil

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS = ROOT / "app/src/main/assets/builtin"
CONFIGS = ASSETS / "configs"
SINGLE = CONFIGS / "single"
JUZI_KEY = "csp_Juzi"

rep = json.load(open(ROOT / "tools/my_api/builtin_site_report.json", encoding="utf-8"))
drop_keys = {x["key"] for x in rep["dead"]} | {x["key"] for x in rep["slow"]}
print("剔除 dead %d + slow %d" % (len(rep["dead"]), len(rep["slow"])))

all_cfg = json.load(open(CONFIGS / "all.json", encoding="utf-8"))
top_spider = all_cfg.get("spider", "")
sites = [s for s in all_cfg["sites"] if s.get("key") != JUZI_KEY]
kept = [s for s in sites if s.get("key") not in drop_keys]
print("站点：%d → 保留 %d" % (len(sites), len(kept)))

cms = [s for s in kept if str(s.get("type")) in ("0", "1")]
spider = [s for s in kept if str(s.get("type")) not in ("0", "1")]
print("  CMS %d + 爬虫 %d" % (len(cms), len(spider)))

# ---------- 1) all.json 重写（不含橘汁） ----------
all_cfg["sites"] = kept
(CONFIGS / "all.json").write_text(json.dumps(all_cfg, ensure_ascii=False, indent=1), encoding="utf-8")

# ---------- 2) 每接口独立配置（文件名用 md5，避免中文 URL 编码问题） ----------
import hashlib

if SINGLE.exists():
    shutil.rmtree(SINGLE)
SINGLE.mkdir(parents=True)
for s in kept:
    cfg = {"spider": top_spider, "sites": [s]}
    fname = hashlib.md5(s["key"].encode("utf-8")).hexdigest()[:12] + ".json"
    (SINGLE / fname).write_text(
        json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")

# ---------- 3) collection.json ----------
juzi = json.load(open(CONFIGS / "juzi.json", encoding="utf-8"))["sites"][0]
urls = [{"name": "① 橘汁短剧", "url": "clan://localhost/builtin/configs/juzi.json"}]
if cms:
    urls.append({"name": "② CMS合集(搜索)", "url": "clan://localhost/builtin/configs/cms.json"})
n0 = len(urls)
for i, s in enumerate(cms + spider, start=n0 + 1):
    fname = hashlib.md5(s["key"].encode("utf-8")).hexdigest()[:12]
    urls.append({"name": s.get("name", s["key"]),
                 "url": "clan://localhost/builtin/configs/single/%s.json" % fname})
urls.append({"name": "☆ 全部源(搜索)", "url": "clan://localhost/builtin/configs/all.json"})
json.dump({"urls": urls}, open(ASSETS / "collection.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)

# cms.json 重写为纯 CMS 合集（供搜索用）
cms_cfg = {"wallpaper": all_cfg.get("wallpaper", ""), "spider": top_spider, "sites": cms}
(CONFIGS / "cms.json").write_text(json.dumps(cms_cfg, ensure_ascii=False, indent=1), encoding="utf-8")

# ---------- 4) 删除旧聚合档 ----------
for old in ("fused.json", "spider.json", "spider_a.json", "spider_b.json"):
    p = CONFIGS / old
    if p.exists():
        p.unlink()
        print("删除旧档", old)

# ---------- 5) manifest ----------
mf_path = ASSETS / "manifest.json"
mf = json.load(open(mf_path, encoding="utf-8"))
mf["sites"] = len(kept) + 1
mf["tiers"] = [
    {"file": "configs/juzi.json", "name": "① 橘汁短剧", "sites": 1, "desc": "橘汁短剧独立线路"},
    {"file": "configs/cms.json", "name": "② CMS合集(搜索)", "sites": len(cms), "desc": "全部实测CMS聚合，跨站搜索用"},
    {"file": "configs/single/", "name": "③~每接口独立线路", "sites": len(kept), "desc": "每个内置接口一条独立线路，卡就切下一个"},
    {"file": "configs/all.json", "name": "☆ 全部源(搜索)", "sites": len(kept), "desc": "全部站点聚合，跨站搜索"},
]
mf["cleaned"] = rep.get("generated", "")
mf["dropped"] = len(rep["dead"]) + len(rep["slow"])
mf_path.write_text(json.dumps(mf, ensure_ascii=False, indent=2), encoding="utf-8")

print("\ncollection.json 线路数：%d" % len(urls))
print("完成")
