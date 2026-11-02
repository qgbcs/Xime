package com.kingzcheung.xime.rime

import com.kingzcheung.xime.settings.SchemaManager
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * default.custom.yaml 所有权迁移（纯函数）：
 * - [SchemaManager.stripSchemaListBlock]：把 app 托管的 schema_list 块从用户文件
 *   剥离（迁往 prefs），块外内容与换行风格原样保留——此后该文件归用户，app 零写入，
 *   用户对 page_size/key_binder 等的修改不再被覆盖；
 * - [SchemaManager.replaceSchemaListBlock] 仍用于把 prefs 派生的启用列表写进
 *   default.yaml（applyEnabledSchemasToDefaultYaml，覆盖见 SchemaListBlockTest）。
 *
 * 每页候选数的最终生效在 JNI 层 setPageSize 直接注入引擎（rime_jni.cc）。
 */
class DefaultCustomYamlTest {

    /** 旧版模板形态：schema_list + menu 等基线节。 */
    private val legacyTemplate = """
        patch:
          schema_list:
            - schema: wubi86_pinyin
            - schema: wubi86
            - schema: pinyin_simp

          # Menu
          menu:
            page_size: 20
          switcher:
            caption: "〔方案选单〕"
    """.trimIndent()

    @Test
    fun `剥离schema_list并提取方案列表`() {
        val (stripped, schemas) = SchemaManager.stripSchemaListBlock(legacyTemplate)
        assertEquals(listOf("wubi86_pinyin", "wubi86", "pinyin_simp"), schemas)
        assertFalse("块整体移除", stripped.contains("schema_list"))
        assertFalse(stripped.contains("wubi86"))
    }

    @Test
    fun `剥离后块外内容原样保留`() {
        val (stripped, _) = SchemaManager.stripSchemaListBlock(legacyTemplate)
        assertTrue("patch 根保留", stripped.trimStart().startsWith("patch:"))
        assertTrue("menu 节保留", stripped.contains("page_size: 20"))
        assertTrue("switcher 节保留", stripped.contains("switcher:"))
        assertTrue("注释保留", stripped.contains("# Menu"))
        // 块原位置前的空行结构保持（patch: 与 menu 间不应塞入垃圾）
        assertFalse(stripped.contains("- schema"))
    }

    @Test
    fun `无schema_list块时原样返回`() {
        val noBlock = "patch:\n  menu:\n    page_size: 20\n"
        val (stripped, schemas) = SchemaManager.stripSchemaListBlock(noBlock)
        assertEquals(noBlock, stripped)
        assertTrue(schemas.isEmpty())
    }

    @Test
    fun `剥离只吃到首个非列表项`() {
        // schema_list 后紧跟其他键：只剥列表本身，后续键不受影响
        val text = "patch:\n  schema_list:\n    - schema: a\n    - schema: b\n  menu:\n    page_size: 9\n"
        val (stripped, schemas) = SchemaManager.stripSchemaListBlock(text)
        assertEquals(listOf("a", "b"), schemas)
        assertTrue(stripped.contains("menu:"))
        assertTrue(stripped.contains("page_size: 9"))
        assertFalse(stripped.contains("- schema:"))
    }

    @Test
    fun `文件末尾的块剥离后保留末尾换行`() {
        val text = "patch:\n  menu:\n    page_size: 20\n  schema_list:\n    - schema: a\n"
        val (stripped, schemas) = SchemaManager.stripSchemaListBlock(text)
        assertEquals(listOf("a"), schemas)
        assertTrue("末尾换行保留", stripped.endsWith("\n"))
        assertFalse(stripped.contains("schema_list"))
    }

    @Test
    fun `剥离保留CRLF风格`() {
        val crlf = "patch:\r\n  schema_list:\r\n    - schema: wubi86\r\n  menu:\r\n    page_size: 20\r\n"
        val (stripped, schemas) = SchemaManager.stripSchemaListBlock(crlf)
        assertEquals(listOf("wubi86"), schemas)
        assertTrue("CRLF 必须保留", stripped.contains("\r\n"))
        assertFalse("无裸 LF 残留", stripped.replace("\r\n", "").contains("\n"))
    }

    @Test
    fun `畸形列表项不产生方案id且被一并剥离`() {
        val text = "patch:\n  schema_list:\n    - schema: a\n    - 非法项\n  menu:\n    page_size: 20\n"
        val (stripped, schemas) = SchemaManager.stripSchemaListBlock(text)
        assertEquals(listOf("a"), schemas)
        assertFalse(stripped.contains("非法项"))
        assertTrue(stripped.contains("page_size: 20"))
    }
}
