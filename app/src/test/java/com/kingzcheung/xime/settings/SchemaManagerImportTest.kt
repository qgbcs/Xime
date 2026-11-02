package com.kingzcheung.xime.settings

import org.junit.Assert.assertEquals
import org.junit.Test

/** SchemaManager 里保留的纯解析函数：sha256 与归档壳目录剥离。 */
class SchemaManagerImportTest {

    @Test
    fun `sha256Hex of abc`() {
        assertEquals(
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            SchemaManager.sha256Hex("abc".toByteArray()),
        )
    }

    @Test
    fun `sha256Hex of empty`() {
        assertEquals(
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            SchemaManager.sha256Hex(ByteArray(0)),
        )
    }


    // ── findSchemaBaseDir：剥 GitHub 归档壳目录（essay.txt 不能落进 rime-essay-master/） ──
    // ── findSchemaBaseDir：剥 GitHub 归档壳目录(修 essay.txt 落进 rime-essay-master/ 子目录) ──

    @Test
    fun `findSchemaBaseDir strips wrapper for package without schema yaml (rime-essay)`() {
        val entries = listOf(
            "rime-essay-master/essay.txt",
            "rime-essay-master/AUTHORS",
            "rime-essay-master/LICENSE",
        )
        assertEquals("rime-essay-master/", SchemaManager.findSchemaBaseDir(entries))
    }

    @Test
    fun `findSchemaBaseDir strips wrapper for rime-prelude (no schema yaml)`() {
        val entries = listOf(
            "rime-prelude-master/symbols.yaml",
            "rime-prelude-master/default.yaml",
            "rime-prelude-master/README.md",
        )
        assertEquals("rime-prelude-master/", SchemaManager.findSchemaBaseDir(entries))
    }

    @Test
    fun `findSchemaBaseDir no strip when no-schema files already at root`() {
        assertEquals("", SchemaManager.findSchemaBaseDir(listOf("essay.txt", "AUTHORS")))
    }

    @Test
    fun `findSchemaBaseDir no strip when no-schema files mixed root and subdir`() {
        assertEquals("", SchemaManager.findSchemaBaseDir(listOf("essay.txt", "sub/x.txt")))
    }

    @Test
    fun `findSchemaBaseDir uses schema parent dir (regression, wrapped scheme)`() {
        val entries = listOf(
            "rime-cangjie-master/cangjie5.schema.yaml",
            "rime-cangjie-master/cangjie5.dict.yaml",
            "rime-cangjie-master/README.md",
        )
        assertEquals("rime-cangjie-master/", SchemaManager.findSchemaBaseDir(entries))
    }

    @Test
    fun `findSchemaBaseDir empty for flat scheme (regression)`() {
        assertEquals(
            "",
            SchemaManager.findSchemaBaseDir(listOf("cangjie5.schema.yaml", "cangjie5.dict.yaml")),
        )
    }
}
