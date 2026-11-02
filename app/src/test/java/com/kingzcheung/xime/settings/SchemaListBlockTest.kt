package com.kingzcheung.xime.settings

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Tests for the pure [SchemaManager.replaceSchemaListBlock] used by F1
 * (writing the enabled schema list directly into default.yaml so librime
 * actually compiles the enabled schemes).
 */
class SchemaListBlockTest {

    private val defaultYaml = """
        # Rime default settings
        # encoding: utf-8

        config_version: "0.36"

        schema_list:
          - schema: wubi86
          - schema: wubi86_pinyin
          - schema: pinyin_simp

        switcher:
          caption: "〔方案选单〕"
    """.trimIndent()

    /** Pull the schema ids out of the schema_list block of a yaml string. */
    private fun extractSchemaList(yaml: String): List<String> {
        val out = mutableListOf<String>()
        var inBlock = false
        for (line in yaml.lines()) {
            if (line.trim() == "schema_list:") { inBlock = true; continue }
            if (inBlock) {
                val t = line.trim()
                if (t.startsWith("- schema:")) {
                    out.add(t.removePrefix("- schema:").trim())
                } else if (t.isNotEmpty()) {
                    break
                }
            }
        }
        return out
    }

    @Test
    fun `replaces existing block with a single enabled schema`() {        val result = SchemaManager.replaceSchemaListBlock(defaultYaml, listOf("quick5"))
        assertEquals(listOf("quick5"), extractSchemaList(result))
        assertFalse("old schemas must be gone", result.contains("wubi86"))
    }

    @Test
    fun `replaces existing block with multiple enabled schemas in order`() {
        val result = SchemaManager.replaceSchemaListBlock(
            defaultYaml, listOf("cangjie5", "quick5", "pinyin_simp")
        )
        assertEquals(listOf("cangjie5", "quick5", "pinyin_simp"), extractSchemaList(result))
    }

    @Test
    fun `preserves content before and after the block`() {
        val result = SchemaManager.replaceSchemaListBlock(defaultYaml, listOf("quick5"))
        assertTrue("config_version kept", result.contains("""config_version: "0.36""""))
        assertTrue("switcher block kept", result.contains("switcher:"))
        assertTrue("nested switcher content kept", result.contains("〔方案选单〕"))
        assertTrue("leading comment kept", result.contains("# Rime default settings"))
    }

    @Test
    fun `keeps the two-space indentation style of list items`() {
        val result = SchemaManager.replaceSchemaListBlock(defaultYaml, listOf("quick5"))
        assertTrue(result.contains("\n  - schema: quick5"))
    }

    @Test
    fun `handles a schema_list block at end of file`() {
        val atEof = "config_version: \"0.36\"\n\nschema_list:\n  - schema: wubi86\n"
        val result = SchemaManager.replaceSchemaListBlock(atEof, listOf("quick5"))
        assertEquals(listOf("quick5"), extractSchemaList(result))
        assertTrue(result.contains("config_version"))
    }

    @Test
    fun `empty enabled list clears schema_list`() {
        val result = SchemaManager.replaceSchemaListBlock(defaultYaml, emptyList())
        assertEquals(emptyList<String>(), extractSchemaList(result))
        assertTrue(result.contains("schema_list: []"))
        assertTrue(result.contains("config_version"))
    }

    @Test
    fun `empty then non-empty keeps a single schema_list block`() {
        val cleared = SchemaManager.replaceSchemaListBlock(defaultYaml, emptyList())
        val result = SchemaManager.replaceSchemaListBlock(cleared, listOf("wubi86"))
        assertEquals(1, Regex("(?m)^schema_list:").findAll(result).count())
        assertEquals(listOf("wubi86"), extractSchemaList(result))
    }

    @Test
    fun `writing empty twice stays a single empty block`() {
        val once = SchemaManager.replaceSchemaListBlock(defaultYaml, emptyList())
        val twice = SchemaManager.replaceSchemaListBlock(once, emptyList())
        assertEquals(1, Regex("(?m)^schema_list:").findAll(twice).count())
        assertEquals(emptyList<String>(), extractSchemaList(twice))
    }

    @Test
    fun `dedupes duplicate schema_list blocks from earlier writes`() {
        val duplicated = defaultYaml + "\n\nschema_list:\n  - schema: quick5\n"
        val result = SchemaManager.replaceSchemaListBlock(duplicated, listOf("wubi86"))
        assertEquals(1, Regex("(?m)^schema_list:").findAll(result).count())
        assertEquals(listOf("wubi86"), extractSchemaList(result))
        assertTrue(result.contains("switcher:"))
    }

    @Test
    fun `preserves CRLF line endings`() {
        val crlf = "config_version: \"0.36\"\r\n\r\nschema_list:\r\n  - schema: wubi86\r\n\r\nswitcher:\r\n"
        val result = SchemaManager.replaceSchemaListBlock(crlf, listOf("quick5"))
        assertTrue("CRLF must be preserved", result.contains("\r\n"))
        // 去掉所有 CRLF 后不应残留任何裸 LF（即没有被规范化成 LF 的行）
        assertFalse("no lone LF introduced", result.replace("\r\n", "").contains("\n"))
        assertEquals(listOf("quick5"), extractSchemaList(result))
    }

    @Test
    fun `inserts a block when none exists`() {
        val noBlock = "config_version: \"0.36\"\n\nswitcher:\n  caption: x\n"
        val result = SchemaManager.replaceSchemaListBlock(noBlock, listOf("quick5"))
        assertEquals(listOf("quick5"), extractSchemaList(result))
        assertTrue(result.contains("switcher:"))
    }

    // ---- parseSchemaListBlock：读取方案包 default.yaml 的开箱启用集 ----

    @Test
    fun `解析白霜包 default_yaml 的 schema_list`() {
        val text = """
            schema_list:
              - schema: rime_frost             # 白霜拼音（全拼）
              - schema: rime_frost_double_pinyin_mspy
              - schema: rime_frost_t9
              # - schema: rime_frost_double_pinyin_abc
              - schema: rime_frost_wubi86

            menu:
              page_size: 8
        """.trimIndent()
        assertEquals(
            listOf(
                "rime_frost",
                "rime_frost_double_pinyin_mspy",
                "rime_frost_t9",
                "rime_frost_wubi86",
            ),
            SchemaManager.parseSchemaListBlock(text),
        )
    }

    @Test
    fun `解析时忽略注释与引号`() {
        val text = "schema_list:\n  - schema: \"a\" # x\n  - schema: 'b'\n"
        assertEquals(listOf("a", "b"), SchemaManager.parseSchemaListBlock(text))
    }

    @Test
    fun `没有 schema_list 时返回空`() {
        assertTrue(SchemaManager.parseSchemaListBlock("menu:\n  page_size: 8\n").isEmpty())
    }
}
