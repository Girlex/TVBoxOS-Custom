package com.github.catvod.spider;

import android.content.Context;
import android.content.SharedPreferences;
import android.text.TextUtils;
import android.util.Base64;

import com.github.catvod.crawler.Spider;
import com.github.catvod.net.OkHttp;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.security.KeyFactory;
import java.security.PublicKey;
import java.security.SecureRandom;
import java.security.spec.X509EncodedKeySpec;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import javax.crypto.Cipher;
import javax.crypto.spec.SecretKeySpec;

import okhttp3.MediaType;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

/**
 * 橘汁短剧（v3.0.8.0 逆向移植）。
 * 密钥全部静态内置，无设备绑定；域名自愈：主域名探测失败后自动从内置 OSS
 * 备用源列表拉取运营方新域名（az.txt），持久化到本地偏好。
 *
 * 协议链路（全部实测验证 2026-11-18）：
 * - 握手 POST /api/v5/find/app/zone（RSARequest proto）→ RSAPublic{str1..str5}
 *   按 fakePos=1 拼接 str2+str3+str4+str5 = secondLevelPublic
 * - v5 业务请求 POST /api/proto/v5/*（SecureRequest proto）
 *   body 参数串+timestamp 用 safeCodeKey AES/ECB/PKCS7 加密，random8 前缀切 encrypt1/encrypt2
 *   publicParams 附加 timestamp/random_str/sig(RSA)/sig2/sig3(AES 切 8:余)
 * - 响应 ApiResult{code=1,msg=2,data=3}，data 为裸 protobuf
 * - v3 security 接口（tag/list）响应为双层 AES JSON：先 safeCodeKey 后 safeSecretKey
 */
public class Juzi extends Spider {

    // ---------- 静态常量（逆向自 APK，勿改） ----------
    private static final String PKG = "com.asjdhjhd.mgd.p3";
    private static final String VAPP = "3080";
    private static final String UA = "okhttp/4.9.0";
    private static final String DEFAULT_BASE = "http://juziappcc.hzhcbkj.cn";

    private static final String K1_CHAIN = "AWSS@E" + "AAA112" + "U*Yjgt" + "UITB)F" + "Md1khdaF";
    @SuppressWarnings("unused")
    private static final String GEN_CHAIN = "11GK2w" + "e32144" + "LO&hil" + "UITB)F" + "Md1khdaF";

    /** 响应第一层 / v5 请求体加密密钥（32 字节） */
    private static final String SAFE_KEY = "ZU1DSFFWNENKMDYXVUQRN0ZJDUC4PQ==";
    /** 响应第二层密钥（与服务器下发 app.config.aeskey 一致） */
    private static final String SSK = "OC1A06E197EF10CF3F6058CA7A803B5E";

    /** firstLevelPublic：RSA 握手签名用（X509 DER 的 Base64） */
    private static final String RSA_PUBLIC_B64 =
            "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCr8SzZhjYy+rsya1K09t8d2K50pWFoBkgUqMpKOiW"
                    + "+3IEVKd4eTdvg9RSOjQ82kypL6R9BnsmrS1V8s4PVDwjQbUtYhTPPC9Hz16qY7rpD6m0d2vr09/UpWQ5uOy9PR"
                    + "0QTrsioveZ+DIe9jc3C+zBCu/kZSY/R8stwJoiitki3gwIDAQAB";

    /** 内置 OSS 域名下发源密文（K1_CHAIN 解出 URL 列表，运营方改 az.txt 切换域名） */
    private static final String OSS_URLS_CIPHER =
            "s23SguEemJsHUSNVfv3BY9v14UGBeI0nT22HeZdh/ul8atECClbaPcm9rCjCIB73bWUmibJZr4Lh"
                    + "zLqA3I/A/UJibJw1qoeCoGrQn+g7PmptZSaJslmvguHMuoDcj8D9EfEzKalqI2+GBaR9Ohs3WmMUqD8Vd6J"
                    + "isLBSo1/cnMiidwtDMihoj+8yI9gX8A2Qcr1tUvXQag9AWwz+UgtjYtH4Wd3j45ogntoh3YsY2GwM72IXpR+U"
                    + "hk3B1ooWeFN7nDizDepQHrfd+prK35covYJex7RrIsOnd3wlQHBhsPBF1lyoKRXZ/iyoIU4+T/aG";

