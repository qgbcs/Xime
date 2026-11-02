package com.kingzcheung.xime.scheme

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/** 归档探测/解压：跳过 app 与用户数据、剥壳目录、保留方案 id 与体积。 */
class SchemeArchiveTest {

    @get:Rule
    val temp = TemporaryFolder()

    private fun zip(name: String, entries: Map<String, String>): File {
        val file = File(temp.root, name)
        ZipOutputStream(file.outputStream()).use { zip ->
            entries.forEach { (path, content) ->
                zip.putNextEntry(ZipEntry(path))
                zip.write(content.toByteArray())
                zip.closeEntry()
            }
        }
        return file
    }

    @Test
    fun `app 自有文件与用户数据不随包导入`() {
        assertTrue(SchemeArchive.isSkippedEntry("xime.yaml"))
        assertTrue(SchemeArchive.isSkippedEntry("xime.custom.yaml"))
        assertTrue(SchemeArchive.isSkippedEntry("themes/bg.png"))
        assertTrue(SchemeArchive.isSkippedEntry("fonts/a.ttf"))
        assertTrue(SchemeArchive.isSkippedEntry("custom_phrase.txt"))
        assertTrue(SchemeArchive.isSkippedEntry("installation.yaml"))
        assertTrue(SchemeArchive.isSkippedEntry("user.yaml"))
        assertTrue(SchemeArchive.isSkippedEntry("sync/abc/x.userdb.txt"))
        assertTrue(SchemeArchive.isSkippedEntry("pinyin_simp.userdb/CURRENT"))
        assertTrue(SchemeArchive.isSkippedEntry(".registry.json"))
        assertTrue(SchemeArchive.isSkippedEntry(".manifests/pkg.json"))
    }

    @Test
    fun `方案内容随包导入`() {
        assertFalse(SchemeArchive.isSkippedEntry("default.yaml"))
        assertFalse(SchemeArchive.isSkippedEntry("default.custom.yaml"))
        assertFalse(SchemeArchive.isSkippedEntry("wubi86.schema.yaml"))
        assertFalse(SchemeArchive.isSkippedEntry("cn_dicts/base.dict.yaml"))
        assertFalse(SchemeArchive.isSkippedEntry("build/wubi86.prism.bin"))
        assertFalse(SchemeArchive.isSkippedEntry("opencc/t2s.json"))
        assertFalse(SchemeArchive.isSkippedEntry("lua/t9_preedit.lua"))
    }

    @Test
    fun `探测出方案 id 与解压后体积，忽略被跳过的条目`() {
        val archive = zip(
            "pkg.zip",
            mapOf(
                "rime-ice-master/rime_ice.schema.yaml" to "schema:\n  schema_id: rime_ice\n",
                "rime-ice-master/cn_dicts/base.dict.yaml" to "dict",
                "rime-ice-master/default.yaml" to "schema_list: []",
                "rime-ice-master/xime.custom.yaml" to "should be skipped",
                "__MACOSX/._rime_ice.schema.yaml" to "junk",
            ),
        )
        val probe = SchemeArchive.probe(archive)
        assertNotNull(probe)
        probe!!
        assertEquals(listOf("rime_ice"), probe.schemaIds)
        assertEquals("rime-ice-master/", probe.baseDir)
        // 用户数据（xime.custom.yaml）不再算"被丢弃"，而是解到 overlay 后应用
        assertEquals(4, probe.fileCount)
        assertTrue(probe.skippedEntries.isEmpty())
        assertTrue(probe.unpackedBytes > 0)

        val overlay = temp.newFolder("overlay")
        assertEquals(1, SchemeArchive.extractUserData(archive, overlay, probe.baseDir))
        assertTrue(File(overlay, "xime.custom.yaml").isFile)
    }

    @Test
    fun `解压剥掉壳目录并跳过用户数据`() {
        val archive = zip(
            "pkg.zip",
            mapOf(
                "rime-ice-master/rime_ice.schema.yaml" to "schema",
                "rime-ice-master/xime.custom.yaml" to "skip",
            ),
        )
        val probe = SchemeArchive.probe(archive)!!
        val target = temp.newFolder("rime")
        assertEquals(1, SchemeArchive.extract(archive, target, probe.baseDir))
        assertTrue(File(target, "rime_ice.schema.yaml").isFile)
        assertFalse(File(target, "xime.custom.yaml").exists())
        assertFalse(File(target, "rime-ice-master").exists())
    }

    @Test
    fun `损坏的压缩包返回 null`() {
        val broken = File(temp.root, "broken.zip")
        broken.writeText("not a zip")
        assertNull(SchemeArchive.probe(broken))
    }
}
