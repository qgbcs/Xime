package com.kingzcheung.xime.task

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.Data
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.chaquo.python.Python
import com.kingzcheung.xime.util.FileLogger
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * WorkManager 触发入口：把执行转交 Python task_manager.execute_task。
 *
 * 调度模型：Python 侧 task_manager 用 OneTimeWorkRequest 自接力实现任意秒级周期
 * （PeriodicWorkRequest 最小 15 分钟不满足需求）。WorkManager 保证系统级可靠触发，
 * 进程死亡/重启后依然会被拉起执行。
 */
class PythonTaskWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result {
        val taskId = inputData.getString(KEY_TASK_ID) ?: return Result.failure()
        return try {
            if (!Python.isStarted()) {
                FileLogger.w(TAG, "Python 未启动，task=$taskId 稍后重试")
                return Result.retry()
            }
            val r = Python.getInstance()
                .getModule("task_manager")
                .callAttr("execute_task", taskId)
                .toString()
            FileLogger.i(TAG, "task=$taskId result=$r")
            Result.success()
        } catch (e: Throwable) {
            FileLogger.e(TAG, "task=$taskId 执行失败", e)
            Result.failure()
        }
    }

    companion object {
        const val KEY_TASK_ID = "task_id"
        private const val TAG = "PythonTaskWorker"
    }
}

/**
 * 精确闹钟入口：AlarmManager 到点后拉起本广播，再把执行转交 Python。
 *
 * 用于 Python 侧 backend=alarm 的短间隔任务（WorkManager 的最小延迟虽然能到秒级，
 * 但 Doze 下可能被推迟；精确闹钟在持有 SCHEDULE_EXACT_ALARM/USE_EXACT_ALARM 时
 * 可以 setExactAndAllowWhileIdle 按时唤醒）。进程未就绪时回退 WorkManager。
 */
class TaskAlarmReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val taskId = intent.getStringExtra(PythonTaskWorker.KEY_TASK_ID) ?: return
        try {
            if (!Python.isStarted()) {
                FileLogger.w(TAG, "alarm task=$taskId Python 未启动，回退 WorkManager")
                TaskScheduler.schedule(taskId, 0)
                return
            }
            Python.getInstance()
                .getModule("task_manager")
                .callAttr("execute_task", taskId)
            FileLogger.i(TAG, "alarm task=$taskId delivered")
        } catch (e: Throwable) {
            FileLogger.e(TAG, "alarm task=$taskId 执行失败", e)
        }
    }

    companion object {
        private const val TAG = "TaskAlarmReceiver"
    }
}

/**
 * Python 经 jclass 调用的静态调度桥。
 * 用唯一工作名 pytask_<id> + REPLACE，保证同 id 只有一个在排队。
 *
 * 三个入口全部幂等（同 id 覆盖）：
 *  - schedule：普通 WorkManager；
 *  - scheduleWithConstraints：带网络/充电/电量/存储/空闲约束；
 *  - scheduleExact：AlarmManager 精确闹钟（Doze 下尽量按时）。
 */
object TaskScheduler {

    private const val ACTION_ALARM = "com.kingzcheung.xime.task.ALARM"

    @JvmStatic
    fun schedule(taskId: String, delaySeconds: Int) {
        enqueue(taskId, delaySeconds, null)
    }

    @JvmStatic
    fun scheduleWithConstraints(taskId: String, delaySeconds: Int, constraintsJson: String) {
        enqueue(taskId, delaySeconds, parseConstraints(constraintsJson))
    }

