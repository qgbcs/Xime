package com.kingzcheung.xime.task

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.chaquo.python.Python
import com.kingzcheung.xime.util.FileLogger
import kotlinx.coroutines.delay
import java.io.File

/**
 * 开机广播入口：BOOT_COMPLETED 时写入待发标记并排队一次后台工作，
 * 由 Python task_manager.init() 恢复完任务后补发 boot_completed 事件。
 *
 * 不在广播里直接投递事件：此时 Python 尚未启动、任务注册表也没加载，
 * 事件会因没有匹配任务而丢失。标记 + WorkManager 拉起进程可保证先后顺序。
 */
class BootEventReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        try {
            File(context.filesDir, PENDING_FILE)
                .writeText(System.currentTimeMillis().toString())
            FileLogger.i(TAG, "开机完成，已写入 boot 待发标记")
        } catch (t: Throwable) {
            FileLogger.e(TAG, "写 boot 待发标记失败", t)
        }
        TaskScheduler.scheduleBootEvent()
    }

    companion object {
        const val PENDING_FILE = "boot_pending"
        private const val TAG = "BootEventReceiver"
    }
}

/**
 * 保证进程存活到 Python 初始化完成，让 init() 有机会消费 boot 待发标记。
 * 初始化成功即结束；超时交给 WorkManager 重试。
 */
class BootEventWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result {
        repeat(WAIT_SECONDS) {
            if (Python.isStarted()) {
                try {
                    val ready = Python.getInstance()
                        .getModule("task_manager")
                        .callAttr("is_initialized")
                        .toString() == "True"
                    if (ready) {
                        // 幂等：若 init 已消费过标记则返回 fired=false
                        Python.getInstance()
                            .getModule("task_manager")
                            .callAttr("consume_boot_pending")
                        FileLogger.i(TAG, "Python task_manager 已初始化，boot 事件已投递")
                        return Result.success()
                    }
                } catch (_: Throwable) {
                    // task_manager 尚未导入完成，继续等待
                }
            }
            delay(1000)
        }
        FileLogger.w(TAG, "等待 Python 初始化超时，交由 WorkManager 重试")
        return Result.retry()
    }

    companion object {
        const val WORK_NAME = "pytask_boot_event"
        private const val WAIT_SECONDS = 60
        private const val TAG = "BootEventWorker"
    }
}
