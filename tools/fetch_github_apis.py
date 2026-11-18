#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 GitHub 抓「最近更新」的 TVBox 接口地址，喂给 make_my_api.py 汇总。

流程：
  1) 用 Search API 搜最近 N 天有推送的 tvbox 仓库（两种排序：最新 / 最高星），合并去重
  2) 每个仓库取文件树，挑出根目录及浅层目录里的 .json/.txt/.m3u 配置候选
  3) 解析 README，捞出作者自己公布在文中的接口地址（这类通常比 raw 地址更抗封）
  4) 逐条粗筛：能 200 取到、且内容像 TVBox 配置（sites/urls/#EXTM3U/#genre#）才保留
  5) 合并写入 tools/my_api/urls.txt（保留你已有的条目），随后可直接跑 make_my_api.py

产出：tools/my_api/github_sources.json、tools/my_api/github_report.html、urls.txt
用法：
    python3 tools/fetch_github_apis.py                 # 最近 45 天，取 25 个仓库
    python3 tools/fetch_github_apis.py --days 14 --max-repos 40
    python3 tools/fetch_github_apis.py --dry-run       # 只抓不写 urls.txt
"""
import argparse
import html
import json
import pathlib
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "tools" / "my_api"
PROXY = "socks5h://127.0.0.1:10808"
UA = "okhttp/3.15"
PAT_FILE = pathlib.Path.home() / ".workbuddy" / "github_pat"
API = "https://api.github.com"

# 仓库里不像「配置」的文件/目录，直接跳过，避免把 package.json 这类当接口
SKIP_DIRS = {"node_modules", ".github", ".git", "app/src", "ios", "android", "gradle",
             "docs", "doc", "i18n", "locale", "locales", "dist", "build", "vendor",
             ".idea", "test", "tests", "__pycache__", "lib", "libs"}
SKIP_FILES = {"package.json", "package-lock.json", "composer.json", "tsconfig.json",
              "manifest.json", "angular.json", "pnpm-lock.yaml", "yarn.lock",
              "renovate.json", "dependabot.yml", "settings.json", "launch.json",
              "version.json", "info.json", "meta.json", "data.json"}
CFG_EXT = (".json", ".txt", ".m3u", ".m3u8")
# README 里这些链接不是接口
BAD_LINK = re.compile(r"(shields\.io|badgen\.net|badge|\.png|\.jpg|\.jpeg|\.gif|\.svg|"
                      r"\.webp|/issues|/pulls|/releases|/actions|/blob/|/tree/|/wiki|"
                      r"github\.com/[^/]+/[^/]+/?$|t\.me/|jq\.qq\.com|/sponsors)", re.I)


def esc(s):
    return html.escape(str(s), quote=True)


def token():
    try:
        return PAT_FILE.read_text().strip()
    except Exception:
        return ""


TOKEN = token()


def curl(args, timeout=40):
    try:
        r = subprocess.run(["curl", "-sS", "-L", "--max-time", str(timeout)] + args,
                           capture_output=True, timeout=timeout + 10)
        return (r.stdout or b""), (r.stderr or b"").decode("utf-8", "replace")[:200]
    except subprocess.TimeoutExpired:
        return b"", "curl 超时"


def gh(path, params=None):
    """调用 GitHub API（走代理）。返回 (json, 备注)。"""
    url = API + path
    if params:
        # q 里的 : 和 > 必须编码，否则 curl 会直接报 URL malformed
        url += "?" + "&".join("%s=%s" % (k, quote_plus(str(v))) for k, v in params.items())
    headers = ["-H", "Accept: application/vnd.github+json", "-H", "User-Agent: tvbox-tool"]
    if TOKEN:
        headers += ["-H", "Authorization: Bearer " + TOKEN]
    body, err = curl(headers + ["--proxy", PROXY, url])
    if not body:
        return None, err or "空响应"
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return None, ("非 JSON 响应: " + body[:120].decode("utf-8", "replace"))
    if isinstance(data, dict) and data.get("message"):
        return None, str(data["message"])[:120]
    return data, ""


def fetch_raw(url, timeout=15):
    """抓 raw 文件：先代理再直连（国内直连 raw 基本不通，代理兜底）。"""
    for proxy in (PROXY, None):
        args = ["-sS", "-L", "--max-time", str(timeout), "-A", UA]
        args += (["--proxy", proxy] if proxy else ["--noproxy", "*"])
        out, _err = curl(args + [url], timeout=timeout)
        if out:
            return out
    return b""


def search_repos(days, per_page, min_stars):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    queries = [
        {"q": "tvbox pushed:>%s" % since, "sort": "updated", "order": "desc", "per_page": per_page},
        {"q": "tvbox pushed:>%s" % since, "sort": "stars", "order": "desc", "per_page": per_page},
        {"q": "tvbox 接口 pushed:>%s" % since, "sort": "updated", "order": "desc", "per_page": 30},
        {"q": "tvbox live pushed:>%s" % since, "sort": "updated", "order": "desc", "per_page": 30},
    ]
    repos, seen, notes = [], set(), []
    for q in queries:
        data, note = gh("/search/repositories", q)
        if not data:
            notes.append("搜索失败：%s → %s" % (q["q"], note))
            continue
        for it in data.get("items") or []:
            full = it.get("full_name")
            if not full or full in seen:
                continue
            if (it.get("stargazers_count") or 0) < min_stars:
                continue
            if it.get("archived"):
                continue
            seen.add(full)
            repos.append({
                "repo": full, "branch": it.get("default_branch") or "main",
                "stars": it.get("stargazers_count") or 0,
                "pushed": (it.get("pushed_at") or "")[:19],
                "desc": (it.get("description") or "")[:120],
                "size_kb": it.get("size") or 0,
            })
    return repos, notes


def repo_tree(repo, branch):
    data, note = gh("/repos/%s/git/trees/%s" % (repo, branch), {"recursive": "1"})
    if not data or not isinstance(data.get("tree"), list):
        data, note = gh("/repos/%s/contents/" % repo)
        if isinstance(data, list):
            return [{"path": x.get("path", ""), "type": "blob" if x.get("type") == "file" else "tree",
                     "size": x.get("size") or 0} for x in data], ""
        return [], note or "取文件树失败"
    return data["tree"], ""


def shallow(path):
    parts = path.split("/")
    if len(parts) > 3:
        return False
    low = path.lower()
    return not any(d in low for d in SKIP_DIRS)


def pick_config_files(tree, max_files=8):
    """挑出仓库里最像 TVBox 配置的文件（根目录优先）。"""
    picked = []
    for f in tree:
        if f.get("type") != "blob":
            continue
        path = f.get("path") or ""
        name = path.rsplit("/", 1)[-1]
        low = path.lower()
        if not low.endswith(CFG_EXT) or name.lower() in SKIP_FILES:
            continue
        if not shallow(path):
            continue
        size = f.get("size") or 0
        if size == 0 or size > 3 * 1024 * 1024:
            continue
        depth = path.count("/")
        picked.append((depth, -size, path))
    picked.sort()
    return [p[2] for p in picked[:max_files]]


def readme_links(repo, branch, tree):
    """从 README 里捞作者公布的接口地址。"""
    paths = [f["path"] for f in tree if f.get("type") == "blob"
             and re.match(r"(readme|README|Readme)[^/]*\.(md|txt)$", (f.get("path") or "").rsplit("/", 1)[-1])
             and (f.get("path") or "").count("/") == 0]
    if not paths:
        return []
    raw = "https://raw.githubusercontent.com/%s/%s/%s" % (repo, branch, paths[0])
    body = fetch_raw(raw).decode("utf-8", "replace")
    if not body:
        return []
    out = []
    for m in re.finditer(r"https?://[^\s\)\]\"'<>`|，。、；]+", body):
        u = m.group(0).rstrip(".,;:)]}'\"")
        if BAD_LINK.search(u):
            continue
        tail = u.rstrip("/").rsplit("/", 1)[-1].lower()
        looks_cfg = tail.endswith(CFG_EXT) or re.search(r"\.(json|txt|m3u8?)($|\?)", tail)
        ctx = body[max(0, m.start() - 40):m.start()]
        if not looks_cfg:
            if not (u.endswith("/") and re.search(r"(接口|配置|订阅|api|tvbox|线路)", ctx, re.I)):
                continue
        out.append(u)
    return out


def looks_like_config(body):
    txt = body[:20000].decode("utf-8", "replace")
    if not txt.strip():
        return False
    low = txt.lower()
    if "sites" in low or '"urls"' in low or "#genre#" in low or "#extm3u" in low:
        return True
    return False


def probe(url, timeout=12):
    body = fetch_raw(url, timeout=timeout)
    return {"url": url, "ok": bool(body) and looks_like_config(body),
            "size": len(body), "hint": ("配置" if body and looks_like_config(body)
                                        else ("非配置内容" if body else "取不到"))}


def raw_url(repo, branch, path):
    return "https://raw.githubusercontent.com/%s/%s/%s" % (repo, branch, path)


def main():
    ap = argparse.ArgumentParser(description="从 GitHub 抓最近更新的 TVBox 接口地址")
    ap.add_argument("--days", type=int, default=45, help="只看最近 N 天有推送的仓库（默认 45）")
    ap.add_argument("--max-repos", type=int, default=25, help="最多处理多少个仓库（默认 25）")
    ap.add_argument("--min-stars", type=int, default=0, help="仓库最低星数（默认 0）")
    ap.add_argument("--per-page", type=int, default=40, help="每次搜索返回条数（默认 40）")
    ap.add_argument("--max-candidates", type=int, default=60, help="最多探测多少个候选地址")
    ap.add_argument("--dry-run", action="store_true", help="只抓取和探测，不写 urls.txt")
    args = ap.parse_args()

    if not TOKEN:
        print("提示：未读到 %s，将用未认证额度（每小时 60 次），可能不够用。" % PAT_FILE)

    print("=" * 74)
    print("GitHub 最近更新接口抓取 ｜ 最近 %d 天 ｜ 最多 %d 个仓库" % (args.days, args.max_repos))
    print("=" * 74)

    repos, notes = search_repos(args.days, args.per_page, args.min_stars)
    for n in notes:
        print("  ! " + n)
    if not repos:
        raise SystemExit("搜索不到仓库，检查代理 (127.0.0.1:10808) 与 GitHub token。")

    # 排序：近期推送优先，其次星数
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def score(r):
        try:
            days = (now - datetime.strptime(r["pushed"][:10], "%Y-%m-%d")).days
        except Exception:
            days = args.days
        return (max(0, args.days - days) * 2 + min(r["stars"], 800) / 40.0)

    repos.sort(key=score, reverse=True)
    repos = repos[: args.max_repos]
    print("命中 %d 个仓库，处理前 %d 个：" % (len(repos), len(repos)))
    for r in repos[:12]:
        print("  %-42s %s  ★%d" % (r["repo"], r["pushed"][:10], r["stars"]))
    if len(repos) > 12:
        print("  ... 另外 %d 个" % (len(repos) - 12))

    # ---------- 逐仓库取文件树 ----------
    print("\n读取仓库文件树 ...")
    candidates = []   # {url, name, repo, kind}
    with ThreadPoolExecutor(max_workers=4) as ex:
        trees = list(ex.map(lambda r: (r, repo_tree(r["repo"], r["branch"])), repos))
    for r, (tree, note) in trees:
        if not tree:
            print("  - %-40s %s" % (r["repo"], note or "空"))
            continue
        files = pick_config_files(tree)
        for p in files:
            candidates.append({"url": raw_url(r["repo"], r["branch"], p),
                               "name": "%s/%s" % (r["repo"].split("/")[-1], p),
                               "repo": r["repo"], "kind": "仓库文件"})
        print("  + %-40s %d 个配置候选" % (r["repo"], len(files)))

    # README 链接（作者自公布地址，通常更好用）
    print("\n解析 README 中的公开接口 ...")
    tree_map = {r["repo"]: tree for r, (tree, _note) in trees}
    with ThreadPoolExecutor(max_workers=4) as ex:
        links = list(ex.map(lambda r: (r, readme_links(r["repo"], r["branch"],
                                                      tree_map.get(r["repo"], []))), repos))
    readme_count = 0
    for r, urls in links:
        for u in urls:
            candidates.append({"url": u, "name": "%s/README" % r["repo"].split("/")[-1],
                               "repo": r["repo"], "kind": "README"})
            readme_count += 1
    print("  README 捞出 %d 条" % readme_count)

    # 去重（忽略 scheme）
    uniq, seen = [], set()
    for c in candidates:
        k = re.sub(r"^https?://", "", c["url"]).rstrip("/").lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    print("\n候选合计 %d 条（仓库文件 %d / README %d），去重后 %d 条" % (
        len(candidates), len(candidates) - readme_count, readme_count, len(uniq)))
    if len(uniq) > args.max_candidates:
        # README 优先（更可能是作者维护的正经接口），其次仓库文件
        uniq.sort(key=lambda c: (c["kind"] != "README",))
        uniq = uniq[: args.max_candidates]

    # ---------- 粗筛 ----------
    print("粗筛 %d 条候选（只留能取到且内容像 TVBox 配置的）..." % len(uniq))
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda c: (c, probe(c["url"])), uniq))
    good = [c for c, r in results if r["ok"]]
    for c, r in results:
        if not r["ok"]:
            print("  x %-52s %s" % (c["url"][:52], r["hint"]))
    print("通过粗筛 %d 条" % len(good))

    # ---------- 写 urls.txt（保留原有条目）----------
    urls_file = OUT / "urls.txt"
    OUT.mkdir(parents=True, exist_ok=True)
    existing = []
    if urls_file.is_file():
        existing = [ln.strip() for ln in urls_file.read_text(encoding="utf-8").splitlines()
                    if ln.strip() and not ln.strip().startswith("#")]

    if not args.dry_run:
        lines = [
            "# 个人专属接口 · 地址清单",
            "# 自动抓取时间 %s ｜ GitHub 最近 %d 天更新的仓库" % (
                datetime.now().strftime("%Y-%m-%d %H:%M"), args.days),
            "# 「名称|地址」格式；重复地址会被 make_my_api.py 自动合并",
            "",
            "# ---- GitHub 本轮抓取（README 公开接口优先）----",
        ]
        for c in good:
            if c["kind"] == "README":
                lines.append("%s|%s" % (c["name"], c["url"]))
        for c in good:
            if c["kind"] != "README":
                lines.append("%s|%s" % (c["name"], c["url"]))
        if existing:
            lines += ["", "# ---- 你原有的地址 ----", ""] + existing
        urls_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n已写入 %s（%d 条本轮 + %d 条原有）" % (urls_file, len(good), len(existing)))

    (OUT / "github_sources.json").write_text(json.dumps({
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "days": args.days,
        "repos": [{k: v for k, v in r.items()} for r in repos],
        "candidates": len(uniq),
        "good": good,
        "probed": [{"url": c["url"], "ok": r["ok"], "hint": r["hint"], "size": r["size"]}
                   for c, r in results],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    write_report(args, repos, good, results)
    print("报告：%s" % (OUT / "github_report.html"))
    print("\n下一步：python3 tools/make_my_api.py --rounds 2")


def write_report(args, repos, good, results):
    rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%d</td><td class='mono'>%s</td></tr>" % (
            esc(r["repo"]), esc(r["pushed"][:10]), r["stars"], esc(r["desc"]))
        for r in repos)
    cand_rows = "".join(
        "<tr class='%s'><td>%s</td><td class='mono'>%s</td><td>%s</td></tr>" % (
            "" if r["ok"] else "drop", esc(c["name"]), esc(c["url"]), esc(r["hint"]))
        for c, r in results)
    doc = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GitHub 接口抓取报告</title><style>
body{margin:0;padding:32px 20px 60px;background:#f6f7f9;color:#1f2329;
     font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.6}
.wrap{max-width:1080px;margin:0 auto}h1{font-size:22px;margin:0 0 6px}
h2{font-size:16px;margin:26px 0 10px}p{color:#5f6672;font-size:13px}
.card{background:#fff;border:1px solid #e6e8ec;border-radius:12px;padding:18px 20px;margin-bottom:16px}
table{width:100%%;border-collapse:collapse;font-size:13px}
th,td{padding:9px 8px;border-bottom:1px solid #eef0f3;text-align:left;vertical-align:top}
th{background:#fafbfc;color:#6b7280;font-size:12px;font-weight:600}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;word-break:break-all}
tr.drop td{color:#98a2b3}.box{border-left:4px solid #175cd3;background:#eff8ff;
border-radius:10px;padding:14px 18px;font-size:14px;margin-bottom:18px}
</style></head><body><div class="wrap">
<h1>GitHub 最近更新接口 · 抓取报告</h1>
<p>%s ｜ 最近 %d 天 ｜ %d 个仓库 ｜ 粗筛通过 %d 条</p>
<div class="box">本轮只负责「找到地址并确认内容像 TVBox 配置」。
卡不卡、是否重复、站该不该留，交给 <b>make_my_api.py</b> 实测决定——它会多轮测速、展开多仓、
校验爬虫 jar、双重去重后融合成一份个人配置。</div>
<h2>处理的仓库</h2><div class="card" style="padding:8px 10px"><table>
<thead><tr><th>仓库</th><th>最近推送</th><th>★</th><th>描述</th></tr></thead><tbody>%s</tbody></table></div>
<h2>候选与粗筛结果</h2><div class="card" style="padding:8px 10px"><table>
<thead><tr><th>来源</th><th>地址</th><th>结论</th></tr></thead><tbody>%s</tbody></table></div>
</div></body></html>""" % (esc(datetime.now().strftime("%Y-%m-%d %H:%M")), args.days,
                           len(repos), len(good), rows, cand_rows)
    (OUT / "github_report.html").write_text(doc, encoding="utf-8")


if __name__ == "__main__":
    main()
