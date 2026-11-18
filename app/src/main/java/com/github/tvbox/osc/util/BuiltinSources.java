package com.github.tvbox.osc.util;

import android.content.Context;
import android.text.TextUtils;
import android.util.Log;

import com.google.gson.Gson;
import com.orhanobut.hawk.Hawk;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.Reader;
import java.util.ArrayList;

/**
 * 全本地内置源。
 *
 * <p>配置 JSON、爬虫 jar、直播 M3U 全部随 APK 打包在 {@code assets/builtin/}，
 * 客户端只访问 {@link #DEFAULT_API} 及 {@code clan://localhost/builtin/...}，
 * 由 RemoteServer 从本地文件返回，<b>外部配置地址失效也能正常加载</b>。
 *
 * <p>聚合入口里没有任何 http(s) 在线线路；线路顺序由
 * tools/optimize_offline_sources.py 多轮测速后按端到端耗时升序决定，第一条即默认线路。
 * 想更新快照：依次运行 snapshot_repo_sources.py、optimize_offline_sources.py、
 * build_offline_sources.py 再编译；也可把同名文件放到手机存储
 * {@code /sdcard/builtin/} 下覆盖，无需重新编译。
 */
public final class BuiltinSources {

    /** 内置聚合源入口，由 RemoteServer 映射到 assets/builtin/collection.json */
    public static final String DEFAULT_API = "clan://localhost/builtin.json";

    /** 默认直播地址，指向 assets/builtin/live/l1.txt（测速最快的一份 M3U） */
    public static final String DEFAULT_LIVE_API = "clan://localhost/builtin/live/l1.txt";

    /** 内置源总入口在线路列表里的显示名称 */
    public static final String SOURCE_TAG = "内置源";

    private static final String ASSET_ROOT = "builtin/";
    private static final String MANIFEST = ASSET_ROOT + "manifest.json";
    private static final String COLLECTION = ASSET_ROOT + "collection.json";
    private static final String TAG = "BuiltinSources";

    private static volatile Manifest sManifest;

    private BuiltinSources() {
    }

    public static class Source {
        public String name;
        public String origin;
        public String local;
        public String config;
        public String jar;
        public int sites;
        public double total_ms;
    }

    public static class Manifest {
        public int version;
        public String generated;
        public String default_live;
        public ArrayList<Source> sources;
    }

    /** 读取 assets/builtin/manifest.json（进程内缓存） */
    public static Manifest manifest(Context context) {
        if (sManifest == null) {
            synchronized (BuiltinSources.class) {
                if (sManifest == null) {
                    Manifest m = null;
                    Reader reader = null;
                    try {
                        reader = new InputStreamReader(
                                context.getApplicationContext().getAssets().open(MANIFEST), "UTF-8");
                        m = new Gson().fromJson(reader, Manifest.class);
                    } catch (Throwable th) {
                        Log.e(TAG, "read manifest: " + th.getMessage());
                    } finally {
                        if (reader != null) {
                            try {
                                reader.close();
                            } catch (Throwable ignored) {
                            }
                        }
                    }
                    if (m == null) m = new Manifest();
                    if (m.sources == null) m.sources = new ArrayList<>();
                    sManifest = m;
                }
            }
        }
        return sManifest;
    }

    /** 读取 assets 下的内置文件，找不到返回 null */
    public static byte[] assetBytes(Context context, String assetPath) {
        InputStream is = null;
        try {
            is = context.getApplicationContext().getAssets().open(assetPath);
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[16384];
            int n;
            while ((n = is.read(buf)) != -1) {
                bos.write(buf, 0, n);
            }
            return bos.toByteArray();
        } catch (Throwable th) {
            return null;
        } finally {
            if (is != null) {
                try {
                    is.close();
                } catch (Throwable ignored) {
                }
            }
        }
    }

    /** 聚合入口内容；RemoteServer 返回前会把 clan://localhost/ 换成真实本机地址 */
    public static String collection(Context context) {
        byte[] b = assetBytes(context, COLLECTION);
        if (b == null) return "{\"urls\":[]}";
        try {
            return new String(b, "UTF-8");
        } catch (Throwable th) {
            return "{\"urls\":[]}";
        }
    }

    /** 在 App.initParams() 中调用：预置默认源、历史与线路列表，去重且不覆盖用户当前选择 */
    public static void init(Context context) {
        Manifest m = manifest(context);

        String current = Hawk.get(HawkConfig.API_URL, "");
        if (TextUtils.isEmpty(current)) {
            current = DEFAULT_API;
            Hawk.put(HawkConfig.API_URL, current);
        }

        // 直播默认走本地 M3U；从旧版升级时把残留的在线直播地址换成本地文件
        String live = Hawk.get(HawkConfig.LIVE_API_URL, "");
        if (TextUtils.isEmpty(live) || live.startsWith("http")) {
            String localLive = TextUtils.isEmpty(m.default_live) ? DEFAULT_LIVE_API : m.default_live;
            String asset = localLive.replace("clan://localhost/builtin/", ASSET_ROOT);
            if (assetBytes(context, asset) == null) {
                localLive = DEFAULT_LIVE_API;
            }
            Hawk.put(HawkConfig.LIVE_API_URL, localLive);
        }

        ArrayList<String> history = Hawk.get(HawkConfig.API_HISTORY, new ArrayList<String>());
        ArrayList<String> mergedHistory = history == null
                ? new ArrayList<String>() : new ArrayList<String>(history);
        addHistory(mergedHistory, DEFAULT_API);
        for (Source s : m.sources) {
            addHistory(mergedHistory, s.local);
        }
        Hawk.put(HawkConfig.API_HISTORY, mergedHistory);

        ArrayList<String> lines = Hawk.get(HawkConfig.API_LINE_LIST, new ArrayList<String>());
        ArrayList<String> mergedLines = lines == null
                ? new ArrayList<String>() : purgeLegacyOnlineLines(lines);
        // 线路完全由 collection.json（每接口独立一线）在 loadConfig 时刷新，
        // 不再注入「内置源（全部线路）」聚合行，避免线路挤在一起。
        Hawk.put(HawkConfig.API_LINE_LIST, mergedLines);

        String source = Hawk.get(HawkConfig.API_LINE_SOURCE, "");
        if (TextUtils.isEmpty(source)) {
            Hawk.put(HawkConfig.API_LINE_SOURCE, DEFAULT_API);
        }
    }

    /** 清掉旧版注入的「在线·xxx」线路，用户自己添加的在线地址保持不动 */
    private static ArrayList<String> purgeLegacyOnlineLines(ArrayList<String> lines) {
        ArrayList<String> out = new ArrayList<>();
        for (String line : lines) {
            if (HistoryHelper.getApiLineName(line).startsWith("在线·")) continue;
            out.add(line);
        }
        return out;
    }

    private static void addHistory(ArrayList<String> history, String url) {
        if (!TextUtils.isEmpty(url) && !history.contains(url)) {
            history.add(url);
        }
    }

    private static void addLine(ArrayList<String> lines, String name, String url) {
        if (TextUtils.isEmpty(url)) return;
        for (String line : lines) {
            if (url.equals(HistoryHelper.getApiLineUrl(line))) return;
        }
        lines.add(HistoryHelper.buildApiLine(name, url));
    }
}
