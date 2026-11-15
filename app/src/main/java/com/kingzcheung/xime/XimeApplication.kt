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

    /** 主题配置只会解析一次：Activity 首帧回调与 2s 兜底可能同时触发。 */
    private val themeLoadRequested = java.util.concurrent.atomic.AtomicBoolean(false)

    /**
     * 异步加载 xime.yaml 的主题配置（89KB、30+ 色板，解析 1~2s）。
     * 必须在首帧之后调用：与 Compose 首次组合并行会把它拖慢约一倍。
     * 失败只降级为内置默认色板，绝不影响启动与 MQTT/HTTP RPC 主流程。
     */
    fun ensureBackgroundWarmup() {
        if (!themeLoadRequested.compareAndSet(false, true)) return
        applicationScope.launch {
            try {
                val themeId = KeysConfigHelper.loadDefaultThemeId(this@XimeApplication)
                val darkMode = KeysConfigHelper.loadDefaultDarkMode(this@XimeApplication)
                SettingsPreferences.defaultKeyboardTheme = themeId
                SettingsPreferences.defaultDarkMode = darkMode
                KeyboardThemes.initFromConfig(this@XimeApplication)
            } catch (t: Throwable) {
                com.kingzcheung.xime.util.FileLogger.w(
                    "XimeApp", "主题配置异步加载失败，沿用内置默认: ${t.javaClass.simpleName}"
                )
            } finally {
                KeyboardThemes.markConfigReady()
            }
            // 按设置显示或隐藏桌面图标（默认显示）：幂等的组件开关，一并放到后台
            try {
                LauncherIconController.syncFromPreference(this@XimeApplication)
            } catch (t: Throwable) {
                com.kingzcheung.xime.util.FileLogger.w(
                    "XimeApp", "LauncherIconController 同步失败: ${t.javaClass.simpleName}"
                )
            }
            // 词库部署/引擎预热放到首帧之后：首次安装要解压 15MB 词库并编译，
            // 与 Compose 首帧并行会把白屏拉长一倍。输入法服务自身仍会按需初始化。
            preInitializeRimeEngine()
        }
    }

    override fun onCreate() {
        super.onCreate()

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
            // GPS 事件监听：这里只保存 context，没有 GPS 事件订阅时完全不注册监听（零开销）。
            // 必须赶在 startPythonRpc() 之前：Python 启动后 task_manager 会立即同步
            // 订阅集合，那时 LocationEventMonitor 需要已有 context 才能拉起定位。
            // 单独 try/catch：初始化失败只打日志，绝不能阻断 app 启动与 MQTT/HTTP 主流程。
            try {
                com.kingzcheung.xime.task.LocationEventMonitor.init(this)
            } catch (t: Throwable) {
                com.kingzcheung.xime.util.FileLogger.w("XimeApp", "LocationEventMonitor.init 失败: ${t.javaClass.simpleName}")
            }
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
                FileLogger.flush()
            } catch (_: Exception) {
            }
            defaultHandler?.uncaughtException(thread, throwable)
        }

        val isDebug = BuildConfig.DEBUG
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
        PluginManager.quickSendHostApiFactory = { _ ->
            com.kingzcheung.xime.plugin.QuickSendHostApiImpl(this)
        }
        PluginManager.clipboardHostApiFactory = { _ ->
            com.kingzcheung.xime.plugin.ClipboardHostApiImpl(this)
        }
        PluginManager.cryptoHostApiFactory = {
            com.kingzcheung.xime.plugin.crypto.CryptoHostApiImpl()
        }
        PluginManager.initialize(this) {
            if (isDebug) {
                PluginManager.installPluginsFromAssetsForDebug("plugins")
            }
            
            PluginManager.loadEnabledPlugins()
        }
        
        ExtensionManager.initialize(this)

        // 主题配置（89KB xime.yaml，解析 1~2s）推迟到首帧之后再做：
        // 主线程在 Activity 首帧绘制完成后调 ensureThemeConfigLoad()；
        // 纯输入法进程没有 Activity，2s 后兜底启动，保证键盘也能拿到用户主题。
        android.os.Handler(android.os.Looper.getMainLooper()).postDelayed(
            { ensureBackgroundWarmup() }, 2000
        )

        // 初始化模型运行时（内存管理 + 生命周期）
        ModelRuntime.attach(this)

        // 词库部署/引擎预热已改到 ensureBackgroundWarmup() 里、首帧之后执行
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
        // :inference / :asr 也会走 Application.onCreate；子进程若也开引擎，会抢不到 userdb 的 LOCK
        // 并触发 RepairDB，把主进程正在写的 log 归档进 lost/，学习数据整段丢失
        if (!isMainProcess() || RimeEngine.isInitialized()) {
            return
        }
        
        applicationScope.launch {
            try {
                val (userDataDir, sharedDataDir) = RimeConfigHelper.initializeRimeDataAsync(this@XimeApplication)
                val engine = RimeEngine.getInstance()
                engine.initialize(userDataDir, sharedDataDir)

                // 首次安装/升级后静默编译词库。ensureDeployment 内部带互斥且 hash 一致时跳过，
                // 统一负责 deploymentDone/hash 状态，避免与输入法服务的初始化重复触发全量编译。
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