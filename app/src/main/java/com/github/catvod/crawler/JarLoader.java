package com.github.catvod.crawler;

import android.content.Context;
import android.os.Environment;
import android.text.TextUtils;
import android.util.Log;

import com.github.catvod.net.OkHttp;
import com.github.tvbox.osc.base.App;
import com.github.tvbox.osc.server.ControlManager;
import com.github.tvbox.osc.server.RemoteServer;
import com.github.tvbox.osc.util.FileUtils;
import com.github.tvbox.osc.util.MD5;
import com.lzy.okgo.OkGo;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.lang.reflect.Method;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.Callable;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.FutureTask;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import dalvik.system.DexClassLoader;
import okhttp3.Response;

public class JarLoader {

    private static final String TAG = "JarLoader";
    private static final String MAIN_KEY = "main";

    // Android 7.x 的 ART 在多个线程同时校验同一个类时会命中
    // class_linker.cc 的 StatusVerifying -> StatusVerifying 断言并 native abort，
    // 应用层无法捕获。所有 DexClassLoader 的类首次加载/实例化必须串行通过这把锁。
    // 只锁微秒级的类加载动作，绝不把可能联网的 Init.init()/spider.init() 放进来。
    private static final Object DEX_CLASS_LOCK = new Object();
    // Init.init() 内部会同步联网拉取 xllib/ffmpeg 等 so，线上多为 404，
    // 单次可阻塞搜索线程 7 秒以上。最多等 3 秒，超时后让后台线程继续跑完。
    private static final long INIT_WAIT_SECONDS = 3L;

    private final ConcurrentHashMap<String, DexClassLoader> loaders = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<String, Method> proxyMethods = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<String, Method> danmuClickMethods = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<String, Method> danmuLongClickMethods = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<String, Spider> spiders = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<String, Object> locks = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<String, String> siteJarKeys = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<String, String> aliases = new ConcurrentHashMap<>();
    private final ProtectedInitJar protectedInitJar = new ProtectedInitJar();
    private volatile String recent = MAIN_KEY;

    public boolean load(String cache) {
        boolean success = load(MAIN_KEY, new File(cache));
        if (success) recent = MAIN_KEY;
        return success;
    }

    public void setRecentJarKey(String key) {
        if (TextUtils.isEmpty(key)) return;
        recent = realKey(key);
        injectProxyPort(loaders.get(recent));
    }

    public void loadLiveJar(String jar) {
        String key = jarKey(jar);
        parseJar(key, jar);
        setRecentJarKey(key);
    }

    public void clear() {
        for (Spider spider : spiders.values()) {
            try {
                spider.destroy();
            } catch (Throwable ignored) {
            }
        }
        loaders.clear();
        proxyMethods.clear();
        danmuClickMethods.clear();
        danmuLongClickMethods.clear();
        spiders.clear();
        locks.clear();
        siteJarKeys.clear();
        aliases.clear();
        protectedInitJar.clear();
        recent = MAIN_KEY;
    }

    private boolean load(String key, File file) {
        if (Thread.interrupted()) return false;
        if (!exists(file)) return false;
        if (loaders.containsKey(key)) return true;
        try {
            file.setReadOnly();
            String cachePath = jarDir().getAbsolutePath();
            DexClassLoader loader;
            synchronized (DEX_CLASS_LOCK) {
                if (loaders.containsKey(key)) return true;
                loader = new DexClassLoader(file.getAbsolutePath(), cachePath, cachePath,
                        App.getInstance().getClassLoader());
                // 在全局锁内完成核心类的首次加载与校验，后续并发 loadClass
                // 只会命中已校验完成的类，不会撞 ART 的类校验竞态。
                preloadClass(loader, "com.github.catvod.spider.Init");
                preloadClass(loader, "com.github.catvod.spider.Proxy");
                preloadClass(loader, "com.github.catvod.spider.Danmaku");
            }
            // 可能同步联网，放在锁外并限制等待时间
            if (!invokeInit(loader, file.getAbsolutePath())) {
                Log.i(TAG, "load error key=" + key + ", protected jar loader not bound");
                return false;
            }
            synchronized (DEX_CLASS_LOCK) {
                invokeProxy(key, loader);
                invokeDanmaku(key, loader);
                injectProxyPort(loader);
            }
            loaders.put(key, loader);
            Log.i(TAG, "load success key=" + key + ", file=" + file.getAbsolutePath());
            return true;
        } catch (Throwable e) {
            Log.i(TAG, "load error key=" + key + ", msg=" + e.getMessage());
            e.printStackTrace();
            return false;
        }
    }

