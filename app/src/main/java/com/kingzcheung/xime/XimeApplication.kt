package com.kingzcheung.xime

import android.app.Application
import android.os.Build
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import coil.ImageLoader
import coil.ImageLoaderFactory
import coil.disk.DiskCache
import coil.memory.MemoryCache
import com.kingzcheung.xime.correction.CorrectionPriors
import com.kingzcheung.xime.correction.KeyTapLogger
import com.kingzcheung.xime.plugin.ExtensionManager
import com.kingzcheung.xime.plugin.PluginConfigStoreImpl
import com.kingzcheung.xime.util.FileLogger
import com.kingzcheung.xime.util.RpcUiController
import com.kingzcheung.xime.plugin.core.runtime.PluginManager
import com.kingzcheung.xime.rime.RimeConfigHelper
import com.kingzcheung.xime.model.ModelRuntime
import com.kingzcheung.xime.rime.RimeEngine
import com.kingzcheung.xime.settings.KeysConfigHelper
import com.kingzcheung.xime.ui.keyboard.AppFonts
import com.kingzcheung.xime.settings.SettingsPreferences
import com.kingzcheung.xime.ui.theme.KeyboardThemes
import com.kingzcheung.xime.util.LauncherIconController
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import java.io.File

class XimeApplication : Application(), ImageLoaderFactory {

    override fun newImageLoader(): ImageLoader {
        return ImageLoader.Builder(this)
            .memoryCache {
                MemoryCache.Builder(this)
                    .maxSizeBytes(16 * 1024 * 1024)
                    .build()
            }
            .diskCache {
                DiskCache.Builder()
                    .directory(cacheDir.resolve("coil_cache"))
                    .maxSizeBytes(32L * 1024 * 1024)
                    .build()
            }
            .build()
    }
    
    companion object {
        private const val TAG = "XimeApplication"

        /** 主进程 Application 单例，供 TaskScheduler 等无 Context 的静态桥使用。 */
        @JvmStatic
        lateinit var instance: XimeApplication
            private set
    }

    private val applicationScope = CoroutineScope(Dispatchers.IO)

    override fun onCreate() {
        super.onCreate()
        instance = this
        com.kingzcheung.xime.i18n.UiText.init(
            com.kingzcheung.xime.settings.AppLocale.wrap(this)
        )
        // 系统语言/区域变化时刷新全局取词 Context（界面由 Activity/IME 自行重建）
        registerComponentCallbacks(object : android.content.ComponentCallbacks {
            override fun onConfigurationChanged(newConfig: android.content.res.Configuration) {
                com.kingzcheung.xime.i18n.UiText.init(
                    com.kingzcheung.xime.settings.AppLocale.wrap(this@XimeApplication)
                )
            }

            override fun onLowMemory() = Unit
        })

        instance = this
        FileLogger.init(this)
        RpcUiController.initialize(this)
        // 进程名守卫：:inference / :asr 侧进程不引导 Python，
        // 避免侧进程抢 HTTP 端口并以同一 MQTT topic 成为第二个应答者。
        if (isMainProcess()) {
            // 网络边沿事件监听：先于 Python 启动注册，Python 未就绪期间事件自动排队补投
            com.kingzcheung.xime.task.NetworkEventMonitor.start(this)
            // 系统广播监听：默认只进 Kotlin 环形缓冲，仅订阅的事件才进入 Python
            com.kingzcheung.xime.task.SystemEventMonitor.start(this)
            // 最早期：外置设置镜像恢复（外置为准），必须在任何业务读取 kime_settings 之前
            com.kingzcheung.xime.storage.SettingsMirror.reconcile(this)
            startPythonRpc()
        } else {
            FileLogger.i(TAG, "Skip python rpc bootstrap in side process")
        }
        AppFonts.initialize(this)
        val defaultHandler = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            try {
                FileLogger.e("CrashHandler", "Uncaught exception on thread: ${thread.name}", throwable)
                FileLogger.flushNow()
            } catch (_: Exception) {
            }
            defaultHandler?.uncaughtException(thread, throwable)
        }

