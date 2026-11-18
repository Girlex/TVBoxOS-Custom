package com.github.catvod.crawler;

import android.text.TextUtils;

/**
 * 编译期内置的爬虫注册表：站点配置无需携带 jar，
 * 直接实例化 APK 内的 Spider 实现（与外部 jar 爬虫同协议）。
 */
public class BuiltinSpiders {

    public static Spider create(String api) {
        if (TextUtils.isEmpty(api)) return null;
        String name = api.contains("csp_") ? api.split("csp_")[1] : api;
        if ("Juzi".equalsIgnoreCase(name)) {
            try {
                return new com.github.catvod.spider.Juzi();
            } catch (Throwable ignored) {
            }
        }
        if ("Jianpian".equalsIgnoreCase(name)) {
            try {
                return new com.github.catvod.spider.Jianpian();
            } catch (Throwable ignored) {
            }
        }
        return null;
    }
}
