#!/usr/bin/env python3
"""GitHub Actions release 构建前注入签名配置。

只修改 CI 工作副本，不改动仓库内容。使用仓库自带的
.github/workflows/TVBoxOSC.jks（公开测试签名，密码 TVBoxOSC）。
"""
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD_GRADLE = ROOT / "app/build.gradle"
GRADLE_PROPERTIES = ROOT / "gradle.properties"
JKS_SRC = ROOT / ".github/workflows/TVBoxOSC.jks"
JKS_DST = ROOT / "app/TVBoxOSC.jks"

text = BUILD_GRADLE.read_text(encoding="utf-8")

if "signingConfigs" not in text:
    signing_block = """    signingConfigs {
        if (project.hasProperty("RELEASE_STORE_FILE")) {
            myConfig {
                storeFile file(RELEASE_STORE_FILE)
                storePassword RELEASE_STORE_PASSWORD
                keyAlias RELEASE_KEY_ALIAS
                keyPassword RELEASE_KEY_PASSWORD
                v1SigningEnabled true
                v2SigningEnabled true
            }
        }
    }

"""
    anchor = "    defaultConfig {"
    if anchor not in text:
        sys.exit("未找到 defaultConfig 锚点，build.gradle 结构可能已变化")
    text = text.replace(anchor, signing_block + anchor, 1)

hook = """            if (project.hasProperty("RELEASE_STORE_FILE")) {
                signingConfig signingConfigs.myConfig
            }
"""
if "signingConfig signingConfigs.myConfig" not in text:
    for build_type in ("        debug {\n", "        release {\n"):
        if build_type not in text:
            sys.exit("未找到 %s 锚点，build.gradle 结构可能已变化" % build_type.strip())
        text = text.replace(build_type, build_type + hook, 1)

BUILD_GRADLE.write_text(text, encoding="utf-8")

shutil.copyfile(JKS_SRC, JKS_DST)
with GRADLE_PROPERTIES.open("a", encoding="utf-8") as f:
    f.write(
        "\nRELEASE_STORE_FILE=./TVBoxOSC.jks\n"
        "RELEASE_KEY_ALIAS=TVBoxOSC\n"
        "RELEASE_STORE_PASSWORD=TVBoxOSC\n"
        "RELEASE_KEY_PASSWORD=TVBoxOSC\n"
    )

print("签名配置注入完成：app/build.gradle + app/TVBoxOSC.jks + gradle.properties")