    @JvmStatic
    fun scheduleExact(taskId: String, delaySeconds: Int) {
        val ctx = com.kingzcheung.xime.XimeApplication.instance
        val am = ctx.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val pi = alarmIntent(ctx, taskId)
        val triggerAt = System.currentTimeMillis() + delaySeconds.coerceAtLeast(0) * 1000L
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && !am.canScheduleExactAlarms()) {
                am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pi)
            } else {
                am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pi)
            }
        } catch (e: SecurityException) {
            FileLogger.w("TaskScheduler", "精确闹钟无权限，降级 setAndAllowWhileIdle: ${e.message}")
            am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pi)
        }
    }

    @JvmStatic
    fun cancelExact(taskId: String) {
        try {
            val ctx = com.kingzcheung.xime.XimeApplication.instance
            val am = ctx.getSystemService(Context.ALARM_SERVICE) as AlarmManager
            am.cancel(alarmIntent(ctx, taskId))
        } catch (e: Throwable) {
            FileLogger.w("TaskScheduler", "cancelExact $taskId 失败: ${e.message}")
        }
    }

    /** 开机完成：拉起一次 BootEventWorker，等 Python 初始化后由 init 补发事件。 */
    @JvmStatic
    fun scheduleBootEvent() {
        val ctx = com.kingzcheung.xime.XimeApplication.instance
        val req = OneTimeWorkRequestBuilder<BootEventWorker>().build()
        WorkManager.getInstance(ctx)
            .enqueueUniqueWork(BootEventWorker.WORK_NAME, ExistingWorkPolicy.REPLACE, req)
    }

    @JvmStatic
    fun cancel(taskId: String) {
        val ctx = com.kingzcheung.xime.XimeApplication.instance
        WorkManager.getInstance(ctx).cancelUniqueWork(workName(taskId))
    }

    private fun enqueue(taskId: String, delaySeconds: Int, constraints: Constraints?) {
        val ctx = com.kingzcheung.xime.XimeApplication.instance
        val data = Data.Builder()
            .putString(PythonTaskWorker.KEY_TASK_ID, taskId)
            .build()
        val builder = OneTimeWorkRequestBuilder<PythonTaskWorker>()
            .setInputData(data)
            .setInitialDelay(delaySeconds.coerceAtLeast(0).toLong(), TimeUnit.SECONDS)
        if (constraints != null) {
            builder.setConstraints(constraints)
        }
        WorkManager.getInstance(ctx)
            .enqueueUniqueWork(workName(taskId), ExistingWorkPolicy.REPLACE, builder.build())
    }

    private fun alarmIntent(ctx: Context, taskId: String): PendingIntent {
        val intent = Intent(ctx, TaskAlarmReceiver::class.java)
            .setAction(ACTION_ALARM)
            .putExtra(PythonTaskWorker.KEY_TASK_ID, taskId)
        return PendingIntent.getBroadcast(
            ctx,
            taskId.hashCode(),
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }

    /**
     * 解析 Python 侧下发的约束 JSON：
     * {"network":"connected|unmetered|not_roaming|metered|not_required",
     *  "charging":true,"battery_not_low":true,"storage_not_low":true,"device_idle":true}
     */
    private fun parseConstraints(json: String): Constraints? {
        if (json.isBlank()) return null
        return try {
            val o = JSONObject(json)
            val b = Constraints.Builder()
            var any = false
            when (o.optString("network", "").lowercase()) {
                "connected" -> { b.setRequiredNetworkType(NetworkType.CONNECTED); any = true }
                "unmetered" -> { b.setRequiredNetworkType(NetworkType.UNMETERED); any = true }
                "not_roaming" -> { b.setRequiredNetworkType(NetworkType.NOT_ROAMING); any = true }
                "metered" -> { b.setRequiredNetworkType(NetworkType.METERED); any = true }
                "not_required" -> { b.setRequiredNetworkType(NetworkType.NOT_REQUIRED); any = true }
            }
            if (o.optBoolean("charging", false)) { b.setRequiresCharging(true); any = true }
            if (o.optBoolean("battery_not_low", false)) { b.setRequiresBatteryNotLow(true); any = true }
            if (o.optBoolean("storage_not_low", false)) { b.setRequiresStorageNotLow(true); any = true }
            if (o.optBoolean("device_idle", false)) { b.setRequiresDeviceIdle(true); any = true }
            if (any) b.build() else null
        } catch (e: Throwable) {
            FileLogger.w("TaskScheduler", "约束解析失败: ${e.message}")
            null
        }
    }

    private fun workName(taskId: String) = "pytask_$taskId"
}
