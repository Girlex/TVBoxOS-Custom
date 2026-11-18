# 橘汁 v3.0.8.0 APK 接口源提取报告

提取日期：2026-11-18　|　源文件：`/home/feng/下载/橘汁_v3.0.8.0.apk`

## App 信息
- 包名：`com.asjdhjhd.mgd.p3`，versionCode 3080 / versionName 3.0.8.0，应用名「橘汁」
- 类型：闭源短剧/影视 App（hive 框架 + WindMill 广告），**不是 TVBox 壳**，无 `clan://`、无 spider、无 TVBox 配置格式

## API 接口源（当前激活）
```
http://juziappcc.hzhcbkj.cn
```
验证：`/api/v2/config/getConfigList.do?brief=true` 返回 `{"code":200,...}`，服务存活。

## 提取链路（三层）
1. **密钥注入**（`com.hive.module.MaxApplication.attachBaseContext`）：
   - KEY1=`AWSS@E` KEY2=`11GK2w` KEY3=`e32144` KEY4=`AAA112`
   - KEY5=`LO&hil` KEY6=`U*Yjgt` MAX=`UITB)F` AES尾串=`Md1khdaF`
   - AES 密钥 = KEY1+KEY4+KEY6+MAX+尾串 = `AWSS@EAAA112U*YjgtUITB)FMd1khdaF`（32 字节）
2. **解密 OSS 配置列表**（AES-256-ECB/PKCS7 解密 `OSS_URLS` 常量）：
   ```
   http://tenxuncos.gexuanyi.top/az.txt
   http://oss.gexuanyi.top/az.txt
   http://oss2.gexuanyi.top/az.txt
   https://1844283286.cdn.123clouddisk.com/1844283286/yybfyynb/az.txt   （已失效：未开启直链）
   https://1848463092.cdn.123clouddisk.com/1848463092/cct/az.txt
   ```
3. **OSS 配置内容**（4 个存活源一致）：
   ```json
   {"domain":"http://juziappcc.hzhcbkj.cn","enabled":true}
   ```

## 主要 API 端点（相对上述 domain）
- `POST /api/v2/config/getConfigList.do?brief=true` — 全局配置
- `/api/v3/drama/list`、`/api/v3/drama/getDetail`、`/api/v3/drama/getSeasonDetail`、`/api/v3/drama/getCategory?orderBy=type_id`
- `api/ex/v3/security/drama/list`、`api/ex/v3/security/videoUsableUrl` 等 security 前缀接口（App 内有签名/加密）
- 其他：`/api/live/getList.do`、`/api/v2/member/*`、`/api/v2/user/*`

## 备注
- 备用域名机制：OSS 的 `az.txt` 里的 `domain`/`domain2` 字段就是全部后端地址，运营方通过改 OSS 文件切换域名，无其他备用。
- 下载服务器：`http://download.jclyyr.ltd:8081/`；BT tracker：`tk1/tk2.jila.site:8160`。
- `assets/site_config` 是广告 SDK 配置（cypher:2 加密），与影视接口无关。
- 该源为 App 私有后端，**无法直接当 TVBox 接口使用**（需抓包分析其 security 签名逻辑才能脱离 App 调用）。

---

# TVBox 移植可行性分析（2026-11-18 第二轮逆向）

**结论：可以搬，加解密链路已全部打通并验证，需要写一个 csp 爬虫（dex jar）接入 TVBox。**

## 已打通（Python 参考客户端 `tools/juzhi_tvbox_client.py` 全部实测通过）
1. **safeCodeKey 推导**（32 字节静态 AES 密钥）：
   - 明文 = `证书MD5hex######证书SHA1hex~~~~~~com.asjdhjhd.mgd.p3>>>+++3080`
     （证书 MD5=`3A4A23871C21D1F00D75C58AB5F688F8`，SHA1=`8F2E5859B53F5F488F7021A07B7EFA62EB8FEDBD`）
   - safeCode = b64(AES-256-ECB(`11GK2we32144LO&hilUITB)FMd1khdaF`, 明文))
   - **safeCode 再做一次 Base64**（双重！），取首尾 16 字符大写拼接 = safeCodeKey
   - 本 APK 实际值：`ZU1DSFFWNENKMDYXVUQRN0ZJDUC4PQ==`
