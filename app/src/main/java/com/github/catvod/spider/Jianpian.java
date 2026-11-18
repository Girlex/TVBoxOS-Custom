package com.github.catvod.spider;

import android.content.Context;
import android.content.SharedPreferences;
import android.text.TextUtils;

import com.github.catvod.crawler.Spider;
import com.github.catvod.net.OkHttp;

import org.json.JSONArray;
import org.json.JSONObject;

import java.net.HttpURLConnection;
import java.net.URLEncoder;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

/**
 * 荐片（JP22 v4.2.3，包名 com.qihoo.jp22，武汉一联网络科技）API 逆向移植。
 * 协议全部明文 GET/POST JSON，无加密，2026-11-18 实测验证：
 *
 * - 域名自愈（MainDOHUtils 逆向）：DOH TXT 记录 swrdsfeiujo25sw.cc 返回逗号分隔域名列表，
 *   逐个探测 GET https://jp<rand>.<domain>/api/so/list?channel=Ali&version=&arm=v8a，
 *   200 即可用；探活成功后持久化 SharedPreferences。
 * - 分类列表 GET /api/search/searchList?category_id=<id>&page=<pg>（每页 10 条）
 * - 短剧详情 GET /api/detail?vid=<id> → data.playlist[]（title/url 全量直链 mp4）
 * - 长视频详情 GET /api/video/detailv2?id=<id> → data.source_list_source[]（多线路 m3u8）
 * - 搜索 GET /api/v2/search/videoV2?key=<kw>&page=1&pageSize=20（category_id=67 可限短剧）
 * - 图片 GET /api/packageDomainConfig → data.imgDomain（逗号分隔静态域名，拼 thumbnail 相对路径）
 * - 播放均为直链（短剧 mp4 / 长视频 m3u8），无需二次解析
 *
 * vod_id 约定：短剧前缀 s，长视频前缀 l，详情按前缀路由（无前缀时双接口轮试）。
 */
public class Jianpian extends Spider {

    private static final String UA = "jianpian-android/101";
    private static final String DOH_URL = "https://dns.alidns.com/resolve?name=swrdsfeiujo25sw.cc&type=16";
    private static final String DOH_FALLBACK_URL = "https://doh.pub/dns-query?name=swrdsfeiujo25sw.cc&type=16";

    /** TXT 记录失效时的内置兜底域名（与 App DOH 域名池一致） */
    private static final String[] FALLBACK_DOMAINS = {
            "zxfmj.com", "cqxkn.com", "whhyn.com", "cqlzn.com", "hzlfm.com"
    };
    /** 图片静态域名兜底（与 /api/packageDomainConfig 返回一致） */
    private static final String[] FALLBACK_IMG = {
            "static.hzkqw.com", "static.bjynj.com", "static.cdgkb.com"
    };

    /** 实测不可播的播放 CDN（设备 2026-11-18 验证：DNS 不解析/连接超时），其线路排到末尾 */
    private static final String[] BAD_PLAY_HOSTS = {"mv.gzlcl.com", "dytt-tvs.com"};

    /** 分类兜底（/api/term/ad_fenlei 拉取失败时使用） */
    private static final String[] FALLBACK_CLASSES = {
            "88|推荐", "1|电影", "2|电视剧", "67|短剧", "3|动漫", "4|综艺"
    };

    private static final String PREF_NAME = "jianpian_spider";
    private static final String PREF_BASE = "last_good_base";
    private static final String PREF_IMG = "img_base";

    private static volatile String resolvedBase;
    private static volatile String imgBase;
    private static final Random RND = new Random();

    @Override
    public void init(Context context) {
        super.init(context);
    }

    /* ---------- 域名自愈 ---------- */

    private static List<String> domainsFromDoh(String dohUrl) {
        try {
            Map<String, String> headers = new HashMap<String, String>();
            headers.put("accept", "application/dns-json");
            String resp = OkHttp.string(dohUrl, headers);
            if (TextUtils.isEmpty(resp)) return null;
            JSONObject o = new JSONObject(resp);
            JSONArray answers = o.optJSONArray("Answer");
            if (answers == null) return null;
            for (int i = 0; i < answers.length(); i++) {
                JSONObject a = answers.optJSONObject(i);
                if (a == null || a.optInt("type", 0) != 16) continue;
                String data = a.optString("data", "").replace("\"", "");
                if (TextUtils.isEmpty(data)) continue;
                List<String> out = new ArrayList<String>();
                for (String d : data.split(",")) {
                    d = d.trim();
                    if (!TextUtils.isEmpty(d)) out.add(d);
                }
                if (!out.isEmpty()) return out;
            }
        } catch (Throwable ignored) {
        }
        return null;
    }

