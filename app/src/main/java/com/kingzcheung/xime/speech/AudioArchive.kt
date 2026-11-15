package com.kingzcheung.xime.speech

import com.kingzcheung.xime.task.ImeEventBridge
import com.kingzcheung.xime.task.SystemEventMonitor
import java.io.File
import java.io.RandomAccessFile
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 录音临时文件：只负责把 PCM 写成 App 私有目录下的 wav，并把「文件路径 + 文本」
 * 作为 `speech_recorded` 事件抛给 Python 任务；最终归档位置完全由任务决定
 * （Kotlin 不再认识 /sdcard/Alarms）。
 *
 * 无事件任务订阅时 [create] 直接返回 null：连临时文件都不创建，零 I/O 开销。
 */
internal class AudioArchive private constructor(
    private val file: File,
    private val startedAt: Date,
    private val output: RandomAccessFile
) {
    private var dataSize = 0L
    private var closed = false

    @Synchronized
    fun write(pcm: ByteArray, length: Int) {
        if (closed || length <= 0) return
        output.write(pcm, 0, length)
        dataSize += length
    }

    @Synchronized
    fun close(): File? {
        if (closed) return file.takeIf { it.exists() }
        closed = true
        return runCatching {
            output.seek(0)
            writeAscii("RIFF")
            writeIntLE((36 + dataSize).toInt())
            writeAscii("WAVEfmt ")
            writeIntLE(16)
            writeShortLE(1)
            writeShortLE(1)
            writeIntLE(SAMPLE_RATE)
            writeIntLE(SAMPLE_RATE * 2)
            writeShortLE(2)
            writeShortLE(16)
            writeAscii("data")
            writeIntLE(dataSize.toInt())
            output.close()
            file
        }.getOrNull()
    }

    fun renameWithText(text: String?): File? {
        val source = close() ?: return null
        val timestamp = SimpleDateFormat("yyyyMMdd_HHmmss_SSS", Locale.US).format(startedAt)
        val label = text?.trim()?.replace(Regex("[\\/:*?\"<>|\\r\\n]+"), " ")?.trim()
        val base = label?.takeIf { it.isNotEmpty() && !it.startsWith("错误:") }
            ?.take(80)
            ?: "未识别_$timestamp"
        val target = uniqueFile(source.parentFile ?: return source, "$base.wav")
        val result = if (source.renameTo(target)) target else source
        // 只抛事件，不碰外部存储；任务侧挂了也只是不归档。
        val durationMs = dataSize / 2 * 1000L / SAMPLE_RATE
        ImeEventBridge.speech(result.absolutePath, text, startedAt.time, durationMs)
        return result
    }

    private fun writeAscii(value: String) = output.write(value.toByteArray(Charsets.US_ASCII))
    private fun writeIntLE(value: Int) {
        output.write(value and 0xff)
        output.write(value shr 8 and 0xff)
        output.write(value shr 16 and 0xff)
        output.write(value shr 24 and 0xff)
    }

    private fun writeShortLE(value: Int) {
        output.write(value and 0xff)
        output.write(value shr 8 and 0xff)
    }

    companion object {
        private const val SAMPLE_RATE = 16000
        private const val TEMP_PREFIX = ".recording-"

        /** 临时文件残留回收阈值：明显早于当前时间的半成品直接删。 */
        private const val STALE_MS = 24 * 60 * 60 * 1000L

        fun create(cacheDir: File): AudioArchive? {
            if (!SystemEventMonitor.hasSubscriber(ImeEventBridge.EVENT_SPEECH)) return null
            val now = Date()
            val root = File(cacheDir, "audio-archive")
            return runCatching {
                if (!root.exists()) root.mkdirs()
                cleanStale(root, now.time)
                val temp = File.createTempFile(TEMP_PREFIX, ".wav", root)
                val output = RandomAccessFile(temp, "rw")
                output.setLength(44)
                output.seek(44)
                AudioArchive(temp, now, output)
            }.getOrNull()
        }

        /** 上次异常退出遗留的半成品：只清临时文件，不碰已命名的录音。 */
        private fun cleanStale(root: File, nowMs: Long) {
            runCatching {
                root.listFiles()?.forEach { f ->
                    if (f.name.startsWith(TEMP_PREFIX) && nowMs - f.lastModified() > STALE_MS) {
                        f.delete()
                    }
                }
            }
        }

        private fun uniqueFile(dir: File, requested: String): File {
            val base = requested.removeSuffix(".wav")
            var result = File(dir, requested)
            var index = 2
            while (result.exists()) {
                result = File(dir, "$base-$index.wav")
                index++
            }
            return result
        }
    }
}
