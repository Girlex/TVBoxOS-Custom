#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验多档本地源（build_local_good.py 产物）的完整性与安全性。

检查项：
  1. collection.json 列出的每档 url 都有对应配置文件
  2. manifest.tiers 与 collection.urls 一一对应
  3. 每档配置的 spider 与所有站点 jar 均为 clan://localhost/ 本地地址
  4. 每档配置的站点 api/ext 中不含在线脚本 URL（全本地）
  5. 假搜索站（建安/最大资源/最大采集）不出现在任何档位
  6. 直播默认地址存在、manifest.online 为空
  7. adb 覆盖副本与 assets 逐字节一致
  8. 档位站点数：精选<=全部、纯CMS<=全部、爬虫<=全部
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS = ROOT / "app/src/main/assets/builtin"
COPY = ROOT / "tools/my_api/local_builtin/builtin"
PREFIX = "clan://localhost/"
# url 形如 clan://localhost/builtin/configs/x.json；ASSETS 已指向 .../assets/builtin，
# 故去掉 PREFIX 后还要再去掉开头的 builtin/ 才能拼到实际文件。
def url_to_rel(url):
    r = url[len(PREFIX):] if url.startswith(PREFIX) else url
    return r[len("builtin/"):] if r.startswith("builtin/") else r
FAKE_MARK = ["154.219.117.232", "zuidapi.com", "zuida.xyz", "jacloudapi"]

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("OK" if ok else "XX", name, ("  → " + detail) if detail else ""))


def load(p):
    return json.loads(p.read_text(encoding="utf-8"))


def sites_local(cfg):
    """返回 (是否全本地, 违规样例)。

    关键：只检查"会被当成脚本/jar 加载"的在线引用——
      · spider / jar 字段必须是 clan:// 本地
      · api 字段若以 .js/.py/.json 结尾（脚本站），必须是 clan:// 本地
    type=1 的 api 是 CMS 接口 URL、ext 里的域名是爬虫请求地址，均属正常配置，
    不算在线脚本残留（真正的加载事故是 api 指向在线 .js 被当类名加载）。
    """
    bad = []
    spider = str(cfg.get("spider", ""))
    if spider and not spider.startswith(PREFIX):
        bad.append("spider=" + spider[:60])
    SCRIPT_EXT = (".js", ".py", ".json")
    for s in cfg.get("sites", []):
        v = str(s.get("jar", ""))
        if v and not v.startswith(PREFIX) and not v.startswith("csp_"):
            bad.append("%s.jar=%s" % (s.get("name"), v[:60]))
        api = str(s.get("api", "")).split(";")[0]
        if api.startswith("http") and not api.startswith(PREFIX):
            # api 是脚本 URL（带 .js/.py/.json 后缀）却仍在线 → 残留
            path = api.split("?")[0].lower()
            if path.endswith(SCRIPT_EXT):
                bad.append("%s.api在线脚本=%s" % (s.get("name"), api[:60]))
    return (not bad), bad[:5]


def main():
    print("=" * 72)
    print("多档本地源静态校验")
    print("=" * 72)
    if not ASSETS.exists():
        print("assets 不存在:", ASSETS)
        return 1
    collection = load(ASSETS / "collection.json")
    manifest = load(ASSETS / "manifest.json")
    urls = collection.get("urls", [])
    check("collection 至少 2 档", len(urls) >= 2, "%d 档" % len(urls))

    # 1. 每档配置文件存在
    cfg_paths = []
    all_exist = True
    for u in urls:
        rel = url_to_rel(u["url"])
        p = ASSETS / rel
        all_exist &= p.is_file()
        cfg_paths.append((u["name"], rel, p))
    check("每档配置文件都存在", all_exist,
          ", ".join("%s=%s" % (n, "有" if p.is_file() else "缺") for n, r, p in cfg_paths))

    # 2. manifest.tiers 与 collection 对应
    tiers = manifest.get("tiers", [])
    check("manifest.tiers 档位数一致", len(tiers) == len(urls),
          "tiers=%d urls=%d" % (len(tiers), len(urls)))

    # 3~5. 逐档检查
    cfgs = {}
    fake_found = []
    tier_counts = {}
    for name, rel, p in cfg_paths:
        if not p.is_file():
            continue
        cfg = load(p)
        cfgs[rel] = cfg
        nsite = len(cfg.get("sites", []))
        tier_counts[rel] = nsite
        ok, bad = sites_local(cfg)
        check("[%s] 全本地无在线脚本" % name, ok, "; ".join(bad) if bad else "%d 站" % nsite)
        for s in cfg.get("sites", []):
            api = str(s.get("api", ""))
            if any(m in api for m in FAKE_MARK):
                fake_found.append("%s:%s" % (name, s.get("name")))
    check("无假搜索站(建安/最大)", not fake_found, "; ".join(fake_found) if fake_found else "干净")

    # 6. 直播/在线
    check("manifest.online 为空", not manifest.get("online"),
          "%d 条" % len(manifest.get("online", [])))
    dl = str(manifest.get("default_live", ""))
    check("默认直播本地存在", dl.startswith(PREFIX) and (ASSETS / url_to_rel(dl)).is_file(), dl)

    # 7. 副本一致
    same = True
    for rel in ["collection.json", "manifest.json"] + [r for n, r, p in cfg_paths]:
        a, b = ASSETS / rel, COPY / rel
        if not (a.is_file() and b.is_file() and a.read_bytes() == b.read_bytes()):
            same = False
            print("    不一致:", rel)
    check("adb 副本与 assets 逐字节一致", same)

    # 8. 档位站点数关系
    all_n = tier_counts.get("configs/all.json", 0)
    lite_n = tier_counts.get("configs/fused.json", 0)
    cms_n = tier_counts.get("configs/cms.json", 0)
    spider_n = tier_counts.get("configs/spider.json", 0)
    check("精选<=全部", lite_n <= all_n and lite_n > 0, "%d<=%d" % (lite_n, all_n))
    check("纯CMS<=全部", cms_n <= all_n and cms_n > 0, "%d<=%d" % (cms_n, all_n))
    check("爬虫<=全部", spider_n <= all_n and spider_n > 0, "%d<=%d" % (spider_n, all_n))

    print("\n" + "=" * 72)
    print("结果: %d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项:", ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
