package com.kingzcheung.xime.settings

import android.content.Context
import android.content.res.AssetManager
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.mockito.kotlin.mock
import org.mockito.kotlin.whenever
import java.io.ByteArrayInputStream
import java.io.File
import java.io.IOException

/**
 * 行级布局配置测试：`keyboard.<section>.layout` 的 rows 对象形式（align/width）、
 * columns、row_align 的解析与装载（缺省 uniform、custom 段级覆盖）。
 */
class KeyboardRowsLayoutConfigTest {

    @get:Rule
    val tmp = TemporaryFolder()

    private val defaultXime = """
        keyboard:
          qwerty:
            layout:
              row_align: uniform
              rows:
                - [q, w, e, r, t, y, u, i, o, p]
                - [a, s, d, f, g, h, j, k, l]
                - [z, x, c, v, b, n, m]
          qwerty_en:
            layout:
              rows:
                - [q, w, e, r, t, y, u, i, o, p]
                - [a, s, d, f, g, h, j, k, l]
                - [z, x, c, v, b, n, m]
    """.trimIndent()

    // ── 纯文本解析 ──

    @Test
    fun `数组行解析为无行级覆盖的行`() {
        val rows = KeysConfigHelper.parseKeyboardRowsYamlText(defaultXime, "qwerty")!!
        assertEquals(3, rows.size)
        assertEquals(listOf("q", "w", "e", "r", "t", "y", "u", "i", "o", "p"), rows[0].keys)
        assertNull(rows[0].align)
        assertNull(rows[0].width)
    }

    @Test
    fun `对象行的 keys 支持合并键嵌套`() {
        val text = """
            keyboard:
              qwerty_14:
                layout:
                  rows:
                    - [[q, w], [e, r], [t, y]]
                    - { keys: [[a, s], [d, f], [g, h]], align: stretch }
                    - { keys: "z, [x, c], v" }
        """.trimIndent()
        val rows = KeysConfigHelper.parseKeyboardRowsYamlText(text, "qwerty_14")!!
        // 嵌套子数组 → 合并键 ID（组内字母拼接）
        assertEquals(listOf("qw", "er", "ty"), rows[0].keys)
        assertEquals(listOf("as", "df", "gh"), rows[1].keys)
        assertEquals(RowAlign.STRETCH, rows[1].align)
        // 标量兜底写法同样支持方括号合并键
        assertEquals(listOf("z", "xc", "v"), rows[2].keys)
    }

    @Test
    fun `对象行解析 align 与 width`() {
        val text = """
            keyboard:
              qwerty:
                layout:
                  rows:
                    - [q, w, e, r, t, y, u, i, o, p]
                    - { keys: [a, s, d, f, g, h, j, k, l], align: stretch }
                    - { keys: [z, x, c, v, b, n, m], width: 0.7 }
        """.trimIndent()
        val rows = KeysConfigHelper.parseKeyboardRowsYamlText(text, "qwerty")!!
        assertEquals(RowAlign.STRETCH, rows[1].align)
        assertEquals(9, rows[1].keys.size)
        assertEquals(0.7f, rows[2].width!!, 0.0001f)
        assertNull(rows[2].align)
    }

    @Test
    fun `对象行的 align 非法值忽略、width 夹取到 0 到 1`() {
        val text = """
            keyboard:
              qwerty:
                layout:
                  rows:
                    - { keys: [a, b], align: centre }
                    - { keys: [c, d], width: 3.5 }
        """.trimIndent()
        val rows = KeysConfigHelper.parseKeyboardRowsYamlText(text, "qwerty")!!
        assertNull(rows[0].align)
        assertEquals(1f, rows[1].width!!, 0.0001f)
    }

    @Test
    fun `对象行缺 keys 时被丢弃`() {
        val text = """
            keyboard:
              qwerty:
                layout:
                  rows:
                    - { align: stretch }
                    - [a, b]
        """.trimIndent()
        val rows = KeysConfigHelper.parseKeyboardRowsYamlText(text, "qwerty")!!
        assertEquals(1, rows.size)
        assertEquals(listOf("a", "b"), rows[0].keys)
    }

