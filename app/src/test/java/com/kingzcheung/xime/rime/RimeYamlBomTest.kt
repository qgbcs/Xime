package com.kingzcheung.xime.rime

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/** 部署前 BOM 归一化：外部编辑器写入的 UTF-8 BOM 会让 librime 解析失败。 */
class RimeYamlBomTest {

    @get:Rule
    val temp = TemporaryFolder()

    private val bom = byteArrayOf(0xEF.toByte(), 0xBB.toByte(), 0xBF.toByte())

    @Test
    fun `strips utf8 bom from user yaml`() {
        val file = File(temp.root, "default.custom.yaml")
        file.writeBytes(bom + "patch:\n  menu:\n".toByteArray())

        val fixed = RimeConfigHelper.stripBomFromUserYaml(temp.root)

        assertEquals(1, fixed)
        assertArrayEquals("patch:\n  menu:\n".toByteArray(), file.readBytes())
    }

    @Test
    fun `keeps yaml without bom untouched`() {
        val file = File(temp.root, "wanxiang.custom.yaml")
        val original = "patch:\n  key_binder:\n".toByteArray()
        file.writeBytes(original)

        val fixed = RimeConfigHelper.stripBomFromUserYaml(temp.root)

        assertEquals(0, fixed)
        assertArrayEquals(original, file.readBytes())
    }

    @Test
    fun `skips dict yaml and non yaml files`() {
        val dict = File(temp.root, "pinyin_simp.dict.yaml")
        dict.writeBytes(bom + "---\n".toByteArray())
        val txt = File(temp.root, "notes.txt")
        txt.writeBytes(bom + "x".toByteArray())

        val fixed = RimeConfigHelper.stripBomFromUserYaml(temp.root)

        assertEquals(0, fixed)
        assertArrayEquals(bom + "---\n".toByteArray(), dict.readBytes())
        assertArrayEquals(bom + "x".toByteArray(), txt.readBytes())
    }

    @Test
    fun `missing directory is a no-op`() {
        assertEquals(0, RimeConfigHelper.stripBomFromUserYaml(File(temp.root, "missing")))
    }
}