        val isDebug = BuildConfig.DEBUG
        if (isDebug) {
            // 插件 console 日志落盘（xipm dev/logs 轮询回显，规避 ROM 后台日志限流）
            com.kingzcheung.xime.plugin.PluginDevConsoleFileSink.install(this)
        }
        PluginManager.configStoreFactory =
            PluginManager.PluginConfigStoreFactory { app, pluginId ->
                PluginConfigStoreImpl(app, pluginId)
            }
        PluginManager.wsHostApiFactory = { pluginId ->
            com.kingzcheung.xime.plugin.ws.WsHostApiImpl(this, pluginId)
        }
        // 大文件句柄仓库（流式上传/下载用）：与 http 能力共享同一实例，
        // backup.push 登记的备份包句柄才能被 host.http.upload 解析到。
        val blobStore = com.kingzcheung.xime.plugin.http.PluginBlobStore(
            File(cacheDir, "plugin-blobs")
        )
        PluginManager.blobStoreFactory = { blobStore }
        PluginManager.httpHostApiFactory = { pluginId ->
            com.kingzcheung.xime.plugin.http.HttpHostApiImpl(this, pluginId, blobStore)
        }
        PluginManager.sseHostApiFactory = { pluginId ->
            com.kingzcheung.xime.plugin.http.SseHostApiImpl(this, pluginId)
        }
        PluginManager.cryptoHostApiFactory = {
            com.kingzcheung.xime.plugin.crypto.CryptoHostApiImpl()
        }
        PluginManager.quickSendHostApiFactory = { _ ->
            com.kingzcheung.xime.plugin.QuickSendHostApiImpl(this)
        }
        PluginManager.clipboardHostApiFactory = { _ ->
            com.kingzcheung.xime.plugin.ClipboardHostApiImpl(this)
        }
        com.kingzcheung.xime.plugin.core.security.PluginErrorLog.initialize(
            com.kingzcheung.xime.plugin.FilePluginErrorStore(
                File(filesDir, "logs/plugins/errors.jsonl")
            )
        )
        PluginManager.initialize(this) {
            if (isDebug) {
                PluginManager.installPluginsFromAssetsForDebug("plugins")
            }
            
            PluginManager.loadEnabledPlugins()
        }
        
        ExtensionManager.initialize(this)

        // 从 xime.yaml 加载配色方案
        KeyboardThemes.initFromConfig(this)

        // 从 xime.yaml 的 style 读取默认主题和显示模式
        // color_scheme 可指定 dynamic（Material You 动态配色，仅 Android 12+，
        // 低版本 getThemeById 自动回退到内置配色）
        SettingsPreferences.defaultKeyboardTheme = KeysConfigHelper.loadDefaultThemeId(this)
        SettingsPreferences.defaultDarkMode = KeysConfigHelper.loadDefaultDarkMode(this)

        // 按设置显示或隐藏桌面图标（默认显示）。
        LauncherIconController.syncFromPreference(this)

        // 初始化模型运行时（内存管理 + 生命周期）
        ModelRuntime.attach(this)

        preInitializeRimeEngine()
    }
    
    private fun isMainProcess(): Boolean {
        val processName = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            Application.getProcessName()
        } else {
            try {
                File("/proc/self/cmdline").inputStream().bufferedReader().readText()
                    .trim { it == '\u0000' || it.isWhitespace() }
            } catch (_: Exception) {
                ""
            }
        }
        return processName == packageName
    }

    private fun preInitializeRimeEngine() {
        if (RimeEngine.isInitialized()) {
            return
        }
        
        applicationScope.launch {
            try {
                val (userDataDir, sharedDataDir) = RimeConfigHelper.initializeRimeDataAsync(this@XimeApplication)
                val engine = RimeEngine.getInstance()
                engine.initialize(userDataDir, sharedDataDir)

                // 冷启动自检：每进程一次，无改动时立即返回；首次安装/产物缺失时
                // 内部自动升级为 fullCheck 维护。与输入法服务共享 deploymentLock，
                // 不会重复触发编译。
                RimeConfigHelper.ensureDeployment(this@XimeApplication)
            } catch (e: Exception) {
                Log.e(TAG, "Failed to pre-initialize Rime engine", e)
            }
        }
    }

    private fun startPythonRpc() {
        applicationScope.launch {
            try {
                if (!Python.isStarted()) {
                    Python.start(AndroidPlatform(this@XimeApplication))
                }
                val logPath = if (SettingsPreferences.isRpcLogToDiskEnabled(this@XimeApplication)) {
                    File(filesDir, "rpc.log").absolutePath
                } else {
                    null
                }
                Python.getInstance()
                    .getModule("app")
                    .callAttr("start", logPath, filesDir.absolutePath)
            } catch (e: Exception) {
                FileLogger.e(TAG, "Failed to start Python RPC", e)
            }
        }
    }
}