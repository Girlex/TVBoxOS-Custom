#!/usr/bin/env python3
"""GitHub Actions 云编译助手：触发 workflow、查询状态、下载 APK。

令牌来源（按优先级）：--token-file 指定文件、~/.workbuddy/github_pat、环境变量 GITHUB_TOKEN。
令牌不会被打印，也不会写入仓库。

示例：
  python3 tools/github_ci_helper.py dispatch --repo OWNER/REPO
  python3 tools/github_ci_helper.py status   --repo OWNER/REPO --wait
  python3 tools/github_ci_helper.py download --repo OWNER/REPO --out ./apk
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import zipfile

API = "https://api.github.com"
WORKFLOW = "build-custom.yml"
DEFAULT_PROXY = "socks5h://127.0.0.1:10808"
DRY_RUN = False


def load_token(explicit):
    for candidate in (
        pathlib.Path(explicit).expanduser() if explicit else None,
        pathlib.Path.home() / ".workbuddy/github_pat",
    ):
        if candidate and candidate.is_file():
            value = candidate.read_text(encoding="utf-8").strip()
            if value:
                return value
    return os.environ.get("GITHUB_TOKEN", "").strip()


def curl(args, url, token, method="GET", body=None, headers_only=False, out=None):
    cmd = ["curl", "-sS", "--max-time", "60", "-X", method,
           "-H", "Accept: application/vnd.github+json",
           "-H", "X-GitHub-Api-Version: 2022-11-28",
           "-H", "Authorization: Bearer " + token]
    if args.proxy:
        cmd += ["--proxy", args.proxy]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    if headers_only:
        cmd += ["-D", "-", "-o", os.devnull]
    elif out:
        cmd += ["-o", out]
    cmd += ["-w", "\n__CODE__%{http_code}", url]
    shown = " ".join(cmd).replace(token, "***TOKEN***")
    if DRY_RUN:
        print("[dry-run]", shown)
        return 200, "" if out else "{}"
    r = subprocess.run(cmd, capture_output=True, text=True)
    text = r.stdout
    m = re.search(r"__CODE__(\d+)$", text)
    code = int(m.group(1)) if m else 0
    payload = text[:m.start()] if m else text
    if r.returncode and not m:
        print("curl 失败:", r.stderr.strip()[:300], file=sys.stderr)
    return code, payload.strip()


def api(args, token, method, path, body=None):
    return curl(args, API + path, token, method=method, body=body)


def latest_run(args, token, repo):
    code, text = api(args, token, "GET",
                     "/repos/%s/actions/workflows/%s/runs?per_page=10" % (repo, WORKFLOW))
    if code != 200:
        print("查询 workflow runs 失败: HTTP %d %s" % (code, text[:300]), file=sys.stderr)
        return None
    runs = json.loads(text).get("workflow_runs", [])
    return runs[0] if runs else None


def cmd_dispatch(args, token):
    body = {"ref": args.ref, "inputs": {"variant": args.variant,
                                        "build_type": args.build_type}}
    code, text = api(args, token, "POST",
                     "/repos/%s/actions/workflows/%s/dispatches" % (args.repo, WORKFLOW),
                     body=body)
    print("dispatch HTTP %d %s" % (code, text[:200]))
    if code not in (200, 204):
        sys.exit("触发失败（检查令牌是否有 Actions/Workflows 写权限、仓库是否已启用 Actions）")


def cmd_status(args, token):
    deadline = time.time() + args.timeout
    while True:
        run = latest_run(args, token, args.repo)
        if run:
            print("run #%s | %s | %s | %s" % (run["id"], run["status"],
                                              run["conclusion"], run["html_url"]))
            if run["status"] == "completed" or not args.wait:
                return run
        elif not args.wait:
            print("暂无运行记录")
            return None
        if time.time() >= deadline:
            print("等待超时，可稍后重新运行 status --wait", file=sys.stderr)
            return run
        time.sleep(args.interval)


def cmd_download(args, token):
    run_id = args.run_id
    if not run_id:
        run = latest_run(args, token, args.repo)
        if not run:
            sys.exit("没有找到 workflow run")
        run_id = run["id"]
        print("使用最新 run #%s（%s）" % (run_id, run["conclusion"]))
    code, text = api(args, token, "GET",
                     "/repos/%s/actions/runs/%s/artifacts" % (args.repo, run_id))
    if code != 200:
        sys.exit("查询 artifact 失败: HTTP %d %s" % (code, text[:300]))
    artifacts = json.loads(text).get("artifacts", [])
    if not artifacts:
        sys.exit("该 run 还没有 artifact（可能仍在构建或构建失败）")
    art = next((a for a in artifacts if "TVBox" in a["name"]), artifacts[0])
    print("artifact: %s (%.1f MB)" % (art["name"], art["size_in_bytes"] / 1048576))

    zip_path = str(pathlib.Path(args.out) / (art["name"] + ".zip"))
    code, headers = curl(args, API + "/repos/%s/actions/artifacts/%s/zip" % (args.repo, art["id"]),
                         token, headers_only=True)
    loc = re.search(r"^location:\s*(\S+)", headers, re.I | re.M)
    if not loc:
        sys.exit("未拿到 artifact 下载地址（HTTP %d）" % code)
    if DRY_RUN:
        print("[dry-run] 下载到", zip_path)
        return
    r = subprocess.run(["curl", "-sS", "-L", "--max-time", "300", "-o", zip_path]
                       + (["--proxy", args.proxy] if args.proxy else []) + [loc.group(1)],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit("下载失败: " + r.stderr.strip()[:300])

    out = pathlib.Path(args.out)
    apks = []
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if name.lower().endswith(".apk") and "unsigned" not in name.lower():
                target = out / pathlib.Path(name).name
                target.write_bytes(z.read(name))
                apks.append(target)
    print("APK 已保存到:")
    for p in apks:
        print("  %s  (%.1f MB)" % (p, p.stat().st_size / 1048576))
    if not apks:
        sys.exit("artifact 中没有找到已签名的 APK")


def main():
    global DRY_RUN
    p = argparse.ArgumentParser(description="TVBoxOS 云编译助手")
    p.add_argument("--repo", required=True, help="OWNER/REPO")
    p.add_argument("--token-file", help="存放 PAT 的文件路径")
    p.add_argument("--proxy", default=DEFAULT_PROXY, help="curl 代理，传空字符串可禁用")
    p.add_argument("--dry-run", action="store_true", help="只打印命令，不实际请求")
    sub = p.add_subparsers(dest="action", required=True)

    d = sub.add_parser("dispatch", help="手动触发云编译")
    d.add_argument("--ref", default="main")
    d.add_argument("--variant", default="java", choices=["java", "java32", "java64"])
    d.add_argument("--build-type", default="debug", choices=["debug", "release"])

    s = sub.add_parser("status", help="查看运行状态")
    s.add_argument("--wait", action="store_true", help="等待构建结束")
    s.add_argument("--timeout", type=int, default=1800, help="最长等待秒数")
    s.add_argument("--interval", type=int, default=20, help="轮询间隔秒数")

    w = sub.add_parser("download", help="下载最新构建产出的 APK")
    w.add_argument("--run-id", help="指定 run id，缺省取最新")
    w.add_argument("--out", default="./apk", help="输出目录")

    args = p.parse_args()
    DRY_RUN = args.dry_run
    token = load_token(args.token_file)
    if not token and not DRY_RUN:
        sys.exit("未找到令牌：请把 PAT 写入 ~/.workbuddy/github_pat（600 权限），或设置 GITHUB_TOKEN")
    if args.action == "download":
        pathlib.Path(args.out).mkdir(parents=True, exist_ok=True)
    {"dispatch": cmd_dispatch, "status": cmd_status, "download": cmd_download}[args.action](args, token or "DRYRUN")


if __name__ == "__main__":
    main()