    private static boolean probe(String base) {
        // 短超时 HttpURLConnection（OkHttp 默认超时太长，死域名会拖慢首次加载）
        try {
            HttpURLConnection conn = (HttpURLConnection) new java.net.URL(
                    base + "api/so/list?channel=Ali&version=&arm=v8a").openConnection();
            conn.setRequestMethod("GET");
            conn.setConnectTimeout(5000);
            conn.setReadTimeout(5000);
            conn.setRequestProperty("User-Agent", UA);
            int code = conn.getResponseCode();
            if (code != 200) return false;
            java.io.BufferedReader reader = new java.io.BufferedReader(
                    new java.io.InputStreamReader(conn.getInputStream(), "UTF-8"));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null) sb.append(line);
            reader.close();
            conn.disconnect();
            return new JSONObject(sb.toString()).optInt("code", 0) == 1;
        } catch (Throwable t) {
            return false;
        }
    }

    private static void savePref(String key, String val) {
        try {
            Context ctx = mContext;
            if (ctx == null) return;
            SharedPreferences.Editor e = ctx.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE).edit();
            e.putString(key, val);
            e.apply();
        } catch (Throwable ignored) {
        }
    }

    private static String loadPref(String key) {
        try {
            Context ctx = mContext;
            if (ctx == null) return null;
            return ctx.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE).getString(key, null);
        } catch (Throwable ignored) {
            return null;
        }
    }

    private static String base() {
        String cached = resolvedBase;
        if (cached != null) return cached;
        cached = loadPref(PREF_BASE);
        if (cached != null && probe(cached)) {
            resolvedBase = cached;
            return cached;
        }
        // 实测最稳的域名排最前，避免死域名逐个超时；DOH 结果去重后跟在后面
        LinkedHashMap<String, Boolean> ordered = new LinkedHashMap<String, Boolean>();
        ordered.put(FALLBACK_DOMAINS[0], Boolean.TRUE); // zxfmj.com
        List<String> domains = domainsFromDoh(DOH_URL);
        if (domains == null) domains = domainsFromDoh(DOH_FALLBACK_URL);
        if (domains != null) {
            for (String d : domains) ordered.put(d, Boolean.TRUE);
        }
        for (String d : FALLBACK_DOMAINS) ordered.put(d, Boolean.TRUE);
        for (String d : ordered.keySet()) {
            // 与 App 一致：随机子域绕过 CDN 缓存
            String sub = "jp" + (100 + RND.nextInt(900)) + "." + d;
            String candidate = "https://" + sub + "/";
            if (probe(candidate)) {
                resolvedBase = candidate;
                savePref(PREF_BASE, candidate);
                return candidate;
            }
        }
        // 全部失败：返回默认域名，靠请求自身的容错兜底
        String def = "https://jp100.zxfmj.com/";
        resolvedBase = def;
        return def;
    }

    private static String img() {
        String cached = imgBase;
        if (cached != null) return cached;
        cached = loadPref(PREF_IMG);
        if (cached != null) {
            imgBase = cached;
            return cached;
        }
        try {
            String resp = OkHttp.string(base() + "api/packageDomainConfig", headers());
            JSONObject o = new JSONObject(resp);
            String csv = o.optJSONObject("data") == null ? "" : o.optJSONObject("data").optString("imgDomain");
            if (!TextUtils.isEmpty(csv)) {
                String first = csv.split(",")[0].trim();
                if (!TextUtils.isEmpty(first)) {
                    imgBase = "https://" + first + "/";
                    savePref(PREF_IMG, imgBase);
                    return imgBase;
                }
            }
        } catch (Throwable ignored) {
        }
        imgBase = "https://" + FALLBACK_IMG[0] + "/";
        return imgBase;
    }

    private static Map<String, String> headers() {
        Map<String, String> h = new HashMap<String, String>();
        h.put("User-Agent", UA);
        return h;
    }

    private static String get(String url) {
        try {
            return OkHttp.string(url, headers());
        } catch (Throwable t) {
            return "";
        }
    }

    private static String pic(String path) {
        if (TextUtils.isEmpty(path)) return "";
        if (path.startsWith("http")) return path;
        return img() + (path.startsWith("/") ? path.substring(1) : path);
    }

    /* ---------- 内容接口 ---------- */

    private static JSONObject vodFromSearch(JSONObject d, String prefix) throws Exception {
        JSONObject v = new JSONObject();
        String id = prefix + d.optLong("id", 0);
        v.put("vod_id", id);
        v.put("vod_name", d.optString("title"));
        v.put("vod_pic", pic(d.optString("thumbnail")));
        String mask = d.optString("mask");
        v.put("vod_remarks", mask == null ? "" : mask);
        return v;
    }

    @Override
    public String homeContent(boolean filter) {
        try {
            JSONArray classes = new JSONArray();
            List<String[]> pairs = new ArrayList<String[]>();
            try {
                String resp = get(base() + "api/term/ad_fenlei");
                JSONObject o = new JSONObject(resp);
                JSONArray arr = o.optJSONArray("data");
                if (arr != null) {
                    for (int i = 0; i < arr.length(); i++) {
                        JSONObject c = arr.optJSONObject(i);
                        if (c == null) continue;
                        if (c.optInt("pid", 0) != 0) continue;
                        if (c.optInt("status", 1) != 1) continue;
                        int id = c.optInt("id", 0);
                        String name = c.optString("name");
                        if (id == 0 || TextUtils.isEmpty(name)) continue;
                        pairs.add(new String[]{String.valueOf(id), name});
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
            // 推荐位：全站推荐列表第一条数据
            String resp = get(base() + "api/search/searchList?category_id=88&page=1");
            JSONObject o = new JSONObject(resp);
            JSONArray arr = o.optJSONArray("data");
            if (arr != null && arr.length() > 0) {
                JSONObject v = vodFromSearch(arr.optJSONObject(0), "l");
                return new JSONObject().put("list", new JSONArray().put(v)).toString();
            }
        } catch (Throwable ignored) {
        }
        return "";
    }

    @Override
    public String categoryContent(String tid, String pg, boolean filter, HashMap<String, String> extend) {
        try {
            int page = 1;
            try {
                page = Integer.parseInt(pg);
            } catch (Throwable ignored) {
            }
            if (page < 1) page = 1;
            String resp = get(base() + "api/search/searchList?category_id=" + tid + "&page=" + page);
            JSONObject o = new JSONObject(resp);
            JSONArray arr = o.optJSONArray("data");
            JSONArray list = new JSONArray();
            if (arr != null) {
                for (int i = 0; i < arr.length(); i++) {
                    JSONObject d = arr.optJSONObject(i);
                    if (d == null) continue;
                    // category_id=67 为短剧，走 /api/detail；其余为长视频，走 /api/video/detailv2
                    boolean drama = d.optInt("category_id", 0) == 67;
                    list.put(vodFromSearch(d, drama ? "s" : "l"));
                }
            }
            JSONObject result = new JSONObject();
            result.put("list", list);
            result.put("page", page);
            result.put("pagecount", arr != null && arr.length() >= 10 ? page + 1 : page);
            result.put("limit", arr == null ? 0 : arr.length());
            result.put("total", arr == null ? 0 : arr.length());
            return result.toString();
        } catch (Throwable t) {
            return "";
        }
    }

    private static boolean hasBadHost(JSONObject source) {
        try {
            JSONArray eps = source.optJSONArray("source_list");
            if (eps == null) return false;
            for (int i = 0; i < eps.length(); i++) {
                JSONObject ep = eps.optJSONObject(i);
                if (ep == null) continue;
                String url = ep.optString("url");
                if (TextUtils.isEmpty(url)) continue;
                for (String bad : BAD_PLAY_HOSTS) {
                    if (url.contains(bad)) return true;
                }
            }
        } catch (Throwable ignored) {
        }
        return false;
    }

    /** 短剧详情：/api/detail?vid=，playlist 单线路 */
    private JSONObject detailDrama(String id, JSONObject v) throws Exception {
        String resp = get(base() + "api/detail?token=&vid=" + id);
        JSONObject o = new JSONObject(resp);
        if (o.optInt("code", 0) != 1) return null;
        JSONObject d = o.optJSONObject("data");
        if (d == null) return null;
        v.put("vod_id", "s" + id);
        v.put("vod_name", d.optString("title"));
        v.put("vod_pic", pic(d.optString("cover_image")));
        v.put("vod_content", d.optString("description"));
        v.put("vod_remarks", d.optInt("episodes_count", 0) + "集");
        String score = String.valueOf(d.optDouble("score", 0));
        if (!"0.0".equals(score)) v.put("vod_score", score);
        JSONArray cats = d.optJSONArray("categories");
        if (cats != null && cats.length() > 0) v.put("vod_class", cats.join(","));
        JSONArray pl = d.optJSONArray("playlist");
        StringBuilder eps = new StringBuilder();
        if (pl != null) {
            for (int i = 0; i < pl.length(); i++) {
                JSONObject ep = pl.optJSONObject(i);
                if (ep == null) continue;
                String url = ep.optString("url");
                if (TextUtils.isEmpty(url)) continue;
                String title = ep.optString("title");
                if (TextUtils.isEmpty(title)) title = "第" + (i + 1) + "集";
                if (eps.length() > 0) eps.append('#');
                eps.append(title.replace("$", "_").replace("#", "_")).append('$')
                        .append(URLEncoder.encode(url, "UTF-8"));
            }
        }
        if (eps.length() == 0) return null;
        v.put("vod_play_from", "荐片短剧");
        v.put("vod_play_url", eps.toString());
        return v;
    }

    /** 长视频详情：/api/video/detailv2?id=，多线路 m3u8 */
    private JSONObject detailVideo(String id, JSONObject v) throws Exception {
        String resp = get(base() + "api/video/detailv2?id=" + id);
        JSONObject o = new JSONObject(resp);
        if (o.optInt("code", 0) != 1) return null;
        JSONObject d = o.optJSONObject("data");
        if (d == null) return null;
        v.put("vod_id", "l" + id);
        v.put("vod_name", d.optString("title"));
        v.put("vod_pic", pic(d.optString("thumbnail")));
        v.put("vod_content", d.optString("description"));
        v.put("vod_year", d.optString("year"));
        v.put("vod_area", d.optString("area"));
        String score = d.optString("score");
        if (!TextUtils.isEmpty(score) && !"0.0".equals(score)) v.put("vod_score", score);
        StringBuilder actors = new StringBuilder();
        JSONArray arr = d.optJSONArray("actors");
        if (arr != null) {
            for (int i = 0; i < arr.length() && i < 12; i++) {
                JSONObject a = arr.optJSONObject(i);
                String n = a == null ? arr.optString(i) : a.optString("name");
                if (TextUtils.isEmpty(n)) continue;
                if (actors.length() > 0) actors.append(",");
                actors.append(n);
            }
        }
        v.put("vod_actor", actors.toString());
        StringBuilder directors = new StringBuilder();
        arr = d.optJSONArray("directors");
        if (arr != null) {
            for (int i = 0; i < arr.length() && i < 6; i++) {
                JSONObject a = arr.optJSONObject(i);
                String n = a == null ? arr.optString(i) : a.optString("name");
                if (TextUtils.isEmpty(n)) continue;
                if (directors.length() > 0) directors.append(",");
                directors.append(n);
            }
        }
        v.put("vod_director", directors.toString());
        StringBuilder types = new StringBuilder();
        arr = d.optJSONArray("types");
        if (arr != null) {
            for (int i = 0; i < arr.length(); i++) {
                JSONObject a = arr.optJSONObject(i);
                String n = a == null ? "" : a.optString("name");
                if (TextUtils.isEmpty(n)) continue;
                if (types.length() > 0) types.append(",");
                types.append(n);
            }
        }
        if (types.length() > 0) v.put("vod_class", types.toString());
        String mask = d.optString("mask");
        v.put("vod_remarks", mask == null ? "" : mask);
        // 多线路：source_list_source[] { name, source_list[] { source_name, url } }
        // 两轮遍历：可播线路在前；"VIP线路"与实测坏 CDN 线路排最后
        LinkedHashMap<String, StringBuilder> flags = new LinkedHashMap<String, StringBuilder>();
        JSONArray sources = d.optJSONArray("source_list_source");
        for (int round = 0; round < 2 && sources != null; round++) {
            for (int i = 0; i < sources.length(); i++) {
                JSONObject s = sources.optJSONObject(i);
                if (s == null) continue;
                String flag = s.optString("name");
                if (TextUtils.isEmpty(flag)) flag = "线路" + (i + 1);
                boolean isBad = flag.startsWith("VIP") || hasBadHost(s);
                if (round == 0 && isBad) continue;
                if (round == 1 && !isBad) continue;
                JSONArray eps = s.optJSONArray("source_list");
                if (eps == null) continue;
                for (int j = 0; j < eps.length(); j++) {
                    JSONObject ep = eps.optJSONObject(j);
                    if (ep == null) continue;
                    String url = ep.optString("url");
                    if (TextUtils.isEmpty(url)) continue;
                    String title = ep.optString("source_name");
                    if (TextUtils.isEmpty(title)) title = "第" + (j + 1) + "集";
                    StringBuilder sb = flags.get(flag);
                    if (sb == null) {
                        sb = new StringBuilder();
                        flags.put(flag, sb);
                    }
                    if (sb.length() > 0) sb.append('#');
                    sb.append(title.replace("$", "_").replace("#", "_")).append('$')
                            .append(URLEncoder.encode(url, "UTF-8"));
                }
            }
        }
        if (flags.isEmpty()) return null;
        StringBuilder from = new StringBuilder();
        StringBuilder urls = new StringBuilder();
        for (String flag : flags.keySet()) {
            if (from.length() > 0) {
                from.append("$$$");
                urls.append("$$$");
            }
            from.append(flag);
            urls.append(flags.get(flag));
        }
        v.put("vod_play_from", from.toString());
        v.put("vod_play_url", urls.toString());
        return v;
    }

    @Override
    public String detailContent(List<String> ids) {
        try {
            String raw = ids.get(0);
            String prefix = raw.length() > 1 ? raw.substring(0, 1) : "";
            String id = raw.startsWith("s") || raw.startsWith("l") ? raw.substring(1) : raw;
            JSONObject v = new JSONObject();
            JSONObject done;
            if ("s".equals(prefix)) {
                done = detailDrama(id, v);
                if (done == null) done = detailVideo(id, v);
            } else if ("l".equals(prefix)) {
                done = detailVideo(id, v);
                if (done == null) done = detailDrama(id, v);
            } else {
                done = detailVideo(id, v);
                if (done == null) done = detailDrama(id, v);
            }
            if (done == null) return "";
            JSONArray list = new JSONArray().put(v);
            return new JSONObject().put("list", list).toString();
        } catch (Throwable t) {
            return "";
        }
    }

    @Override
    public String searchContent(String key, boolean quick) {
        try {
            String kw = URLEncoder.encode(key, "UTF-8");
            JSONArray list = new JSONArray();
            // 全库搜索 + 短剧搜索合并（接口支持 category_id 过滤）
            String[][] scopes = {{"", "l"}, {"67", "s"}};
            for (String[] scope : scopes) {
                String url = base() + "api/v2/search/videoV2?key=" + kw
                        + (TextUtils.isEmpty(scope[0]) ? "" : "&category_id=" + scope[0])
                        + "&page=1&pageSize=20";
                String resp = get(url);
                if (TextUtils.isEmpty(resp)) continue;
                JSONObject o = new JSONObject(resp);
                if (o.optInt("code", 0) != 1) continue;
                JSONArray arr = o.optJSONArray("data");
                if (arr == null) continue;
                for (int i = 0; i < arr.length(); i++) {
                    JSONObject d = arr.optJSONObject(i);
                    if (d == null) continue;
                    list.put(vodFromSearch(d, scope[1]));
                    if (list.length() >= 40) break;
                }
                if (list.length() >= 40) break;
            }
            return new JSONObject().put("list", list).toString();
        } catch (Throwable t) {
            return "";
        }
    }

    @Override
    public String playerContent(String flag, String id, List<String> vipFlags) {
        try {
            if (TextUtils.isEmpty(id)) return "";
            String url = id;
            try {
                url = java.net.URLDecoder.decode(id, "UTF-8");
            } catch (Throwable ignored) {
            }
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
