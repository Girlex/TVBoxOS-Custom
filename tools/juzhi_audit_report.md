# 橘汁 v3.0.8.0 APK 安全审计报告（后门 / 限制 / 去广告）

审计日期：2026-11-18　|　对象：`/home/feng/下载/橘汁_v3.0.8.0.apk`（包名 `com.asjdhjhd.mgd.p3`，104 MB）
方法：jadx 全量反编译（`/tmp/juzhi_extract/jadx_out`，约 4700 个类）+ Manifest 解码 + 全文模式扫描

## 一、后门检查结论：**未发现传统意义后门**

| 检查项 | 结果 |
|---|---|
| 短信 / 通讯录 / 通话记录读取 | ✅ 零命中（`content://sms`、`content://contacts`、`content://call_log` 全无） |
| Root 提权 / 漏洞利用 | ✅ 无。`Runtime.exec` 命中全部在广告/推送 SDK 内（getui、beizi、sigmob、umeng、bugly 等），只用于 `ps`/`top` 读设备状态 |
| App 自身动态加载 dex | ✅ 无。所有 `DexClassLoader` 命中都在广告 SDK 插件加载器（pangle/kwad/windmill/百度/gms dynamite） |
| 读应用列表 | ✅ 业务代码（com.hive/com.base）零命中；`QUERY_ALL_PACKAGES` 权限是广告 SDK 风控用 |
| 定位 | ✅ 业务代码零 `LocationManager`；精确定位权限仅供广告 SDK |
| 剪贴板 | 仅 UI 功能复制（日志/评论），无静默上传 |
| 硬编码可疑上报域名 | ✅ 无。业务包内 URL 全是功能相关（官网、BT 搜索、tracker、下载服） |
| exported 组件 | 全是常规入口（启动页/播放器/广告 SDK 路由/个推），无隐蔽远控入口 |

**但存在三类「灰色收集/控制」**：

1. **设备指纹上报**：每个请求带 `publicParams` 头（udid/uuid/androidID/机型/分辨率/网络等约 30 字段，明文可伪造，服务端只验存在性）。这是协议必需，无法去除。
2. **第三方 SDK 收集**：友盟统计 + 个推 + Bugly + 约 10 家广告 SDK（穿山甲/快手/广点通/百度/Sigmob/Beizi/美数/WindMill…），各自向自家域名上报设备与行为数据。可网络层屏蔽（见下）。
3. **服务端远控开关**：`config.app.security`（SecurityConfig）由服务器下发，含 `enabled` + `jumpUrl`——运营方可远程强制禁用/跳转；域名本身也由 OSS `az.txt` 远程切换。这不是后门，但意味着「服务端说了算」。

## 二、限制分析：全部是**服务端强制**，改客户端没用

1. **VIP/付费剧集**：每集数据带服务端下发的 `isVip`/`playable` 字段；播放地址必须经 `/api/ex/v3/security/videoUsableUrl`（v5 RSA 签名）由服务器逐集下发。客户端跳过 isVip 判断只会拿到「无地址」。
2. **签名绑定**：safeCodeKey 由 APK 签名证书 MD5/SHA1 推导 → **重新打包 APK 必然换签名 → 密钥推导失效 → 所有 security 接口 403，App 直接报废**。这是内置防篡改机制，也堵死了「改 APK 去限制/去广告」的路径。
3. 内容加密（双层 AES + proto + RSA 握手）密钥全静态，已全部打通（见 `juzhi_tvbox_client.py`）——这条是**移植 TVBox 的可行路径**，不依赖改 APK。

## 三、去广告：两条可行路线

**路线 A（推荐）：移植进 TVBox，广告天然不存在。** 广告 SDK 只在橘汁 App 里运行；写 csp 爬虫直接调它的内容 API，TVBox 里从头到尾不会加载任何广告 SDK。这也是本项目主线的做法。

**路线 B：继续用原 App，网络层屏蔽。** 电视盒子（无 root）改不了 `/etc/hosts`，需在路由器 / AdGuard Home / Pi-hole 层面生效。
清单：`tools/juzhi_adblock_hosts.txt`（59 个域名，全部在反编译源码中确认，按 SDK 分组）。
已验证不影响：视频播放（播放地址走业务域名，与广告域名无关）、搜索（www.baidu.com 保留）、手机号一键登录（cmpassport 保留）。
副作用：推送收不到、崩溃不再上报（都是预期效果）。

**路线 C（不可行）：改 APK 重打包去广告。** 签名一变密钥链全断，除非同时改写密钥推导逻辑（等于破解防篡改保护），不推荐也不必要——路由 A/B 已覆盖需求。

## 四、建议

- 继续按既定路线写 `csp_Juzi` 爬虫（v5 播放层还差实测，可用 `adb logcat -s NetSecurityRequest` 对照）。
- 若同时保留原 App 日常使用，把 hosts 清单挂到路由器即可完成「去广告 + 去上报」。