    private static void preloadClass(DexClassLoader loader, String name) {
        try {
            loader.loadClass(name);
        } catch (Throwable ignored) {
        }
    }

    private boolean invokeInit(DexClassLoader loader, String jar) {
        boolean protectedJar = false;
        try {
            final Class<?> initClass = loader.loadClass("com.github.catvod.spider.Init");
            protectedJar = protectedInitJar.check(jar);
            if (protectedJar) {
                // 加密 jar 的绑定是纯反射操作，不联网，保持原有同步语义以免绑定不完整
                Log.i(TAG, "echo-load initProtectedJar file=" + jar);
                return protectedInitJar.init(initClass);
            }
            final Method method = initClass.getMethod("init", Context.class);
            Boolean done = runInitWithTimeout(new Callable<Boolean>() {
                @Override
                public Boolean call() {
                    try {
                        method.invoke(null, App.getInstance());
                        return true;
                    } catch (Throwable e) {
                        Log.i(TAG, "jar Init.init error=" + e.getMessage());
                        return false;
                    }
                }
            });
            if (Boolean.FALSE.equals(done)) {
                // 少数 jar 的初始化依赖调用线程的 Looper，回到原线程重试一次保证兼容
                try {
                    method.invoke(null, App.getInstance());
                } catch (Throwable ignored) {
                }
            }
        } catch (Throwable e) {
            e.printStackTrace();
        }
        return !protectedJar;
    }

    /** 返回 null 表示超时（后台线程仍会执行完），false 表示初始化抛异常。 */
    private static Boolean runInitWithTimeout(Callable<Boolean> body) {
        FutureTask<Boolean> task = new FutureTask<Boolean>(body);
        Thread worker = new Thread(task, "jar-init");
        worker.setDaemon(true);
        worker.setPriority(Thread.MIN_PRIORITY);
        worker.start();
        try {
            return task.get(INIT_WAIT_SECONDS, TimeUnit.SECONDS);
        } catch (TimeoutException e) {
            Log.i(TAG, "jar Init.init still running in background after "
                    + INIT_WAIT_SECONDS + "s, stop blocking caller");
            return null;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            task.cancel(true);
            return false;
        } catch (ExecutionException e) {
            return false;
        } catch (Throwable e) {
            return false;
        }
    }

    private void invokeProxy(String key, DexClassLoader loader) {
        try {
            Class<?> clz = loader.loadClass("com.github.catvod.spider.Proxy");
            Method method = clz.getMethod("proxy", Map.class);
            proxyMethods.put(key, method);
        } catch (Throwable e) {
            e.printStackTrace();
        }
    }

    private void invokeDanmaku(String key, DexClassLoader loader) {
        try {
            Class<?> clz = loader.loadClass("com.github.catvod.spider.Danmaku");
            try {
                danmuClickMethods.put(key, clz.getMethod("onClick", String.class, String.class));
            } catch (Throwable ignored) {
            }
            try {
                danmuLongClickMethods.put(key, clz.getMethod("onLongClick", String.class, String.class));
            } catch (Throwable ignored) {
            }
        } catch (Throwable ignored) {
        }
    }

    public void parseJar(String key, String jar) {
        if (TextUtils.isEmpty(key) || TextUtils.isEmpty(jar)) return;
        if (loaders.containsKey(key)) return;
        Object lock = lock(key);
        synchronized (lock) {
            if (loaders.containsKey(key)) return;
            String source = jar;
            String md5 = "";
            String[] texts = jar.split(";md5;");
            if (texts.length > 1) {
                source = texts[0];
                md5 = texts[1].trim();
            }
            aliases.put(jarKey(source), key);
            if (md5.startsWith("http")) {
                String value = OkHttp.string(md5, null);
                md5 = value == null ? "" : value.trim();
            }
            File file = fileForJar(source);
            if (!TextUtils.isEmpty(md5) && exists(file) && MD5.getFileMd5(file).equalsIgnoreCase(md5)) {
                load(key, file);
            } else if (TextUtils.isEmpty(md5) && exists(file) && !FileUtils.isWeekAgo(file)) {
                load(key, file);
            } else if (source.startsWith("http")) {
                load(key, download(source, file));
            } else if (source.startsWith("assets")) {
                load(key, copyAsset(source, file));
            } else if (source.startsWith("file")) {
                load(key, local(source));
            } else if (source.startsWith("clan://")) {
                load(key, download(clanToAddress(source), file));
            }
        }
    }