    /** 伪造设备 publicParams（服务端只做存在性/基本字段校验） */
    private static final String PUBLIC_PARAMS = "{\"plat\":\"android\",\"v\":\"1\",\"vOs\":\"11\","
            + "\"_vOsCode\":\"30\",\"vApp\":\"3080\",\"vName\":\"3.0.8.0\",\"pkg\":\"com.asjdhjhd.mgd.p3\","
            + "\"appName\":\"%E6%A9%98%E6%B1%81\",\"mac\":\"02:00:00:00:00:00\",\"model\":\"Pixel 4\","
            + "\"brand\":\"google\",\"facturer\":\"Google\",\"udid\":\"a1b2c3d4e5f60718\","
            + "\"uuid\":\"8f7e6d5c4b3a2918\",\"chid\":\"10000\",\"androidID\":\"9c8b7a6d5e4f3012\","
            + "\"resolution\":\"1080x1920\",\"density\":\"2.625\",\"dpi\":\"420\",\"net\":\"WIFI\","
            + "\"carrier\":\"NA\",\"cpu\":\"arm64-v8a\",\"abid\":\"\",\"cpuId\":\"\",\"device\":0,"
            + "\"lang\":\"zh\",\"country\":\"CN\",\"tenantId\":\"\",\"young\":0}";

    private static final String[] FALLBACK_CLASSES = {"12|剧集", "13|电影", "14|综艺", "15|动漫", "16|短剧"};

    private static final String PREF_NAME = "juzi_spider";
    private static final String PREF_BASE = "last_good_base";

    private static volatile String resolvedBase;
    private static volatile String secondPub;
    private static final SecureRandom RND = new SecureRandom();
    private static final String RAND_CHARS = "1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";