2. **safeSecretKey** = AES 解密 SAFE_SECRET_KEY 常量 = `OC1A06E197EF10CF3F6058CA7A803B5E`
   （与服务器 getConfigList 下发的 `app.config.aeskey.aes_key` 完全一致，交叉验证通过）
3. **publicParams 请求头**：设备信息 JSON 可伪造，服务端只验证存在性（缺失时报"公共参数请求头不能为空"）
4. **响应解密**：`data = b64( AES(SSK, b64( AES(safeCodeKey, JSON) )) )` 双层，均已实测解出明文
5. **其它常量全部解出**：M_P_N→包名、IJKLMOP→fakePos=1、HIJLKMO→RSA 公钥（1024bit SPKI）、
   ABCDEFG→构建时间戳；RSA 公钥模数与服务端 config 下发的 firstKey 密钥对一致（服务端甚至把私钥下发了）

## 实测可用的端点
| 端点 | 加密 | 状态 |
|---|---|---|
| `api/v2/config/getConfigList.do?brief=true` | 明文 | ✅ 全局配置 |
| `api/ex/v3/security/tag/list` | 双AES | ✅ 首页标签+轮播（含影片跳转 id） |
| `api/ex/v3/security/drama/recommend?id=N` | 双AES | ✅ 影片元数据+相关推荐（name/cover/remark/brief/actor） |
| `api/ex/v3/ranking/rankList` | 明文 | ✅ 排行榜（剧集/电影/综艺/动漫/短剧周榜，mode/hotType/cate 参数） |
| `api/ex/v3/drama/typeExtend?cate=1` | 明文 | ✅（返回分类扩展） |
| `api/ex/v3/security/drama/list` | ? | ❌ 路由 404（服务端已下线，App 新版走 v5） |
| `api/v3/drama/getDetail`、`/api/v3/drama/list` | ? | ❌ 路由 404 |
| `/api/proto/v5/drama/getDetail`、`/api/proto/v5/videoUsableUrl`、`/api/proto/v5/drama/popular` | protobuf+签名 | ⚠️ 结构已逆向，未实测 |

## v5 播放地址层（已逆向，未实测）
- 请求体 `SecureRequest` proto：`{timestamp, aes_fakestr(20位随机), aes_encrypt1, aes_encrypt2, random_str(8位随机)}`
  - `aes_enc = AES(safeCodeKey, "k=v&k=v" + timestamp)`；前拼 8 位 random 后按 20 切成 encrypt1/encrypt2
- publicParams 追加：`timestamp`、`random_str`(16位)、`sig` = RSA 加密(secondLevelPublic, ts+random+vApp)、
  `sig2/sig3` = AES(SSK, ts+random) 按 8 切分
- secondLevelPublic 通过 RSA 握手获取：请求 `RSARequest` proto，响应 `RSAPublic{str1..str5}`，
  按 fakePos=1 跳过 str2，拼接 str1+str3+str4+str5
- **所有密钥均静态可知**（safeCodeKey/SSK/RSA公钥都在 APK 里），proto 字段定义在反编译源
  `com/base/model/proto/*.java`，用 python 原生手拼 wire format 即可，无需 protobuf 库

## 移植路径（如要做）
1. 写 Java 爬虫类（实现 `com.github.catvod.crawler.Spider`），内置上述常量与解密逻辑
2. jdk8 `javac` 编译 + AOSP `dx` 转 dex + zip 成 jar，放到可访问的 URL 或内置 assets
3. TVBox 配置：`{"sites":[{"key":"juzi","name":"橘汁","type":3,"api":"csp_Juzi","jar":"..."}]}`
4. 播放地址：实现 v5 握手（用 `adb logcat -s NetSecurityRequest NetSecurityParamsHelper` 抓真机日志
   对照验证最省事——日志 tag 全部明文打印密钥与密文）
5. 风险：App 版本升级会换 cert/密钥（但 versionCode 参与推导，需按目标版本 APK 重算）；服务端可随时封 publicParams 异常

## 复现命令
```bash
printf 'AWSS@EAAA112U*YjgtUITB)FMd1khdaF' | xxd -p | tr -d '\n'   # 32字节密钥hex
# 解密 OSS_URLS：openssl enc -aes-256-ecb -d -a -K <hex> < OSS_URLS_b64
curl "http://juziappcc.hzhcbkj.cn/api/v2/config/getConfigList.do?brief=true"
/home/feng/.workbuddy/binaries/python/envs/default/bin/python tools/juzhi_tvbox_client.py
```
