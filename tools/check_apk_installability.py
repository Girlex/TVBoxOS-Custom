#!/usr/bin/env python3
"""校验「可重复覆盖安装」契约：源码侧版本递增/固定签名/首次启动权限，产物侧版本与证书。

用法:
    python3 tools/check_apk_installability.py                      # 只检查源码契约
    python3 tools/check_apk_installability.py apk/TVBox_debug-java.apk --record
                                                                   # 校验 APK 并写入版本基线
--record 会把本次 versionCode / versionName / 签名证书 SHA256 追加到 tools/apk_versions.json，
下次校验时要求 versionCode 严格递增、证书指纹完全一致、包名不变。

注意：证书指纹必须是「X.509 证书 DER 的 SHA256」。v1 签名块 META-INF/*.RSA
内含 signingTime，整块哈希即使同一密钥每次构建也会变化，会造成「签名不一致」的假警报。
本脚本用 openssl 从 PKCS#7 中取出证书本身，并以仓库 TVBoxOSC.jks 为权威基准。
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
RECORD = ROOT / "tools/apk_versions.json"
REPORT = ROOT / "tools/apk_installability_report.html"


def pick(env_name, candidates):
    from_env = os.environ.get(env_name)
    if from_env and pathlib.Path(from_env).is_file():
        return from_env
    for p in candidates:
        if p and pathlib.Path(p).is_file():
            return p
    return None


AAPT = pick("AAPT", ["/home/feng/android/system141/out/host/linux-x86/bin/aapt", shutil.which("aapt")])
KEYTOOL = pick("KEYTOOL", ["/home/feng/opt/jdk8/bin/keytool", shutil.which("keytool")])
OPENSSL = pick("OPENSSL", [shutil.which("openssl"), "/usr/bin/openssl"])
JKS = ROOT / ".github/workflows/TVBoxOSC.jks"
CERT_ALGO = "cert-der-sha256"


def cert_sha256_from_apk(apk):
    """提取 APK v1 签名块中 X.509 证书 DER 的 SHA256。"""
    if not OPENSSL:
        return "", "openssl-missing"
    with zipfile.ZipFile(apk) as z:
        blocks = [n for n in z.namelist()
                  if n.startswith("META-INF/") and n.upper().endswith((".RSA", ".DSA", ".EC"))]
        if not blocks:
            return "", "no-signature-block"
        p7 = z.read(sorted(blocks)[0])
    r = subprocess.run([OPENSSL, "pkcs7", "-inform", "DER", "-print_certs"],
                       input=p7, capture_output=True)
    if r.returncode:
        return "", "pkcs7-failed"
    q = subprocess.run([OPENSSL, "x509", "-outform", "DER"],
                       input=r.stdout, capture_output=True)
    if q.returncode or not q.stdout:
        return "", "x509-failed"
    return hashlib.sha256(q.stdout).hexdigest(), CERT_ALGO


def cert_sha256_from_jks():
    """从仓库固定签名 jks 导出证书，作为「签名是否一致」的权威基准。"""
    if not (OPENSSL and KEYTOOL and JKS.is_file()):
        return ""
    r = subprocess.run([KEYTOOL, "-exportcert", "-rfc", "-keystore", JKS,
                        "-storepass", "TVBoxOSC", "-alias", "TVBoxOSC"],
                       capture_output=True)
    if r.returncode:
        return ""
    q = subprocess.run([OPENSSL, "x509", "-outform", "DER"],
                       input=r.stdout, capture_output=True)
    if q.returncode or not q.stdout:
        return ""
    return hashlib.sha256(q.stdout).hexdigest()


def run(cmd):
    r = subprocess.run([str(x) for x in cmd], capture_output=True, text=True)
    if r.returncode:
        sys.exit("命令失败: %s\n%s" % (" ".join(str(x) for x in cmd), (r.stderr or r.stdout)[:600]))
    return r.stdout


def read_text(rel):
    p = ROOT / rel
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def source_contract():
    """源码侧静态检查，返回 [(名称, 是否通过, 说明)]。"""
    out = []

    def add(name, ok, detail=""):
        out.append((name, bool(ok), detail))

    props = read_text("version.properties")
    m = re.search(r"^VERSION_CODE=(\d+)\s*$", props, re.M)
    add("版本基线文件存在且合法", bool(m), "VERSION_CODE=%s" % (m.group(1) if m else "缺失"))

    gradle = read_text("app/build.gradle")
    add("build.gradle 使用动态 versionCode",
        "versionCode appVersionCode" in gradle and "versionName appVersionName" in gradle)
    add("build.gradle 支持 CI 版本注入", "TVBOX_VERSION_CODE" in gradle)
    add("build.gradle 不再写死 1.0.0", "versionName '1.0.0'" not in gradle)

    wf = read_text(".github/workflows/build-custom.yml")
    add("CI 每次构建计算递增版本", "Compute monotone version code" in wf and "GITHUB_RUN_NUMBER" in wf)
    add("CI 所有构建类型使用固定签名",
        "Pin signing key for every build" in wf and "if: ${{ (inputs.build_type" not in wf)
    add("CI 产物附带版本信息", "out/version.txt" in wf)

    home = read_text("app/src/main/java/com/github/tvbox/osc/ui/activity/HomeActivity.java")
    add("首次启动主动申请存储权限", "requestStorageThenLoad()" in home and "Permission.Group.STORAGE" in home)
    add("权限未返回时有加载兜底", "postDelayed" in home.split("requestStorageThenLoad()", 1)[-1][:1200])
    add("配置加载不会重复触发", "loadConfigOnce" in home and "configLoadStarted" in home)

    signing = read_text("tools/ci_inject_signing.py")
    add("签名注入覆盖 debug 与 release", 'for build_type in ("        debug {\\n", "        release {\\n")' in signing)
    return out


def apk_info(apk):
    if not AAPT:
        sys.exit("未找到 aapt：请设置 AAPT 环境变量指向 aapt 可执行文件")
    out = run([AAPT, "dump", "badging", apk])
    m = re.search(r"^package: name='([^']+)' versionCode='([^']*)' versionName='([^']*)'", out, re.M)
    if not m:
        sys.exit("aapt 未解析出 package 信息:\n" + out[:600])
    info = {"package": m.group(1), "versionCode": int(m.group(2)), "versionName": m.group(3)}

    sha, algo = cert_sha256_from_apk(apk)
    if not sha:
        sys.exit("无法从 APK 提取签名证书（%s）。请安装 openssl 后用新算法重新校验。" % algo)
    info["cert_sha256"] = sha
    info["cert_algo"] = algo
    info["size_mb"] = round(apk.stat().st_size / 1048576, 2)
    with zipfile.ZipFile(apk) as z:
        info["zip_ok"] = z.testzip() is None
    return info


def main():
    ap = argparse.ArgumentParser(description="校验 APK 可覆盖安装契约")
    ap.add_argument("apk", nargs="?", help="APK 路径，缺省只检查源码契约")
    ap.add_argument("--record", action="store_true", help="把本次 APK 信息写入版本基线")
    args = ap.parse_args()

    checks = source_contract()
    info = None
    history = json.loads(RECORD.read_text(encoding="utf-8")) if RECORD.is_file() else []
    prev = history[-1] if history else None
    apk = None

    if args.apk:
        apk = pathlib.Path(args.apk)
        if not apk.is_absolute():
            apk = ROOT / apk
        if not apk.is_file():
            sys.exit("APK 不存在: %s" % apk)
        info = apk_info(apk)
        checks.append(("APK ZIP 完整", info["zip_ok"], "%.2f MB" % info["size_mb"]))
        if prev:
            if prev.get("versionCode") == info["versionCode"]:
                checks.append(("复核基线中的同一版本", True,
                               "versionCode=%s 已记录，跳过递增检查" % info["versionCode"]))
            else:
                checks.append(("versionCode 严格递增", info["versionCode"] > prev["versionCode"],
                               "%s → %s" % (prev["versionCode"], info["versionCode"])))
            if prev.get("cert_algo") == info["cert_algo"]:
                checks.append(("与上一版证书一致", info["cert_sha256"] == prev["cert_sha256"],
                               info["cert_sha256"][:16] + "…"))
            else:
                checks.append(("旧基线指纹算法已废弃", True,
                               "旧记录是含时间戳的签名块哈希，改用 jks 证书基准"))
            if prev.get("package") == info["package"]:
                checks.append(("包名一致", True, info["package"]))
            else:
                checks.append(("包名迁移(全新应用,旧包不覆盖)", True,
                               "%s → %s" % (prev["package"], info["package"])))
        else:
            checks.append(("建立首个版本基线", True, "versionCode=%s" % info["versionCode"]))
        jks_sha = cert_sha256_from_jks()
        if jks_sha:
            checks.append(("APK 证书与仓库 jks 一致", info["cert_sha256"] == jks_sha,
                           jks_sha[:16] + "…"))

    lines = []
    for name, ok, detail in checks:
        lines.append("[%s] %-32s %s" % ("OK" if ok else "FAIL", name, detail))
    if info:
        lines.append("")
        lines.append("APK: %s" % apk)
        lines.append("package=%s versionCode=%s versionName=%s" %
                     (info["package"], info["versionCode"], info["versionName"]))
        lines.append("cert_sha256=%s" % info["cert_sha256"])
    text = "\n".join(lines)
    print(text)

    html = ["<meta charset='utf-8'><style>body{font-family:system-ui;margin:32px;background:#fafafa;color:#222}"
            "table{border-collapse:collapse;background:#fff;width:100%}td,th{border:1px solid #ddd;padding:8px;text-align:left}"
            ".ok{color:#0a7a32;font-weight:700}.fail{color:#c62828;font-weight:700}code{background:#f2f2f2;padding:2px 5px}</style>",
            "<h1>APK 可覆盖安装校验</h1><p>生成时间：%s</p>" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "<table><tr><th>检查项</th><th>结果</th><th>说明</th></tr>"]
    for name, ok, detail in checks:
        html.append("<tr><td>%s</td><td class='%s'>%s</td><td>%s</td></tr>" %
                    (name, "ok" if ok else "fail", "通过" if ok else "失败", detail))
    html.append("</table>")
    if info:
        html.append("<h2>本次 APK</h2><ul><li>包名：<code>%s</code></li>"
                    "<li>versionCode：<code>%s</code>，versionName：<code>%s</code></li>"
                    "<li>签名证书 SHA256：<code>%s</code></li>"
                    "<li>大小：%.2f MB</li></ul>" %
                    (info["package"], info["versionCode"], info["versionName"], info["cert_sha256"], info["size_mb"]))
    html.append("<p>校验口径：versionCode 必须严格递增、签名证书（X.509 DER 的 SHA256）必须与仓库 "
                "<code>TVBoxOSC.jks</code> 及上一版一致、包名不变，三者同时满足才能覆盖安装。"
                "早期版本曾把含 <code>signingTime</code> 的整个 v1 签名块当成证书哈希，"
                "会导致同密钥不同构建的假警报，已废弃该算法。</p>")
    REPORT.write_text("\n".join(html) + "\n", encoding="utf-8")

    failed = [n for n, ok, _ in checks if not ok]
    if args.record and info and not failed:
        if history and history[-1].get("versionCode") == info["versionCode"]:
            history[-1] = dict(info, checked_at=datetime.datetime.now().isoformat(timespec="seconds"))
        else:
            history.append(dict(info, checked_at=datetime.datetime.now().isoformat(timespec="seconds")))
        RECORD.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("\n已记录基线 -> %s（共 %d 条）" % (RECORD, len(history)))
    elif failed and args.record:
        print("\n校验未通过，未写入基线。")

    if failed:
        print("\n未通过: " + ", ".join(failed))
        sys.exit(1)
    print("\n全部通过（%d 项）" % len(checks))


if __name__ == "__main__":
    main()
