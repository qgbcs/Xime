package com.kingzcheung.xime.scheme

import com.kingzcheung.xime.settings.SchemaManager
import com.kingzcheung.xime.util.FileLogger
import org.apache.commons.compress.archivers.tar.TarArchiveInputStream
import org.apache.commons.compress.compressors.gzip.GzipCompressorInputStream
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.util.zip.ZipFile

/**
 * 方案包归档的探测与解压。
 *
 * 探测只读元数据（方案 id、解压后体积、被丢弃的条目、壳目录），解压按需流式写入，
 * 不把归档内容读进内存 —— 方案包常有几十 MB 词典。
 *
 * 解压规则：剥掉 GitHub 归档那层壳目录、跳过 Apple Double、拒绝越界路径，
 * 丢弃属于 app/用户/librime 运行时的条目（见 [isSkippedEntry]），保留归档内 mtime
 * （librime 靠它判断包内预编译产物是否过期）。
 */
internal object SchemeArchive {

    private const val TAG = "SchemeArchive"

    data class Probe(
        /** 归档里顶层 `<id>.schema.yaml` 的方案 id。 */
        val schemaIds: List<String>,
        /** 解压后总字节数（空间预检用）。 */
        val unpackedBytes: Long,
        /** 归档里出现、但会被丢弃的条目（提示包作者塞了不该塞的文件）。 */
        val skippedEntries: List<String>,
        val fileCount: Int,
        /** 需要剥掉的壳目录前缀（可能为空串）。 */
        val baseDir: String,
    )

    fun isArchive(name: String): Boolean =
        name.endsWith(".zip", ignoreCase = true) ||
            name.endsWith(".tar.gz", ignoreCase = true) ||
            name.endsWith(".tgz", ignoreCase = true)

    /** @return null 表示归档损坏或格式不支持。 */
    fun probe(archive: File): Probe? {
        val entries = mutableListOf<Triple<String, Long, Long>>() // path, size, time
        val ok = visit(archive) { path, size, time, _ ->
            entries += Triple(path, size, time)
        }
        if (!ok) return null

        val baseDir = SchemaManager.findSchemaBaseDir(entries.map { it.first })
        var bytes = 0L
        var count = 0
        val skipped = mutableListOf<String>()
        for ((path, size, _) in entries) {
            val rel = path.removePrefix(baseDir)
            if (rel.isBlank()) continue
            if (SchemeFiles.isUserData(rel)) {
                // 用户数据层会解到 overlay 后应用，不按"被丢弃条目"警告
                bytes += size.coerceAtLeast(0)
                count++
                continue
            }
            if (isSkippedEntry(rel)) {
                skipped += rel
                continue
            }
            bytes += size.coerceAtLeast(0)
            count++
        }
        return Probe(
            schemaIds = entries.map { it.first.removePrefix(baseDir) }
                .filter { it.endsWith(".schema.yaml") && !it.contains('/') }
                .map { it.removeSuffix(".schema.yaml") }
                .distinct()
                .sorted(),
            unpackedBytes = bytes,
            skippedEntries = skipped.distinct(),
            fileCount = count,
            baseDir = baseDir,
        )
    }

    /**
     * 解压到 [targetDir]；返回解压文件数，-1 表示失败。
     *
     * @param baseDir [Probe.baseDir]，避免再扫一遍归档找壳目录。
     */
    fun extract(archive: File, targetDir: File, baseDir: String): Int {
        var extracted = 0
        val ok = visit(archive) { path, _, time, write ->
            val rel = path.removePrefix(baseDir)
            if (rel.isBlank() || isSkippedEntry(rel)) return@visit
            val target = safeChild(targetDir, rel)
            if (target == null) {
                FileLogger.w(TAG, "skip unsafe path: $rel")
                return@visit
            }
            target.parentFile?.mkdirs()
            write(target)
            if (time > 0L) target.setLastModified(time)
            extracted++
        }
        if (!ok) return -1
        FileLogger.i(TAG, "extract ${archive.name}: $extracted files")
        return extracted
    }

