package com.gremlin.app

/** The chat as data. Saved history stays plain text ("› " starts one of
 *  your messages), so chats saved by older versions still load. */
object ChatTranscript {
    enum class Kind { USER, MAGIC, SYSTEM }

    data class Turn(val kind: Kind, val text: String, val note: String? = null)

    private const val USER_MARK = "› "
    private const val SYSTEM_MARK = "⁘ "

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

    /** A code fence or plain prose. */
    data class Part(val text: String, val isCode: Boolean)

    /** Splits on ``` fences. An unclosed fence counts as code, because a
     *  streaming reply is often mid-block. */
    fun parts(text: String): List<Part> {
        val out = mutableListOf<Part>()
        val pieces = text.split("```")
        pieces.forEachIndexed { i, piece ->
            if (i % 2 == 0) {
                if (piece.isNotEmpty()) out.add(Part(piece, false))
            } else {
                val newline = piece.indexOf('\n')
                val isLanguageTag = newline >= 0 && piece.take(newline).none { it.isWhitespace() }
                val code = if (isLanguageTag) piece.substring(newline + 1) else piece
                out.add(Part(code.trimEnd(), true))
            }
        }
        return out
    }
}