    // ---------- proto 手写编解码 ----------
    private static byte[] varint(long v) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        while (true) {
            int b = (int) (v & 0x7F);
            v >>>= 7;
            if (v != 0) out.write(b | 0x80);
            else {
                out.write(b);
                break;
            }
        }
        return out.toByteArray();
    }

    private static byte[] pbVarint(int field, long value) {
        byte[] head = {(byte) (field << 3)};
        return concat(head, varint(value));
    }

    private static byte[] pbBytes(int field, byte[] data) {
        byte[] head = {(byte) (field << 3 | 2)};
        return concat(concat(head, varint(data.length)), data);
    }

    private static byte[] pbStr(int field, String s) {
        return pbBytes(field, utf8(s));
    }

    private static byte[] concat(byte[] a, byte[] b) {
        byte[] r = new byte[a.length + b.length];
        System.arraycopy(a, 0, r, 0, a.length);
        System.arraycopy(b, 0, r, a.length, b.length);
        return r;
    }

    private static byte[] utf8(String s) {
        try {
            return s.getBytes("UTF-8");
        } catch (Throwable t) {
            return new byte[0];
        }
    }

    private static String str8(byte[] b) {
        try {
            return new String(b, "UTF-8");
        } catch (Throwable t) {
            return "";
        }
    }

    /** 极简 protobuf 解析：field -> List<Integer|byte[]> */
    private static final class Pb {
        final Map<Integer, List<Object>> f = new TreeMap<Integer, List<Object>>();

        static Pb parse(byte[] data) throws Exception {
            Pb p = new Pb();
            int i = 0;
            while (i < data.length) {
                int tag = 0, shift = 0;
                while (true) {
                    int b = data[i++] & 0xFF;
                    tag |= (b & 0x7F) << shift;
                    shift += 7;
                    if ((b & 0x80) == 0) break;
                }
                int field = tag >>> 3, wire = tag & 7;
                if (wire == 0) {
                    long v = 0;
                    shift = 0;
                    while (true) {
                        int b = data[i++] & 0xFF;
                        v |= (b & 0x7FL) << shift;
                        shift += 7;
                        if ((b & 0x80) == 0) break;
                    }
                    p.add(field, v);
                } else if (wire == 2) {
                    int len = 0;
                    shift = 0;
                    while (true) {
                        int b = data[i++] & 0xFF;
                        len |= (b & 0x7F) << shift;
                        shift += 7;
                        if ((b & 0x80) == 0) break;
                    }
                    byte[] chunk = Arrays.copyOfRange(data, i, i + len);
                    i += len;
                    p.add(field, chunk);
                } else if (wire == 5) {
                    i += 4;
                } else if (wire == 1) {
                    i += 8;
                } else {
                    throw new Exception("bad wire " + wire);
                }
            }
            return p;
        }

        void add(int field, Object v) {
            List<Object> l = f.get(field);
            if (l == null) {
                l = new ArrayList<Object>();
                f.put(field, l);
            }
            l.add(v);
        }

        long num(int field, long def) {
            List<Object> l = f.get(field);
            return l == null || l.isEmpty() ? def : (Long) l.get(0);
        }

        String str(int field) {
            List<Object> l = f.get(field);
            if (l == null || l.isEmpty()) return "";
            Object v = l.get(0);
            return v instanceof byte[] ? str8((byte[]) v) : String.valueOf(v);
        }

        byte[] bytes(int field) {
            List<Object> l = f.get(field);
            return l == null || l.isEmpty() ? new byte[0] : (byte[]) l.get(0);
        }

        List<byte[]> all(int field) {
            List<byte[]> out = new ArrayList<byte[]>();
            List<Object> l = f.get(field);
            if (l != null) for (Object v : l) if (v instanceof byte[]) out.add((byte[]) v);
            return out;
        }
    }

    // ---------- 加密 ----------
    private static byte[] aes(boolean enc, String key, byte[] data) throws Exception {
        Cipher c = Cipher.getInstance("AES/ECB/PKCS5Padding");
        c.init(enc ? Cipher.ENCRYPT_MODE : Cipher.DECRYPT_MODE,
                new SecretKeySpec(key.getBytes("UTF-8"), "AES"));
        return c.doFinal(data);
    }

    private static String rsaEncrypt(String pubB64, String text) throws Exception {
        PublicKey pk = KeyFactory.getInstance("RSA")
                .generatePublic(new X509EncodedKeySpec(Base64.decode(pubB64, Base64.DEFAULT)));
        Cipher c = Cipher.getInstance("RSA/ECB/PKCS1Padding");
        c.init(Cipher.ENCRYPT_MODE, pk);
        return Base64.encodeToString(c.doFinal(utf8(text)), Base64.NO_WRAP);
    }

    /** n-1 个不重复字符 + '='（与 App OooO0Oo 一致） */
    private static String rand(int n) {
        StringBuilder sb = new StringBuilder();
        HashSet<Character> used = new HashSet<Character>();
        while (sb.length() < n - 1) {
            char c = RAND_CHARS.charAt(RND.nextInt(RAND_CHARS.length()));
            if (used.add(c)) sb.append(c);
        }
        return sb.append('=').toString();
    }

    // ---------- 域名自愈 ----------
    private static boolean probe(String base) {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(base + "/api/ex/v3/ranking/rankList").openConnection();
            conn.setConnectTimeout(4000);
            conn.setReadTimeout(6000);
            conn.setRequestMethod("GET");
            conn.setRequestProperty("User-Agent", UA);
            conn.setRequestProperty("publicParams", PUBLIC_PARAMS);
            int code = conn.getResponseCode();
            return code == 200;
        } catch (Throwable t) {
            return false;
        } finally {
            if (conn != null) try { conn.disconnect(); } catch (Throwable ignored) {}
        }
    }

    private static void saveBase(Context ctx, String base) {
        try {
            SharedPreferences.Editor e = ctx.getSharedPreferences(PREF_NAME, 0).edit();
            e.putString(PREF_BASE, base).apply();
        } catch (Throwable ignored) {
        }
    }

    private static String base() {
        String cached = resolvedBase;
        if (cached != null) return cached;
        Context ctx = mContext;
        List<String> candidates = new ArrayList<String>();
        candidates.add(DEFAULT_BASE);
        if (ctx != null) {
            String saved = ctx.getSharedPreferences(PREF_NAME, 0).getString(PREF_BASE, "");
            if (!TextUtils.isEmpty(saved) && !candidates.contains(saved)) candidates.add(saved);
        }
        for (String c : candidates) {
            if (probe(c)) {
                resolvedBase = c;
                if (ctx != null) saveBase(ctx, c);
                return c;
            }
        }
        // 主域名与缓存都失联 → 从内置 OSS 源拉新域名（运营方切换域名的主通道）
        for (String oss : ossUrls()) {
            try {
                String body = OkHttp.string(oss);
                if (TextUtils.isEmpty(body)) continue;
                JSONObject cfg = new JSONObject(body.trim());
                if (!cfg.optBoolean("enabled", false)) continue;
                String[] domains = {cfg.optString("domain", ""), cfg.optString("domain2", "")};
                for (String d : domains) {
                    if (TextUtils.isEmpty(d)) continue;
                    if (probe(d)) {
                        resolvedBase = d;
                        if (ctx != null) saveBase(ctx, d);
                        return d;
                    }
                }
            } catch (Throwable ignored) {
            }
        }
        // 全部失联，仍返回默认域名让请求层报错
        return DEFAULT_BASE;
    }

    private static List<String> ossUrls() {
        List<String> out = new ArrayList<String>();
        try {
            byte[] pt = aes(false, K1_CHAIN, Base64.decode(OSS_URLS_CIPHER, Base64.DEFAULT));
            String s = str8(pt).trim()
                    .replace("[", "").replace("]", "").replace("\"", "").replace(" ", "");
            for (String u : s.split(",")) if (!TextUtils.isEmpty(u)) out.add(u);
        } catch (Throwable ignored) {
        }
        return out;
    }

    // ---------- v3 security 接口（双层 AES JSON，用于首页标签） ----------
    private static Map<String, String> ppHeaders(String pp) {
        Map<String, String> h = new LinkedHashMap<String, String>();
        h.put("User-Agent", UA);
        h.put("publicParams", pp);
        return h;
    }

    private String secGet(String path) {
        try {
            String resp = OkHttp.string(base() + "/" + path, ppHeaders(PUBLIC_PARAMS));
            JSONObject o = new JSONObject(resp);
            if (o.optInt("code", 0) != 200) return null;
            String data = o.optString("data", "");
            if (TextUtils.isEmpty(data)) return null;
            byte[] inner = aes(false, SAFE_KEY, Base64.decode(data, Base64.DEFAULT));
            byte[] outer = aes(false, SSK, Base64.decode(str8(inner), Base64.DEFAULT));
            return str8(outer);
        } catch (Throwable t) {
            return null;
        }
    }

    // ---------- v5 protobuf 层 ----------
    private static void handshake() throws Exception {
        long ts = System.currentTimeMillis();
        String r16 = rand(16);
        String sign = rsaEncrypt(RSA_PUBLIC_B64, ts + r16);
        byte[] body = concat(
                concat(pbVarint(1, ts), pbStr(2, sign)),
                concat(pbStr(3, rand(16)), concat(pbStr(4, r16), pbStr(5, rand(16)))));
        byte[] resp = postProto("/api/v5/find/app/zone", body, PUBLIC_PARAMS);
        Pb top = Pb.parse(resp);
        if (top.num(1, -1) != 200) throw new Exception("handshake code=" + top.num(1, -1));
        Pb pub = Pb.parse(top.bytes(3));
        // fakePos=1：跳过 str1，按 str2+str3+str4+str5 拼接公钥
        secondPub = pub.str(2) + pub.str(3) + pub.str(4) + pub.str(5);
        if (secondPub.length() < 100) throw new Exception("bad secondLevelPublic");
    }

    private static byte[] postProto(String path, byte[] body, String pp) throws Exception {
        Request.Builder b = new Request.Builder().url(base() + path)
                .post(RequestBody.create(MediaType.parse("application/x-protobuf"), body))
                .header("Content-Type", "application/x-protobuf")
                .header("Accept", "application/x-protobuf")
                .header("User-Agent", UA)
                .header("publicParams", pp);
        Response resp = client().newCall(b.build()).execute();
        try {
            return resp.body().bytes();
        } finally {
            resp.close();
        }
    }

    private static byte[] v5(String path, LinkedHashMap<String, String> params) throws Exception {
        if (TextUtils.isEmpty(secondPub)) handshake();
        for (int attempt = 0; attempt < 2; attempt++) {
            try {
                byte[] data = v5Once(path, params);
                Pb top = Pb.parse(data);
                long code = top.num(1, -1);
                if (code == 200) return top.bytes(3);
                if (attempt == 0) handshake(); // 签名/公钥可能过期，重新握手再试一次
                else throw new Exception(path + " code=" + code);
            } catch (Exception e) {
                if (attempt == 0) {
                    secondPub = null;
                    handshake();
                } else {
                    throw e;
                }
            }
        }
        throw new Exception("v5 unreachable");
    }

    private static byte[] v5Once(String path, LinkedHashMap<String, String> params) throws Exception {
        StringBuilder sb = new StringBuilder();
        for (Map.Entry<String, String> e : params.entrySet()) {
            if (sb.length() > 0) sb.append('&');
            sb.append(e.getKey()).append('=').append(e.getValue());
        }
        String bodyStr = sb.toString();
        long ts = System.currentTimeMillis();
        String r8 = rand(8), r20 = rand(20), r16 = rand(16);
        String cipherB64 = Base64.encodeToString(
                aes(true, SAFE_KEY, utf8(bodyStr + ts)), Base64.NO_WRAP);
        String combined = r8 + cipherB64;
        byte[] secure = concat(
                pbStr(1, combined.substring(0, 20)),
                concat(pbStr(2, combined.substring(20)),
                        concat(pbStr(3, r20), concat(pbVarint(4, ts), pbStr(5, r8)))));
        JSONObject pp = new JSONObject(PUBLIC_PARAMS);
        pp.put("timestamp", ts);
        pp.put("random_str", r16);
        pp.put("sig", rsaEncrypt(secondPub, ts + r16 + VAPP));
        String aesSig = Base64.encodeToString(
                aes(true, SSK, utf8(ts + r16)), Base64.NO_WRAP);
        pp.put("sig2", aesSig.substring(0, 8));
        pp.put("sig3", aesSig.substring(8));
        return postProto(path, secure, pp.toString());
    }

    // ---------- 数据组装 ----------
    private static JSONObject coverPic(Pb item) throws Exception {
        byte[] cover = item.bytes(2);
        if (cover.length == 0) return null;
        Pb c = Pb.parse(cover);
        String path = c.str(1);
        return TextUtils.isEmpty(path) ? null : new JSONObject().put("pic", path);
    }

    private static JSONObject vodFromList(Pb item) {
        try {
            JSONObject v = new JSONObject();
            v.put("vod_id", String.valueOf(item.num(3, 0)));
            v.put("vod_name", item.str(5));
            JSONObject pic = coverPic(item);
            v.put("vod_pic", pic == null ? "" : pic.optString("pic"));
            String remark = item.str(13);
            v.put("vod_remarks", remark);
            return v;
        } catch (Throwable t) {
            return null;
        }
    }

    private static JSONArray listFromArray(byte[] data) {
        JSONArray out = new JSONArray();
        try {
            Pb top = Pb.parse(data);
            for (byte[] raw : top.all(1)) {
                JSONObject v = vodFromList(Pb.parse(raw));
                if (v != null && v.optLong("vod_id", 0) > 0) out.put(v);
            }
        } catch (Throwable ignored) {
        }
        return out;
    }

    // ---------- Spider 接口 ----------
    @Override
    public String homeContent(boolean filter) {
        try {
            JSONArray classes = new JSONArray();
            // 排行榜接口为明文，实时拉取分类；失败回退内置清单
            List<String[]> pairs = new ArrayList<String[]>();
            try {
                String resp = OkHttp.string(base() + "/api/ex/v3/ranking/rankList", ppHeaders(PUBLIC_PARAMS));
                JSONObject o = new JSONObject(resp);
                JSONArray arr = o.optJSONArray("data");
                if (arr != null) {
                    for (int i = 0; i < arr.length(); i++) {
                        JSONObject r = arr.optJSONObject(i);
                        if (r == null) continue;
                        if (r.optInt("state", 1) != 1) continue;
                        pairs.add(new String[]{String.valueOf(r.optLong("id", 0)), r.optString("name")});
                    }
                }
            } catch (Throwable ignored) {
            }
            if (pairs.isEmpty()) {
                for (String s : FALLBACK_CLASSES) {
                    String[] kv = s.split("\\|");
                    pairs.add(new String[]{kv[0], kv[1]});
                }
            }
            for (String[] p : pairs) {
                if (p[0].equals("0")) continue;
                JSONObject c = new JSONObject();
                c.put("type_id", p[0]);
                c.put("type_name", p[1]);
                classes.put(c);
            }
            JSONObject result = new JSONObject();
            result.put("class", classes);
            return result.toString();
        } catch (Throwable t) {
            return "";
        }
    }

    @Override
    public String homeVideoContent() {
        try {
            String raw = secGet("api/ex/v3/security/tag/list");
            if (TextUtils.isEmpty(raw)) return "";
            JSONArray modules = new JSONArray(raw);
            JSONArray list = new JSONArray();
            HashSet<String> seen = new HashSet<String>();
            for (int i = 0; i < modules.length() && list.length() < 24; i++) {
                JSONObject m = modules.optJSONObject(i);
                if (m == null) continue;
                JSONArray sections = m.optJSONArray("sections");
                if (sections == null) continue;
                for (int j = 0; j < sections.length() && list.length() < 24; j++) {
                    JSONArray vods = sections.optJSONObject(j).optJSONArray("vodList");
                    if (vods == null) continue;
                    for (int k = 0; k < vods.length() && list.length() < 24; k++) {
                        JSONObject d = vods.optJSONObject(k);
                        if (d == null) continue;
                        String id = String.valueOf(d.optLong("id", 0));
                        if (id.equals("0") || seen.contains(id)) continue;
                        seen.add(id);
                        JSONObject v = new JSONObject();
                        v.put("vod_id", id);
                        v.put("vod_name", d.optString("name"));
                        JSONObject img = d.optJSONObject("coverImage");
                        v.put("vod_pic", img == null ? "" : img.optString("path"));
                        v.put("vod_remarks", d.optString("remark"));
                        list.put(v);
                    }
                }
            }
            return new JSONObject().put("list", list).toString();
        } catch (Throwable t) {
            return "";
        }
    }

    @Override
    public String categoryContent(String tid, String pg, boolean filter, HashMap<String, String> extend) {
        try {
            LinkedHashMap<String, String> params = new LinkedHashMap<String, String>();
            params.put("id", tid);
            params.put("page", TextUtils.isEmpty(pg) ? "1" : pg);
            params.put("pagesize", "24");
            byte[] data = v5("/api/proto/v5/rank/dramaList", params);
            JSONArray list = listFromArray(data);
            int page = 1;
            try { page = Integer.parseInt(pg); } catch (Throwable ignored) {}
            JSONObject result = new JSONObject();
            result.put("list", list);
            result.put("page", page);
            result.put("pagecount", list.length() >= 24 ? page + 1 : page);
            result.put("limit", 24);
            result.put("total", list.length());
            return result.toString();
        } catch (Throwable t) {
            return "";
        }
    }

    @Override
    public String detailContent(List<String> ids) {
        try {
            LinkedHashMap<String, String> params = new LinkedHashMap<String, String>();
            params.put("id", ids.get(0));
            byte[] data = v5("/api/proto/v5/drama/getDetail", params);
            Pb d = Pb.parse(data);
            JSONObject v = new JSONObject();
            v.put("vod_id", String.valueOf(d.num(4, 0)));
            v.put("vod_name", d.str(9));
            try {
                Pb cover = Pb.parse(d.bytes(2));
                v.put("vod_pic", cover.str(1));
            } catch (Throwable ignored) {
                v.put("vod_pic", "");
            }
            v.put("vod_area", d.str(1));
            v.put("vod_year", d.str(18));
            v.put("vod_actor", d.str(25));
            v.put("vod_director", d.str(12));
            String content = d.str(6);
            if (TextUtils.isEmpty(content)) content = d.str(7);
            v.put("vod_content", content);
            String remark = d.str(26);
            v.put("vod_remarks", remark.length() > 40 ? "" : remark);
            // 剧集分组：source 代码 -> 集列表；flag 用 sourceCn 展示
            LinkedHashMap<String, String> flagNames = new LinkedHashMap<String, String>();
            LinkedHashMap<String, StringBuilder> flagEps = new LinkedHashMap<String, StringBuilder>();
            for (byte[] raw : d.all(29)) {
                Pb vid = Pb.parse(raw);
                String path = vid.str(4);
                if (TextUtils.isEmpty(path)) continue;
                String source = vid.str(9);
                if (TextUtils.isEmpty(source)) source = "default";
                String cn = vid.str(10);
                if (TextUtils.isEmpty(cn)) cn = source;
                String title = vid.str(2);
                if (TextUtils.isEmpty(title)) title = vid.str(3);
                if (TextUtils.isEmpty(title)) title = String.valueOf(vid.num(13, 0));
                if (!flagNames.containsKey(source)) {
                    flagNames.put(source, cn);
                    flagEps.put(source, new StringBuilder());
                }
                StringBuilder eps = flagEps.get(source);
                if (eps.length() > 0) eps.append('#');
                eps.append(source).append('@').append(URLEncoder.encode(path, "UTF-8"))
                        .append('$').append(title);
            }
            StringBuilder from = new StringBuilder();
            StringBuilder urls = new StringBuilder();
            for (String source : flagNames.keySet()) {
                if (from.length() > 0) {
                    from.append("$$$");
                    urls.append("$$$");
                }
                from.append(flagNames.get(source));
                urls.append(flagEps.get(source));
            }
            v.put("vod_play_from", from.toString());
            v.put("vod_play_url", urls.toString());
            JSONArray list = new JSONArray().put(v);
            return new JSONObject().put("list", list).toString();
        } catch (Throwable t) {
            return "";
        }
    }

    @Override
    public String searchContent(String key, boolean quick) {
        try {
            LinkedHashMap<String, String> params = new LinkedHashMap<String, String>();
            params.put("searchKeys", key);
            byte[] data = v5("/api/proto/v5/drama/search", params);
            JSONArray list = listFromArray(data);
            return new JSONObject().put("list", list).toString();
        } catch (Throwable t) {
            return "";
        }
    }

    @Override
    public String playerContent(String flag, String id, List<String> vipFlags) {
        try {
            String[] parts = id.split("@", 2);
            String source = parts.length > 0 ? parts[0] : "";
            String vid = parts.length > 1 ? parts[1] : "";
            if (TextUtils.isEmpty(vid)) return "";
            LinkedHashMap<String, String> params = new LinkedHashMap<String, String>();
            params.put("vodPlayFrom", source);
            params.put("playUrl", vid);
            byte[] data = v5("/api/proto/v5/videoUsableUrl", params);
            Pb top = Pb.parse(data);
            String url = top.str(1);
            if (TextUtils.isEmpty(url)) return "";
            JSONObject result = new JSONObject();
            result.put("parse", 0);
            result.put("playUrl", "");
            result.put("url", url);
            result.put("header", "{\"User-Agent\":\"" + UA + "\"}");
            return result.toString();
        } catch (Throwable t) {
            return "";
        }
    }
}
