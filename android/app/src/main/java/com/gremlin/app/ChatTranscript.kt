package com.gremlin.app

/** The chat as data. Saved history stays plain text ("› " starts one of
 *  your messages), so chats saved by older versions still load. */
object ChatTranscript {
    enum class Kind { USER, MAGIC, SYSTEM }

    data class ActivityEvent(
        val stage: String,
        val label: String,
        val detail: String = "",
        val durationMs: Double = 0.0,
    )

    data class Turn(
        val kind: Kind,
        val text: String,
        val note: String? = null,
        val prompt: String? = null,
        val head: String? = null,
        val source: String? = null,
        var saved: Boolean = false,
        val activities: List<ActivityEvent> = emptyList(),
    )

    private const val USER_MARK = "› "
    private const val SYSTEM_MARK = "⁘ "
    private val TABLE_SEP_RE = Regex("""^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$""")

    fun parse(saved: String): List<Turn> {
        val turns = mutableListOf<Turn>()
        val block = StringBuilder()
        var kind = Kind.MAGIC

        fun flush() {
            val text = block.toString().trim()
            block.setLength(0)
            if (text.isNotEmpty()) turns.add(Turn(kind, text))
        }

        for (line in saved.lines()) {
            when {
                line.startsWith(USER_MARK) -> {
                    flush()
                    kind = Kind.USER
                    block.append(line.removePrefix(USER_MARK)).append('\n')
                }
                line.startsWith(SYSTEM_MARK) -> {
                    flush()
                    kind = Kind.SYSTEM
                    block.append(line.removePrefix(SYSTEM_MARK)).append('\n')
                }
                kind != Kind.MAGIC && line.isBlank() -> {
                    flush()
                    kind = Kind.MAGIC
                }
                else -> block.append(line).append('\n')
            }
        }
        flush()
        return turns
    }

    fun serialize(turns: List<Turn>): String = turns.joinToString("\n\n") { turn ->
        val body = if (turn.note.isNullOrEmpty()) turn.text else "${turn.text}\n${turn.note}"
        when (turn.kind) {
            // These end at a blank line, so they can't contain one.
            Kind.USER -> USER_MARK + withoutBlankLines(body)
            Kind.SYSTEM -> SYSTEM_MARK + withoutBlankLines(body)
            Kind.MAGIC -> body
        }
    }

    private fun withoutBlankLines(text: String) = text.replace(Regex("\n\\s*\n+"), "\n")

    /** A code fence, Markdown pipe table, or plain prose segment. */
    data class Part(
        val text: String,
        val isCode: Boolean,
        val language: String = "",
        val isTable: Boolean = false,
    )

    /** Splits on ``` fences (plus Markdown pipe tables inside prose). An unclosed
     *  trailing fence counts as code, because a streaming reply is often mid-block. */
    fun parts(text: String): List<Part> {
        val out = mutableListOf<Part>()
        val pieces = text.split("```")
        pieces.forEachIndexed { i, piece ->
            if (i % 2 == 0) {
                if (piece.isNotEmpty()) splitTables(piece, out)
            } else {
                val newline = piece.indexOf('\n')
                val firstLine = if (newline >= 0) piece.take(newline).trim() else ""
                val isLanguageTag = newline >= 0 && firstLine.none { it.isWhitespace() }
                val lang = if (isLanguageTag) firstLine.lowercase() else ""
                val code = if (isLanguageTag) piece.substring(newline + 1) else piece
                out.add(Part(code.trimEnd(), isCode = true, language = lang))
            }
        }
        return out
    }

    private fun splitTables(prose: String, out: MutableList<Part>) {
        val lines = prose.lines()
        if (lines.size < 2) {
            out.add(Part(prose, isCode = false))
            return
        }
        val buf = StringBuilder()
        var i = 0
        while (i < lines.size) {
            if (i + 1 < lines.size && lines[i].trim().startsWith("|") && TABLE_SEP_RE.matches(lines[i + 1])) {
                if (buf.isNotEmpty()) {
                    out.add(Part(buf.toString(), isCode = false))
                    buf.setLength(0)
                }
                val tbl = StringBuilder()
                while (i < lines.size && lines[i].trim().startsWith("|")) {
                    if (tbl.isNotEmpty()) tbl.append('\n')
                    tbl.append(lines[i])
                    i++
                }
                out.add(Part(tbl.toString(), isCode = false, isTable = true))
                continue
            }
            if (buf.isNotEmpty()) buf.append('\n')
            buf.append(lines[i])
            i++
        }
        if (buf.isNotEmpty()) {
            out.add(Part(buf.toString(), isCode = false))
        }
    }
}
