#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""104 个内置源拆成几条分组线路（橘汁不动）：
  ① 橘汁短剧（不改动）
  ② CMS·极速精选      (11 个实测 CMS)
  ③ 爬虫·精选区       (前 31 个爬虫)
  ④ 爬虫·资源区       (中 31 个爬虫)
  ⑤ 爬虫·备用区       (余下 31 个爬虫)
  ⑥ 全部源(搜索)      (104 站聚合，跨站搜索用)
并删除单接口 single/ 目录。
"""
import json
import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS = ROOT / "app/src/main/assets/builtin"
CONFIGS = ASSETS / "configs"

all_cfg = json.load(open(CONFIGS / "all.json", encoding="utf-8"))
top_spider = all_cfg.get("spider", "")
sites = all_cfg["sites"]
cms = [s for s in sites if str(s.get("type")) in ("0", "1")]
spider = [s for s in sites if str(s.get("type")) not in ("0", "1")]
print("CMS %d + 爬虫 %d = %d" % (len(cms), len(spider), len(sites)))

# ---------- 爬虫分 3 组 ----------
def split3(lst):
    n = len(lst)
    a = (n + 2) // 3
    b = (2 * n + 2) // 3
    return lst[:a], lst[a:b], lst[b:]

g1, g2, g3 = split3(spider)
print("分组：", len(g1), len(g2), len(g3))

def write_cfg(name, group):
    cfg = {"wallpaper": all_cfg.get("wallpaper", ""), "spider": top_spider, "sites": group}
    (CONFIGS / name).write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")

write_cfg("cms.json", cms)
write_cfg("spider_a.json", g1)
write_cfg("spider_b.json", g2)
write_cfg("spider_c.json", g3)

# ---------- collection.json（橘汁不动） ----------
urls = [
    {"name": "① 橘汁短剧",      "url": "clan://localhost/builtin/configs/juzi.json"},
    {"name": "② CMS·极速精选",  "url": "clan://localhost/builtin/configs/cms.json"},
    {"name": "③ 爬虫·精选区",   "url": "clan://localhost/builtin/configs/spider_a.json"},
    {"name": "④ 爬虫·资源区",   "url": "clan://localhost/builtin/configs/spider_b.json"},
    {"name": "⑤ 爬虫·备用区",   "url": "clan://localhost/builtin/configs/spider_c.json"},
    {"name": "⑥ 全部源(搜索)",  "url": "clan://localhost/builtin/configs/all.json"},
]
json.dump({"urls": urls}, open(ASSETS / "collection.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)

# ---------- 删除单接口目录 ----------
if (CONFIGS / "single").exists():
    shutil.rmtree(CONFIGS / "single")
    print("已删除 configs/single/")

# ---------- manifest ----------
mf_path = ASSETS / "manifest.json"
mf = json.load(open(mf_path, encoding="utf-8"))
mf["sites"] = len(sites) + 1
mf["tiers"] = [
    {"file": "configs/juzi.json",    "name": "① 橘汁短剧",     "sites": 1,        "desc": "橘汁短剧独立线路（稳定版，勿动）"},
    {"file": "configs/cms.json",     "name": "② CMS·极速精选", "sites": len(cms), "desc": "全部实测可播 CMS"},
    {"file": "configs/spider_a.json","name": "③ 爬虫·精选区",  "sites": len(g1),  "desc": "实测后端可达爬虫 1/3"},
    {"file": "configs/spider_b.json","name": "④ 爬虫·资源区",  "sites": len(g2),  "desc": "实测后端可达爬虫 2/3"},
    {"file": "configs/spider_c.json","name": "⑤ 爬虫·备用区",  "sites": len(g3),  "desc": "实测后端可达爬虫 3/3"},
    {"file": "configs/all.json",     "name": "⑥ 全部源(搜索)", "sites": len(sites),"desc": "全部站点聚合，跨站搜索"},
]
mf_path.write_text(json.dumps(mf, ensure_ascii=False, indent=2), encoding="utf-8")
print("collection 线路数：%d，未推送编译" % len(urls))