    public DexClassLoader dex(String jar) {
        try {
            String key = jarKey(jar);
            parseJar(key, jar);
            return loaders.get(key);
        } catch (Throwable e) {
            e.printStackTrace();
            return null;
        }
    }

    public Spider getSpider(String key, String api, String ext, String jar) {
        key = key == null ? "" : key;
        api = api == null ? "" : api;
        ext = ext == null ? "" : ext;
        jar = jar == null ? "" : jar;
        if (TextUtils.isEmpty(api)) return new SpiderNull();

        String jaKey = TextUtils.isEmpty(jar) ? MAIN_KEY : jarKey(jar);
        String spKey = jaKey + key;
        recent = jaKey;
        siteJarKeys.put(key, jaKey);
        injectProxyPort(loaders.get(jaKey));

        Spider cached = spiders.get(spKey);
        if (cached != null) {
            Log.i(TAG, "echo-getSpider cached key=" + spKey);
            return cached;
        }

        synchronized (lock("spider:" + spKey)) {
            cached = spiders.get(spKey);
            if (cached != null) return cached;
            try {
                if (!MAIN_KEY.equals(jaKey)) parseJar(jaKey, jar);
                DexClassLoader loader = loaders.get(jaKey);
                if (loader == null) return new SpiderNull();
                Spider spider;
                synchronized (DEX_CLASS_LOCK) {
                    spider = (Spider) loader.loadClass("com.github.catvod.spider." + className(api)).newInstance();
                }
                spider.siteKey = key;
                spider.initApi(new SpiderApi());
                spider.init(App.getInstance(), ext);
                spiders.put(spKey, spider);
                Log.i(TAG, "echo-getSpider success key=" + spKey);
                return spider;
            } catch (Throwable e) {
                Log.i(TAG, "echo-getSpider error key=" + spKey + ", msg=" + e.getMessage());
                e.printStackTrace();
                return new SpiderNull();
            }
        }
    }

    public void searchDanmuUi(String name, String episode, boolean longClick) {
        try {
            ConcurrentHashMap<String, Method> methods = longClick ? danmuLongClickMethods : danmuClickMethods;
            Method method = methods.get(recent);
            if (method == null) method = methods.get("main");
            if (method == null) return;
            method.invoke(null, name, episode);
        } catch (Throwable e) {
            e.printStackTrace();
        }
    }

    public boolean hasDanmuSearchUi() {
        return danmuClickMethods.containsKey(recent) || danmuLongClickMethods.containsKey(recent);
    }

    public JSONObject jsonExt(String key, LinkedHashMap<String, String> jxs, String url) {
        try {
            Class<?> clz = loadParserClass("com.github.catvod.parser.Json" + key);
            Method method = clz.getMethod("parse", LinkedHashMap.class, String.class);
            return (JSONObject) method.invoke(null, jxs, url);
        } catch (Throwable e) {
            e.printStackTrace();
            return null;
        }
    }

    public JSONObject jsonExtMix(String flag, String key, String name, LinkedHashMap<String, HashMap<String, String>> jxs, String url) {
        try {
            Class<?> clz = loadParserClass("com.github.catvod.parser.Mix" + key);
            Method method = clz.getMethod("parse", LinkedHashMap.class, String.class, String.class, String.class);
            return (JSONObject) method.invoke(null, jxs, name, flag, url);
        } catch (Throwable e) {
            e.printStackTrace();
            return null;
        }
    }

    public Object[] proxyInvoke(Map<String, String> params) {
        String siteKey = params == null ? null : params.get("siteKey");
        if (!TextUtils.isEmpty(siteKey)) {
            Object[] result = proxyInvoke(proxyMethods.get(siteJarKeys.get(siteKey)), params);
            if (result != null) return result;
        }
        Object[] result = proxyInvoke(proxyMethods.get(recent), params);
        if (result != null) return result;
        for (Map.Entry<String, Method> entry : proxyMethods.entrySet()) {
            if (entry.getKey().equals(recent)) continue;
            result = proxyInvoke(entry.getValue(), params);
            if (result != null) return result;
        }
        return null;
    }

    private Object[] proxyInvoke(Method method, Map<String, String> params) {
        try {
            return method == null ? null : (Object[]) method.invoke(null, params);
        } catch (Throwable e) {
            e.printStackTrace();
            return null;
        }
    }

    private DexClassLoader requireRecentLoader() {
        DexClassLoader loader = loaders.get(recent);
        if (loader == null) loader = loaders.get(MAIN_KEY);
        if (loader == null) throw new IllegalStateException("No jar loaded for recent key: " + recent);
        return loader;
    }

