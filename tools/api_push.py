#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Git Data API push tool (git push 不可用，代理与 GnuTLS 不兼容).

Usage:
  python3 api_push.py plan           # only print diff plan
  python3 api_push.py push "message" # push diff as one commit

Must run from the repo staging dir (default /tmp/tvbox_ci_repo).
Env: GITHUB_PAT_FILE (default ~/.workbuddy/github_pat),
     GITHUB_REPO (default Girlex/TVBoxOS-Custom), BRANCH (default main).
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

REPO = os.environ.get("GITHUB_REPO", "Girlex/TVBoxOS-Custom")
BRANCH = os.environ.get("BRANCH", "main")
PAT = open(os.path.expanduser(os.environ.get("GITHUB_PAT_FILE", "~/.workbuddy/github_pat"))).read().strip()
PROXY = "socks5h://127.0.0.1:10808"
API = "https://api.github.com"

# mirrors the rsync excludes; these local-only dirs never live in the repo
SKIP_DIRS = {".git", ".workbuddy", "apk", "__pycache__", "offline_sources",
             "raw", "my_api", "device_logs", "ci_logs"}
SKIP_FILES = {".blob_cache.json"}


def api(method, path, body=None, tries=7):
    """GitHub API via curl with long exponential backoff (proxy rate limits)."""
    url = API + path
    delay = 20
    body_file = None
    for i in range(tries):
        cmd = ["curl", "-sS", "--proxy", PROXY, "-m", "300",
               "-X", method, "-H", "Authorization: token " + PAT,
               "-H", "Accept: application/vnd.github+json"]
        if body is not None:
            if body_file is None:
                fd, body_file = tempfile.mkstemp(suffix=".json")
                with os.fdopen(fd, "w") as f:
                    json.dump(body, f)
            cmd += ["-H", "Content-Type: application/json",
                    "--data-binary", "@" + body_file]
        cmd.append(url)
        p = subprocess.run(cmd, capture_output=True, text=True)
        out = p.stdout.strip()
        code = 0
        if out:
            try:
                j = json.loads(out)
                # git objects (commit/blob/tree) carry "sha" + "message";
                # error payloads carry "message" but never "sha"
                if isinstance(j, dict) and "message" in j and "sha" not in j:
                    code = 429 if "rate limit" in j["message"].lower() else 1
                    out = j
            except Exception:
                pass
        if p.returncode == 0 and code == 0:
            try:
                return json.loads(out)
            except Exception:
                return out
        print("  api %s %s -> %s %s, retry in %ds" %
              (method, path, p.returncode, str(out)[:120], delay))
        time.sleep(delay)
        delay = min(delay * 2, 300)
    raise RuntimeError("API failed: %s %s" % (method, path))


def blob_sha(path):
    data = open(path, "rb").read()
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def local_files(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn in SKIP_FILES:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            out[rel] = blob_sha(full)
    return out


def main():
    root = os.getcwd()
    mode = sys.argv[1] if len(sys.argv) > 1 else "plan"
    msg = sys.argv[2] if len(sys.argv) > 2 else "update sources"

    ref = api("GET", "/repos/%s/git/ref/heads/%s" % (REPO, BRANCH))
    base_commit = ref["object"]["sha"]
    commit = api("GET", "/repos/%s/git/commits/%s" % (REPO, base_commit))
    base_tree = commit["tree"]["sha"]
    tree = api("GET", "/repos/%s/git/trees/%s?recursive=1" % (REPO, base_tree))
    if tree.get("truncated"):
        print("WARNING: remote tree truncated, deletions unreliable")
    remote = {e["path"]: e["sha"] for e in tree.get("tree", [])
              if e["type"] == "blob"}

    local = local_files(root)
    skip = set(p.strip() for p in os.environ.get("SKIP_PATHS", "").split(",")
               if p.strip())
    changed = sorted(p for p, s in local.items()
                     if remote.get(p) != s and p not in skip)
    deleted = sorted(p for p in remote if p not in local and p not in skip)
    print("base commit:", base_commit)
    print("changed (%d):" % len(changed))
    for p in changed:
        print("  M", p)
    print("deleted (%d):" % len(deleted))
    for p in deleted:
        print("  D", p)
    if not changed and not deleted:
        print("nothing to push")
        return

    if mode != "push":
        print("plan only; run: api_push.py push '<message>'")
        return

    entries = []
    cache_file = os.path.join(root, ".blob_cache.json")
    cache = {}
    if os.path.exists(cache_file):
        try:
            cache = json.load(open(cache_file))
        except Exception:
            cache = {}
    failed = []
    for p in changed:
        sha = local[p]
        if cache.get(p) == sha:
            entries.append({"path": p, "mode": "100644", "type": "blob",
                            "sha": sha})
            print("  blob cached:", p, flush=True)
            continue
        data = open(os.path.join(root, p), "rb").read()
        try:
            b = api("POST", "/repos/%s/git/blobs" % REPO,
                    {"content": base64.b64encode(data).decode(),
                     "encoding": "base64"})
            if not isinstance(b, dict) or "sha" not in b:
                raise RuntimeError("bad resp: %r" % (b,))
            entries.append({"path": p, "mode": "100644", "type": "blob",
                            "sha": b["sha"]})
            cache[p] = b["sha"]
            json.dump(cache, open(cache_file, "w"))
            print("  blob ok:", p, flush=True)
            time.sleep(1.5)  # avoid secondary rate limit on POST bursts
        except Exception as e:
            print("  blob FAILED (skipped):", p, e, flush=True)
            failed.append(p)
    if failed and len(entries) == 0:
        raise RuntimeError("all blob uploads failed, aborting")
    for p in deleted:
        entries.append({"path": p, "mode": "100644", "type": "blob",
                        "sha": None})
    new_tree = api("POST", "/repos/%s/git/trees" % REPO,
                   {"base_tree": base_tree, "tree": entries})
    new_commit = api("POST", "/repos/%s/git/commits" % REPO,
                     {"message": msg, "tree": new_tree["sha"],
                      "parents": [base_commit]})
    api("PATCH", "/repos/%s/git/refs/heads/%s" % (REPO, BRANCH),
        {"sha": new_commit["sha"], "force": False})
    print("pushed:", new_commit["sha"], "->", "%s@%s" % (REPO, BRANCH))


if __name__ == "__main__":
    main()