    /**
     * 把包内的用户数据层（xime.custom.yaml / themes / fonts / custom_phrase）解到
     * [targetDir]（通常是 `market/<pkg>/overlay`）。安装流程会先恢复旧用户数据，
     * 再通过 overlay 覆盖，从而既保留旧主题/字体，又让新包自带的布局生效。
     *
     * @return 解出文件数，-1 表示归档读失败。
     */
    fun extractUserData(archive: File, targetDir: File, baseDir: String): Int {
        var extracted = 0
        val ok = visit(archive) { path, _, time, write ->
            val rel = path.removePrefix(baseDir)
            if (rel.isBlank() || !SchemeFiles.isUserData(rel)) return@visit
            val target = safeChild(targetDir, rel)
            if (target == null) {
                FileLogger.w(TAG, "skip unsafe user-data path: $rel")
                return@visit
            }
            target.parentFile?.mkdirs()
            write(target)
            if (time > 0L) target.setLastModified(time)
            extracted++
        }
        return if (ok) extracted else -1
    }

    /**
     * 不属于包内容的条目：
     * - app 自有配置（`xime.yaml`）与旧版归属表（`.registry` / `.manifests` / `.package-presets`）；
     * - librime 运行时（`installation.yaml` / `user.yaml` / `sync/` / `*.userdb*`）；
     * - 用户数据白名单（`xime.custom.yaml` / `themes/` / `fonts/` / `custom_phrase*.txt`）。
     */
    internal fun isSkippedEntry(path: String): Boolean {
        val p = path.replace('\\', '/').trimStart('/')
        if (p == "xime.yaml" || p == "installation.yaml" || p == "user.yaml") return true
        if (p.startsWith(".registry") || p.startsWith(".manifests") || p.startsWith(".package-presets")) return true
        if (p.startsWith("sync/")) return true
        if (p.contains(".userdb/") || p.endsWith(".userdb") || p.endsWith(".userdb.txt")) return true
        return SchemeFiles.isUserData(p)
    }

    /**
     * 流式遍历归档：回调里拿到条目元数据与"把该条目写到某文件"的动作。
     * 回调返回后条目内容即失效，所以写盘必须发生在回调内。
     *
     * @return false 表示归档读不了。
     */
    private fun visit(
        archive: File,
        onEntry: (path: String, size: Long, time: Long, writeTo: (File) -> Unit) -> Unit,
    ): Boolean {
        val name = archive.name
        return try {
            when {
                name.endsWith(".zip", ignoreCase = true) -> visitZip(archive, onEntry)
                name.endsWith(".tar.gz", ignoreCase = true) || name.endsWith(".tgz", ignoreCase = true) ->
                    visitTar(archive, onEntry)
                else -> false
            }
        } catch (e: Exception) {
            FileLogger.e(TAG, "visit failed: ${archive.name}", e)
            false
        }
    }

    private fun visitZip(
        archive: File,
        onEntry: (String, Long, Long, (File) -> Unit) -> Unit,
    ): Boolean {
        ZipFile(archive).use { zip ->
            for (entry in zip.entries()) {
                if (entry.isDirectory || isAppleDouble(entry.name)) continue
                onEntry(entry.name, entry.size, entry.time) { target ->
                    zip.getInputStream(entry).use { input -> writeFile(target, input) }
                }
            }
        }
        return true
    }

    private fun visitTar(
        archive: File,
        onEntry: (String, Long, Long, (File) -> Unit) -> Unit,
    ): Boolean {
        archive.inputStream().buffered().use { input ->
            TarArchiveInputStream(GzipCompressorInputStream(input)).use { tar ->
                var entry = tar.nextEntry
                while (entry != null) {
                    if (!entry.isDirectory && !isAppleDouble(entry.name)) {
                        // tar 只能顺序读：回调内直接写盘，不把内容留在内存里。
                        onEntry(entry.name, entry.size, entry.modTime?.time ?: -1L) { target ->
                            writeFile(target, tar)
                        }
                    }
                    entry = tar.nextEntry
                }
            }
        }
        return true
    }

    private fun writeFile(target: File, input: InputStream) {
        FileOutputStream(target).use { output -> input.copyTo(output) }
    }

    private fun isAppleDouble(name: String): Boolean =
        name.startsWith("__MACOSX/") || name.contains("/._") || name.startsWith("._")

    private fun safeChild(targetDir: File, name: String): File? {
        val child = File(targetDir, name)
        return if (child.canonicalPath.startsWith(targetDir.canonicalPath + File.separator)) child else null
    }
}