    @Test
    fun `columns 与 row_align 解析`() {
        val text = """
            keyboard:
              qwerty:
                layout:
                  columns: 12
                  row_align: stretch
        """.trimIndent()
        assertEquals(12, KeysConfigHelper.parseKeyboardColumnsYamlText(text, "qwerty"))
        assertEquals(RowAlign.STRETCH, KeysConfigHelper.parseKeyboardRowAlignYamlText(text, "qwerty"))
        assertNull(KeysConfigHelper.parseKeyboardColumnsYamlText(defaultXime, "qwerty"))
        assertNull(KeysConfigHelper.parseKeyboardRowAlignYamlText(defaultXime, "qwerty_en"))
    }

    @Test
    fun `columns 非正数与 row_align 非法值按未配置处理`() {
        val text = """
            keyboard:
              qwerty:
                layout:
                  columns: 0
                  row_align: mid
        """.trimIndent()
        assertNull(KeysConfigHelper.parseKeyboardColumnsYamlText(text, "qwerty"))
        assertNull(KeysConfigHelper.parseKeyboardRowAlignYamlText(text, "qwerty"))
    }

    @Test
    fun `内置合并键布局声明 stretch 以保持各行铺满`() {
        val yaml = repoFile("src/main/assets/xime.yaml").readText()
        for (section in listOf("qwerty_14", "qwerty_17", "qwerty_18")) {
            assertEquals(
                "$section 应显式 stretch（否则短行会按列居中）",
                RowAlign.STRETCH,
                KeysConfigHelper.parseKeyboardRowAlignYamlText(yaml, section),
            )
        }
        // 标准 26 键缺省 uniform：第二行九键按列居中，与历史观感一致
        assertNull(KeysConfigHelper.parseKeyboardRowAlignYamlText(yaml, "qwerty"))
        assertNull(KeysConfigHelper.parseKeyboardRowAlignYamlText(yaml, "qwerty_en"))
    }

    private fun repoFile(rel: String): File {
        var dir: File? = File(System.getProperty("user.dir")).absoluteFile
        while (dir != null) {
            File(dir, rel).takeIf { it.exists() }?.let { return it }
            File(dir, "app/$rel").takeIf { it.exists() }?.let { return it }
            dir = dir.parentFile
        }
        error("file not found: $rel")
    }

    // ── 装载链路（custom 段级覆盖内置，中英文各自生效）──

    @Test
    fun `custom 配置只覆盖对应键盘的行级声明`() {
        val filesDir = tmp.newFolder("files")
        val rimeDir = File(filesDir, "rime").apply { mkdirs() }
        val customFile = File(rimeDir, "xime.custom.yaml")
        customFile.writeText(
            """
            keyboard:
              qwerty:
                layout:
                  columns: 11
                  row_align: stretch
                  rows:
                    - [q, w, e, r, t, y, u, i, o, p]
                    - { keys: [a, s, d, f, g, h, j, k, l], width: 0.8 }
            """.trimIndent()
        )
        // 单例缓存戳按 mtime+size 早退：推到未来，保证本用例一定重新装载
        customFile.setLastModified(System.currentTimeMillis() + 10_000)

        val assets = mock<AssetManager>()
        whenever(assets.open("xime.yaml")).thenAnswer {
            ByteArrayInputStream(defaultXime.toByteArray())
        }
        whenever(assets.open("xime.custom.yaml")).thenThrow(IOException("not in assets"))

        val context = mock<Context>()
        whenever(context.assets).thenReturn(assets)
        whenever(context.filesDir).thenReturn(filesDir)

        KeysConfigHelper.loadConfig(context)

        val zh = KeysConfigHelper.getRowsLayout(false)
        assertEquals(11, zh.columns)
        assertEquals(RowAlign.STRETCH, zh.rowAlign)
        assertEquals(0.8f, zh.rows[1].width!!, 0.0001f)

        // 英文段未配置 columns/row_align → 缺省：列数取首行键数、uniform
        val en = KeysConfigHelper.getRowsLayout(true)
        assertNull(en.columns)
        assertEquals(RowAlign.UNIFORM, en.rowAlign)
        assertNull(en.rows[0].width)
    }
}
