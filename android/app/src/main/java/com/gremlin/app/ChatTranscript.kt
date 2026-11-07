package com.gremlin.app

/** The chat as data. Saved history stays plain text ("› " starts one of
 *  your messages), so chats saved by older versions still load. */
object ChatTranscript {
    enum class Kind { USER, GREMLIN, SYSTEM }

    data class Turn(val kind: Kind, val text: String, val note: String? = null)

    private const val USER_MARK = "› "

    fun parse(saved: String): List<Turn> {
        val turns = mutableListOf<Turn>()
        val block = StringBuilder()
        var inUser = false

        fun flush() {
            val text = block.toString().trim()
            block.setLength(0)
            if (text.isNotEmpty()) turns.add(Turn(if (inUser) Kind.USER else Kind.GREMLIN, text))
        }

        for (line in saved.lines()) {
            when {
                line.startsWith(USER_MARK) -> {
                    flush()
                    inUser = true
                    block.append(line.removePrefix(USER_MARK)).append('\n')
                }
                inUser && line.isBlank() -> {
                    flush()
                    inUser = false
                }
                else -> block.append(line).append('\n')
            }
        }
        flush()
        return turns
    }

    fun serialize(turns: List<Turn>): String = turns.joinToString("\n\n") { turn ->
        val body = if (turn.note.isNullOrEmpty()) turn.text else "${turn.text}\n${turn.note}"
        if (turn.kind == Kind.USER) USER_MARK + body else body
    }

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
