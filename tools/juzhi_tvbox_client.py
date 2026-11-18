#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
橘汁 v3.0.8.0 API 参考客户端（TVBox 移植可行性验证）
=====================================================
逆向来源：/home/feng/下载/橘汁_v3.0.8.0.apk（包名 com.asjdhjhd.mgd.p3）

密钥推导链（全部静态，无设备绑定）：
1. APK 证书 DER → MD5/SHA1 大写 hex
   safeCode 明文 = MD5hex + "######" + SHA1hex + "~~~~~~" + 包名 + ">>>+++" + versionCode
2. safeCode = b64( AES-256-ECB( KEY2+KEY3+KEY5+MAX+TAIL, 明文 ) )
   safeCodeKey = ( b64(safeCode)[:16] + b64(safeCode)[-16:] ).upper()   ← 32 字节 AES 密钥
3. safeSecretKey = AES-256-ECB 解密( KEY1+KEY4+KEY6+MAX+TAIL, SAFE_SECRET_KEY 常量 )
   = "OC1A06E197EF10CF3F6058CA7A803B5E"（与服务端下发的 app.config.aeskey.aes_key 一致）

请求：
- 所有接口带 publicParams 头（设备信息 JSON，值可伪造，服务端只校验存在性/基本字段）
- security 系列响应：data = b64( AES(safeSecretKey, b64( AES(safeCodeKey, JSON) )) )
  （双层，先 safeCodeKey 后 safeSecretKey）

v5 protobuf 播放地址层（/api/proto/v5/videoUsableUrl 等）：
- SecureRequest proto {timestamp, aes_fakestr, aes_encrypt1, aes_encrypt2, random_str}
- aes_enc = AES(safeCodeKey, body_str + timestamp)；按 8:剩余 切成 encrypt1/encrypt2 前拼 8 位随机串
- publicParams 附加 sig=RSA(secondLevelPublic, ts+random+vApp)、sig2/sig3=AES(safeSecretKey, ts+random) 切 8:余
- secondLevelPublic = RSA 握手获得（str1..str5 跳过 fakePos 位拼接），RSA 公钥已从 APK 解出
- proto 字段定义见反编译源 com/base/model/proto/（RSAPublicProto/RSARequestProto/SecureRequestProto/DramaProto）

