package com.github.tvbox.osc.util;

import com.orhanobut.hawk.Hawk;

import java.util.ArrayList;

/**
 * 内置配置源（2026-11-15 实测可用后合并）。
 *
 * <p>把 5 个可用配置地址合并成一个内置多仓源：默认配置地址 {@link #DEFAULT_API}
 * 指向 App 自带的本地服务，RemoteServer 会用 {@link #configJson()} 直接返回聚合配置，
 * 不依赖任何外部地址即可在「线路选择」里看到全部内置源。
 *
 * <p>每次启动都会把内置源合并进「历史配置」和「线路列表」，去重且不覆盖用户当前选择。
 */
public final class BuiltinSources {

    /** 内置聚合源入口，由 RemoteServer 的 /file/builtin.json 提供 */
    public static final String DEFAULT_API = "clan://localhost/builtin.json";

    /** 内置源总入口在线路列表里的显示名称 */
    public static final String SOURCE_TAG = "内置源";

    /** 实测可用源：{名称, 配置地址}；顺序即线路顺序，第一条为默认加载的源 */
    public static final String[][] SOURCES = {
            {"小马", "https://szyyds.cn/tv/x.json"},
            {"挺好分享多仓", "http://ztha.top/TVBox/GYCK.json"},
            {"老刘备", "https://raw.liucn.cc/box/m.json"},
            {"小盒子单仓", "http://xhztv.top/xhz"},
            {"拾光多仓", "http://xmbjm.fh4u.org/dc.txt"},
    };

    private BuiltinSources() {
    }

    /** 供 RemoteServer 返回的多仓配置 JSON，与 tools/builtin_sources.json 同源 */
    public static String configJson() {
        StringBuilder sb = new StringBuilder("{\"urls\":[");
        for (int i = 0; i < SOURCES.length; i++) {
            if (i > 0) sb.append(',');
            sb.append("{\"name\":\"").append(SOURCES[i][0])
                    .append("\",\"url\":\"").append(SOURCES[i][1]).append("\"}");
        }
        return sb.append("]}").toString();
    }

    /** 在 App.initParams() 中调用 */
    public static void init() {
        // 仅首次启动设置默认源，不覆盖用户已有配置
        String current = Hawk.get(HawkConfig.API_URL, "");
        if (current == null || current.isEmpty()) {
            current = DEFAULT_API;
            Hawk.put(HawkConfig.API_URL, current);
        }
        String live = Hawk.get(HawkConfig.LIVE_API_URL, "");
        if (live == null || live.isEmpty()) {
            Hawk.put(HawkConfig.LIVE_API_URL, current);
        }

        // 合并历史配置：保留用户已有记录，缺失的内置源追加在后面
        ArrayList<String> history = Hawk.get(HawkConfig.API_HISTORY, new ArrayList<String>());
        ArrayList<String> mergedHistory = history == null
                ? new ArrayList<String>() : new ArrayList<String>(history);
        for (String[] source : SOURCES) {
            if (!mergedHistory.contains(source[1])) mergedHistory.add(source[1]);
        }
        Hawk.put(HawkConfig.API_HISTORY, mergedHistory);

        // 合并线路列表：额外加一条「内置源（全部线路）」，方便随时回到总入口
        ArrayList<String> lines = Hawk.get(HawkConfig.API_LINE_LIST, new ArrayList<String>());
        ArrayList<String> mergedLines = lines == null
                ? new ArrayList<String>() : new ArrayList<String>(lines);
        addLine(mergedLines, SOURCE_TAG + "（全部线路）", DEFAULT_API);
        for (String[] source : SOURCES) {
            addLine(mergedLines, source[0], source[1]);
        }
        Hawk.put(HawkConfig.API_LINE_LIST, mergedLines);

        String source = Hawk.get(HawkConfig.API_LINE_SOURCE, "");
        if (source == null || source.isEmpty()) {
            Hawk.put(HawkConfig.API_LINE_SOURCE, DEFAULT_API);
        }
    }

    private static void addLine(ArrayList<String> lines, String name, String url) {
        for (String line : lines) {
            if (url.equals(HistoryHelper.getApiLineUrl(line))) return;
        }
        lines.add(HistoryHelper.buildApiLine(name, url));
    }
}
