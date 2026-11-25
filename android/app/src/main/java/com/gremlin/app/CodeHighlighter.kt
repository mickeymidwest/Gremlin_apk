package com.gremlin.app

/**
 * Lightweight, zero-dependency syntax tokenizer for code blocks.
 * Runs cleanly on both Android UI and JVM unit tests.
 */
object CodeHighlighter {
    enum class TokenKind { COMMENT, STRING, KEYWORD, NUMBER }

    data class Span(val start: Int, val end: Int, val kind: TokenKind)

    private val KEYWORDS = setOf(
        "abstract", "and", "as", "async", "await", "break", "by", "case", "catch",
        "class", "companion", "const", "continue", "data", "def", "default", "do",
        "done", "echo", "elif", "else", "enum", "esac", "except", "export", "extends",
        "false", "False", "fi", "final", "finally", "for", "from", "fun", "function",
        "global", "if", "implements", "import", "in", "init", "inner", "interface",
        "internal", "is", "lambda", "lateinit", "let", "local", "match", "new",
        "nil", "None", "nonlocal", "not", "null", "object", "open", "or", "override",
        "package", "pass", "private", "protected", "public", "raise", "return",
        "sealed", "select", "SELECT", "FROM", "WHERE", "INSERT", "UPDATE", "DELETE",
        "CREATE", "TABLE", "JOIN", "static", "struct", "super", "switch", "then",
        "this", "throw", "throws", "true", "True", "try", "type", "typeof", "val",
        "var", "void", "when", "while", "with", "yield",
    )

    private val HASH_COMMENT_LANGS = setOf(
        "py", "python", "sh", "bash", "zsh", "shell", "yaml", "yml", "toml",
        "ini", "conf", "cfg", "rb", "ruby", "r", "makefile", "",
    )

    private val SLASH_COMMENT_LANGS = setOf(
        "kt", "kotlin", "java", "js", "javascript", "ts", "typescript", "jsx",
        "tsx", "c", "cpp", "h", "hpp", "rs", "rust", "go", "golang", "gradle",
        "kts", "jsonc", "css", "php", "",
    )

    private val NUMBER_RE = Regex("""\b(?:0x[0-9a-fA-F_]+|\d+(?:\.\d+)?)\b""")
    private val IDENT_RE = Regex("""\b[A-Za-z_][A-Za-z0-9_]*\b""")

    fun tokenize(code: String, language: String = ""): List<Span> {
        if (code.isEmpty()) return emptyList()
        val lang = language.trim().lowercase()
        val allowHash = lang in HASH_COMMENT_LANGS
        val allowSlash = lang in SLASH_COMMENT_LANGS
        val n = code.length
        val claimed = BooleanArray(n)
        val spans = mutableListOf<Span>()

        fun addSpan(start: Int, end: Int, kind: TokenKind) {
            if (start < 0 || end <= start || end > n) return
            for (k in start until end) {
                if (claimed[k]) return
            }
            for (k in start until end) claimed[k] = true
            spans.add(Span(start, end, kind))
        }

        // Pass 1: Comments and string literals left-to-right so comments inside
        // strings or keywords inside comments never mis-highlight.
        var i = 0
        while (i < n) {
            val c = code[i]
            when {
                allowSlash && c == '/' && i + 1 < n && code[i + 1] == '/' -> {
                    val eol = code.indexOf('\n', i).let { if (it < 0) n else it }
                    addSpan(i, eol, TokenKind.COMMENT)
                    i = eol
                }
                allowSlash && c == '/' && i + 1 < n && code[i + 1] == '*' -> {
                    val close = code.indexOf("*/", i + 2).let { if (it < 0) n else it + 2 }
                    addSpan(i, close, TokenKind.COMMENT)
                    i = close
                }
                allowHash && c == '#' -> {
                    val eol = code.indexOf('\n', i).let { if (it < 0) n else it }
                    addSpan(i, eol, TokenKind.COMMENT)
                    i = eol
                }
                (c == '"' || c == '\'') -> {
                    val quote = c
                    if (i + 2 < n && code[i + 1] == quote && code[i + 2] == quote) {
                        val triple = "$quote$quote$quote"
                        val close = code.indexOf(triple, i + 3).let { if (it < 0) n else it + 3 }
                        addSpan(i, close, TokenKind.STRING)
                        i = close
                    } else {
                        var j = i + 1
                        while (j < n) {
                            val ch = code[j]
                            if (ch == '\\') {
                                j += 2
                                continue
                            }
                            if (ch == quote || ch == '\n') {
                                if (ch == quote) j++
                                break
                            }
                            j++
                        }
                        addSpan(i, minOf(j, n), TokenKind.STRING)
                        i = j
                    }
                }
                c == '`' && (lang == "js" || lang == "ts" || lang == "javascript" || lang == "typescript") -> {
                    var j = i + 1
                    while (j < n) {
                        val ch = code[j]
                        if (ch == '\\') {
                            j += 2
                            continue
                        }
                        if (ch == '`') {
                            j++
                            break
                        }
                        j++
                    }
                    addSpan(i, minOf(j, n), TokenKind.STRING)
                    i = j
                }
                else -> i++
            }
        }

        // Pass 2: Keywords and numbers in unclaimed regions
        for (m in IDENT_RE.findAll(code)) {
            if (m.value in KEYWORDS) {
                addSpan(m.range.first, m.range.last + 1, TokenKind.KEYWORD)
            }
        }
        for (m in NUMBER_RE.findAll(code)) {
            addSpan(m.range.first, m.range.last + 1, TokenKind.NUMBER)
        }

        spans.sortBy { it.start }
        return spans
    }
}