依赖：pip install pycryptodome
"""
import base64
import hashlib
import json
import os
import urllib.request

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

# ---------- 常量（全部提取自 APK） ----------
PKG = "com.asjdhjhd.mgd.p3"
VERSION_CODE = 3080
BASE = "http://juziappcc.hzhcbkj.cn"   # 由 OSS az.txt 下发，运营方可切换

KEY1, KEY2, KEY3, KEY4 = "AWSS@E", "11GK2w", "e32144", "AAA112"
KEY5, KEY6, MAXK, TAIL = "LO&hil", "U*Yjgt", "UITB)F", "Md1khdaF"
K1_CHAIN = KEY1 + KEY4 + KEY6 + MAXK + TAIL      # 解 OSS_URLS / SAFE_SECRET_KEY / M_P_N
GEN_CHAIN = KEY2 + KEY3 + KEY5 + MAXK + TAIL     # 生成 safeCode

OSS_URLS_CIPHER = ("s23SguEemJsHUSNVfv3BY9v14UGBeI0nT22HeZdh/ul8atECClbaPcm9rCjCIB73bWUmibJZr4Lh"
                   "zLqA3I/A/UJibJw1qoeCoGrQn+g7PmptZSaJslmvguHMuoDcj8D9EfEzKalqI2+GBaR9Ohs3WmMUqD8Vd6J"
                   "isLBSo1/cnMiidwtDMihoj+8yI9gX8A2Qcr1tUvXQag9AWwz+UgtjYtH4Wd3j45ogntoh3YsY2GwM72IXpR+U"
                   "hk3B1ooWeFN7nDizDepQHrfd+prK35covYJex7RrIsOnd3wlQHBhsPBF1lyoKRXZ/iyoIU4+T/aG")

CERT_MD5 = "3A4A23871C21D1F00D75C58AB5F688F8"          # APK 签名证书 DER MD5
CERT_SHA1 = "8F2E5859B53F5F488F7021A07B7EFA62EB8FEDBD"  # SHA1

RSA_PUBLIC_B64 = ("MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCr8SzZhjYy+rsya1K09t8d2K50pWFoBkgUqMpKOiW"
                  "+3IEVKd4eTdvg9RSOjQ82kypL6R9BnsmrS1V8s4PVDwjQbUtYhTPPC9Hz16qY7rpD6m0d2vr09/UpWQ5uOy9PR"
                  "0QTrsioveZ+DIe9jc3C+zBCu/kZSY/R8stwJoiitki3gwIDAQAB")

PUBLIC_PARAMS = json.dumps({
    "plat": "android", "v": "1", "vOs": "11", "_vOsCode": "30", "vApp": "3080",
    "vName": "3.0.8.0", "pkg": PKG, "appName": "%E6%A9%98%E6%B1%81",
    "mac": "02:00:00:00:00:00", "model": "Pixel 4", "brand": "google",
    "facturer": "Google", "udid": "a1b2c3d4e5f60718", "uuid": "8f7e6d5c4b3a2918",
    "chid": "10000", "androidID": "9c8b7a6d5e4f3012", "resolution": "1080x1920",
    "density": "2.625", "dpi": "420", "net": "WIFI", "carrier": "NA",
    "cpu": "arm64-v8a", "abid": "", "cpuId": "", "device": 0,
    "lang": "zh", "country": "CN", "tenantId": "", "young": 0,
}, ensure_ascii=False, separators=(",", ":"))


# ---------- AES 工具 ----------
def aes_ecb_encrypt(key: str, data: bytes) -> bytes:
    return AES.new(key.encode(), AES.MODE_ECB).encrypt(pad(data, 16))


def aes_ecb_decrypt(key: str, ct: bytes) -> bytes:
    return unpad(AES.new(key.encode(), AES.MODE_ECB).decrypt(ct), 16)


# ---------- 密钥推导 ----------
def derive_safe_code_key(cert_der_path: str) -> str:
    """由证书 DER 重新计算 safeCodeKey（也可直接用下方缓存值）。"""
    der = open(cert_der_path, "rb").read()
    md5 = hashlib.md5(der).hexdigest().upper()
    sha1 = hashlib.sha1(der).hexdigest().upper()
    plain = f"{md5}######{sha1}~~~~~~{PKG}>>>+++{VERSION_CODE}"
    sc1 = base64.b64encode(aes_ecb_encrypt(GEN_CHAIN, plain.encode())).decode()
    sc2 = base64.b64encode(sc1.encode()).decode()  # 双重 base64！
    return (sc2[:16] + sc2[-16:]).upper()


def derive_keys():
    """safeSecretKey 也可由 SAFE_SECRET_KEY 常量解出，这里直接用验证过的值。"""
    ssk = aes_ecb_decrypt(K1_CHAIN, base64.b64decode(
        "YT7MFs7DrSmwAwmTKbAK47GUyZpcPaaBswKoP9ex7XMCz8trus8GdDpBt80GqAk2")).decode()
    # 本 APK 的 safeCodeKey（由 derive_safe_code_key 算得）
    safe_key = "ZU1DSFFWNENKMDYXVUQRN0ZJDUC4PQ=="
    return safe_key, ssk


SAFE_KEY, SSK = derive_keys()


# ---------- HTTP ----------
def _get(path: str):
    req = urllib.request.Request(
        BASE + "/" + path.lstrip("/"),
        headers={"User-Agent": "okhttp/4.9.0", "publicParams": PUBLIC_PARAMS})
    return json.loads(urllib.request.urlopen(req, timeout=20).read().decode())


def sec_get(path: str):
    """security 系列接口：请求带 publicParams，响应双层 AES 解密。"""
    r = _get(path)
    if r.get("code") == 200 and isinstance(r.get("data"), str):
        inner = aes_ecb_decrypt(SAFE_KEY, base64.b64decode(r["data"])).decode()
        outer = aes_ecb_decrypt(SSK, base64.b64decode(inner)).decode()
        # 偶发 data 末尾多一段 JSON（服务端拼接），取第一个完整 JSON
        try:
            return json.JSONDecoder().raw_decode(outer)[0]
        except json.JSONDecodeError:
            return json.loads(outer)
    return r


# ---------- 业务接口 ----------
def oss_config_urls():
    """解密 APK 内置 OSS 配置源列表（az.txt → 真实 domain）。"""
    urls = aes_ecb_decrypt(K1_CHAIN, base64.b64decode(OSS_URLS_CIPHER)).decode()
    return urls.strip("[]").replace('"', "").split(",")


def get_domain_from_oss():
    for u in oss_config_urls():
        try:
            cfg = json.loads(urllib.request.urlopen(u, timeout=15).read().decode())
            if cfg.get("enabled"):
                return cfg["domain"]
        except Exception:
            continue
    return BASE


def rank_list():
    """排行榜列表（明文接口，code 200）。"""
    return _get("api/ex/v3/ranking/rankList")


def home_tags():
    """首页分类标签（security 加密）。"""
    return sec_get("api/ex/v3/security/tag/list")


def drama_recommend(drama_id: int):
    """影片详情+相关推荐（security 加密）。list[].name/coverImage/remark/brief"""
    return sec_get(f"api/ex/v3/security/drama/recommend?id={drama_id}")


def config_list():
    """全局配置（明文）。含 app.config.aeskey / config.app.security 等。"""
    return _get("api/v2/config/getConfigList.do?brief=true")


if __name__ == "__main__":
    print("safeCodeKey :", SAFE_KEY)
    print("safeSecretKey:", SSK)
    print("OSS 配置源  :", oss_config_urls())
    print()
    tags = home_tags()
    print("== 首页标签 ==", json.dumps(tags, ensure_ascii=False)[:300])
    print()
    rec = drama_recommend(656399)
    for it in rec.get("list", [])[:3]:
        print(f"  {it['name']} | {it.get('remark')} | {it['coverImage']['path']}")
    print()
    rl = rank_list()
    print("== 排行榜 ==", [(x["id"], x["name"]) for x in rl.get("data", [])][:6])
