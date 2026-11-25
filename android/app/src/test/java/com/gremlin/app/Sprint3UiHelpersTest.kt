package com.gremlin.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

class Sprint3UiHelpersTest {

    @Test
    fun partsSplitsMultipleFencedBlocksWithLanguagesAndUnclosedStreamFence() {
        val sample = """
            Intro prose here.
            ```python
            def greet(name):
                return f"hi {name}"
            ```
            Middle explanation.
            ```bash
            echo "done"
            ```
            Trailing live block:
            ```kotlin
            val x = 42
        """.trimIndent()

        val parts = ChatTranscript.parts(sample)
        val codeParts = parts.filter { it.isCode }
        assertEquals(3, codeParts.size)
        assertEquals("python", codeParts[0].language)
        assertTrue(codeParts[0].text.contains("def greet"))
        assertEquals("bash", codeParts[1].language)
        assertTrue(codeParts[1].text.contains("echo \"done\""))
        assertEquals("kotlin", codeParts[2].language)
        assertTrue(codeParts[2].text.contains("val x = 42"))

        val proseParts = parts.filter { !it.isCode }
        assertTrue(proseParts.any { it.text.contains("Intro prose") })
        assertTrue(proseParts.any { it.text.contains("Middle explanation") })
    }

    @Test
    fun partsExtractsMarkdownTableSeparatelyFromProse() {
        val md = """
            Summary:
            | Service | Port |
            | --- | --- |
            | sshd | 22 |
            Done.
        """.trimIndent()

        val parts = ChatTranscript.parts(md)
        val tables = parts.filter { it.isTable }
        assertEquals(1, tables.size)
        assertTrue(tables[0].text.contains("| sshd | 22 |"))
        assertFalse(tables[0].isCode)
    }

    @Test
    fun codeHighlighterTokenizesKeywordsStringsCommentsAndNumbers() {
        val code = """
            # comment with 123 and return
            def add(a):
                msg = "hello return 99"
                return a + 7
        """.trimIndent()
        val spans = CodeHighlighter.tokenize(code, "python")
        assertTrue(spans.any { it.kind == CodeHighlighter.TokenKind.COMMENT })
        assertTrue(spans.any { it.kind == CodeHighlighter.TokenKind.STRING })
        assertTrue(spans.any { it.kind == CodeHighlighter.TokenKind.KEYWORD && code.substring(it.start, it.end) == "def" })
        assertTrue(spans.any { it.kind == CodeHighlighter.TokenKind.KEYWORD && code.substring(it.start, it.end) == "return" })
        assertTrue(spans.any { it.kind == CodeHighlighter.TokenKind.NUMBER && code.substring(it.start, it.end) == "7" })

        // Verify neither keywords nor numbers inside comment/string bleed through
        val commentSpan = spans.first { it.kind == CodeHighlighter.TokenKind.COMMENT }
        assertFalse(spans.any { it !== commentSpan && it.start >= commentSpan.start && it.end <= commentSpan.end })
    }

    @Test
    fun sseParserParsesActivityFramesAndDoneSummaryCleanly() {
        val actLine = """data: {"type": "activity", "stage": "web_search", "label": "Searched 3 sources", "detail": "docs.python.org, github.com", "duration_ms": 1420.5}"""
        val frame = SseParser.parseLine(actLine)
        assertTrue(frame is SseParser.Frame.Activity)
        val act = (frame as SseParser.Frame.Activity).event
        assertEquals("web_search", act.stage)
        assertEquals("Searched 3 sources", act.label)
        assertEquals("docs.python.org, github.com", act.detail)
        assertEquals(1420.5, act.durationMs, 0.01)

        val doneJson = """{"type":"done","answer":"ok","activity_summary":[{"stage":"web_search","label":"Searched 3 sources","detail":"python.org","duration_ms":1500.0},{"stage":"rerank","label":"Reranked","detail":"","duration_ms":600.0}]}"""
        val summary = SseParser.parseActivitySummaryFromDone(doneJson)
        assertEquals(2, summary.size)
        assertEquals("web_search", summary[0].stage)
        assertEquals("Reranked", summary[1].label)

        val collapsed = SseParser.formatCollapsedPillText(summary, expanded = false)
        assertEquals("▸ Searched 3 sources • Reranked • 2.1s", collapsed)
        val expanded = SseParser.formatCollapsedPillText(summary, expanded = true)
        assertTrue(expanded.startsWith("▾ "))
        assertNotNull(SseParser.parseLine("data: {\"type\":\"delta\",\"text\":\"hello\\n\"}"))
    }
}
