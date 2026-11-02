package com.kingzcheung.xime.settings

import android.content.Context
import android.content.res.AssetManager
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.mockito.kotlin.mock
import org.mockito.kotlin.whenever
import java.io.ByteArrayInputStream
import java.io.File
import java.io.IOException

/**
 * 回归测试（合并键布局重构引入，见 issue #884 关联的 xime.custom.yaml 不生效）：
 * [KeysConfigHelper.loadConfig] 刷新基线缓存后，必须把标准 26 键的行布局落到
 * `_zhRows`。历史上只调用 [KeysConfigHelper.setActiveKeyboardSchema]，而非合并键
 * 方案的 section 为 null，与重置后的 `_activeMergedSection` 相等会被提前 return，
 * 导致 xime.custom.yaml 的 layout.rows 不生效（重新部署也无效）。
 */
class KeysConfigLayoutReloadTest {

    @get:Rule
    val tmp = TemporaryFolder()

    private val defaultXime = """
        keyboard:
          qwerty:
            layout:
              rows:
                - [q, w, e, r, t, y, u, i, o, p]
                - [a, s, d, f, g, h, j, k, l]
                - [z, x, c, v, b, n, m]
    """.trimIndent()

    private val customXime = """
        keyboard:
          qwerty:
            layout:
              rows:
                - [q, w, e, r, t, y, u, i, o, p]
                - [a, s, d, f, g, h, j, k, l, ";"]
                - [z, x, c, v, b, n, m]
    """.trimIndent()

    @Test
    fun `重新部署时 xime custom 行布局生效`() {
        val filesDir = tmp.newFolder("files")
        val rimeDir = File(filesDir, "rime").apply { mkdirs() }
        File(rimeDir, "xime.custom.yaml").writeText(customXime)

        val assets = mock<AssetManager>()
        whenever(assets.open("xime.yaml")).thenAnswer {
            ByteArrayInputStream(defaultXime.toByteArray())
        }
        whenever(assets.open("xime.custom.yaml")).thenThrow(IOException("not in assets"))

        val context = mock<Context>()
        whenever(context.assets).thenReturn(assets)
        whenever(context.filesDir).thenReturn(filesDir)

        KeysConfigHelper.loadConfig(context)

        assertEquals(
            listOf("a", "s", "d", "f", "g", "h", "j", "k", "l", ";"),
            KeysConfigHelper.getRowsLayout(false).rows[1].keys,
        )
    }

    /**
     * 首帧兜底：配置完全读不到时用内置默认行渲染，第三行必须带 shift/delete。
     * 历史问题：默认行只有 3 行纯字母，首帧画出来的键盘第三行两端是空的，按一次键才补上。
     */
    @Test
    fun `配置缺失时兜底行第三行含 shift 与 delete`() {
        val filesDir = tmp.newFolder("files-fallback")

        val assets = mock<AssetManager>()
        whenever(assets.open(org.mockito.kotlin.any())).thenThrow(IOException("no assets"))

        val context = mock<Context>()
        whenever(context.assets).thenReturn(assets)
        whenever(context.filesDir).thenReturn(filesDir)

        KeysConfigHelper.loadConfig(context)

        for (isAsciiMode in listOf(false, true)) {
            val rows = KeysConfigHelper.getRowsLayout(isAsciiMode).rows
            assertEquals(3, rows.size)
            assertEquals("shift", rows[2].keys.first())
            assertEquals("delete", rows[2].keys.last())
        }
    }
}