    private Class<?> loadParserClass(String name) throws ClassNotFoundException {
        synchronized (DEX_CLASS_LOCK) {
            DexClassLoader loader = loaders.get(recent);
            if (loader != null) {
                try {
                    return loader.loadClass(name);
                } catch (ClassNotFoundException ignored) {
                }
            }
            loader = loaders.get(MAIN_KEY);
            if (loader != null) return loader.loadClass(name);
            throw new ClassNotFoundException(name);
        }
    }

    private File download(String url, File file) {
        InputStream is = null;
        FileOutputStream os = null;
        try {
            Response response = OkGo.<File>get(url).execute();
            if (response.body() == null) return file;
            is = response.body().byteStream();
            os = new FileOutputStream(create(file));
            byte[] buffer = new byte[16384];
            int length;
            while ((length = is.read(buffer)) != -1) {
                if (Thread.interrupted()) return file;
                os.write(buffer, 0, length);
            }
            os.flush();
        } catch (Throwable e) {
            e.printStackTrace();
        } finally {
            close(is);
            close(os);
        }
        return file;
    }

    private File copyAsset(String url, File file) {
        InputStream is = null;
        FileOutputStream os = null;
        try {
            String path = url.replace("assets://", "").replace("assets/", "");
            is = App.getInstance().getAssets().open(path);
            os = new FileOutputStream(create(file));
            byte[] buffer = new byte[16384];
            int length;
            while ((length = is.read(buffer)) != -1) {
                os.write(buffer, 0, length);
            }
            os.flush();
        } catch (Throwable e) {
            e.printStackTrace();
        } finally {
            close(is);
            close(os);
        }
        return file;
    }

    private File local(String path) {
        path = path.replace("file:/", "");
        File file = new File(Environment.getExternalStorageDirectory(), path);
        return file.exists() ? file : new File(path);
    }

    private File fileForJar(String jar) {
        return new File(jarDir(), jarKey(jar) + ".jar");
    }

    private File jarDir() {
        File dir = new File(App.getInstance().getCacheDir(), "jar");
        if (!dir.exists()) dir.mkdirs();
        return dir;
    }

    private File create(File file) throws Exception {
        File parent = file.getParentFile();
        if (parent != null && !parent.exists()) parent.mkdirs();
        if (file.exists()) file.delete();
        file.createNewFile();
        file.setReadable(true);
        file.setWritable(true);
        file.setExecutable(true);
        return file;
    }

    private boolean exists(File file) {
        return file != null && file.exists() && file.length() > 0;
    }

    private Object lock(String key) {
        Object lock = locks.get(key);
        if (lock != null) return lock;
        Object created = new Object();
        Object old = locks.putIfAbsent(key, created);
        return old == null ? created : old;
    }

    private String jarKey(String jar) {
        String key = MD5.string2MD5(jar == null ? "" : jar);
        return TextUtils.isEmpty(key) ? MAIN_KEY : key;
    }

    private String realKey(String key) {
        String alias = aliases.get(key);
        return TextUtils.isEmpty(alias) ? key : alias;
    }

    private String className(String api) {
        return api.contains("csp_") ? api.split("csp_")[1] : api;
    }

    private String clanToAddress(String url) {
        if (url.startsWith("clan://localhost/")) {
            return url.replace("clan://localhost/", ControlManager.get().getAddress(true) + "file/");
        }
        if (url.startsWith("clan://")) {
            String text = url.substring(7);
            int index = text.indexOf('/');
            if (index > 0) return "http://" + text.substring(0, index) + "/file/" + text.substring(index + 1);
        }
        return url;
    }

    private void injectProxyPort(DexClassLoader loader) {
        com.github.catvod.Proxy.set(getServerPort());
        if (loader == null) return;
        synchronized (DEX_CLASS_LOCK) {
            try {
                Class<?> proxy = loader.loadClass("com.github.catvod.Proxy");
                Method set = proxy.getMethod("set", int.class);
                set.invoke(null, getServerPort());
            } catch (Throwable ignored) {
            }
        }
    }

    private int getServerPort() {
        try {
            String address = ControlManager.get().getAddress(true);
            if (address != null && address.startsWith("http://127.0.0.1:")) {
                String baseUrl = address.endsWith("/") ? address.substring(0, address.length() - 1) : address;
                return Integer.parseInt(baseUrl.substring(baseUrl.lastIndexOf(":") + 1));
            }
        } catch (Throwable ignored) {
        }
        return RemoteServer.serverPort;
    }

    private void close(java.io.Closeable closeable) {
        try {
            if (closeable != null) closeable.close();
        } catch (Throwable ignored) {
        }
    }
}
