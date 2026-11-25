package com.gremlin.app

import java.util.Locale

/**
 * Zero-dependency parser for SSE `activity` frames and summaries so both
 * Android runtime and plain JVM unit tests share exact frame parsing.
 */
object SseParser {

    sealed class Frame {
        data class Delta(val text: String) : Frame()
        data class Activity(val event: ChatTranscript.ActivityEvent) : Frame()
        data class RawJson(val type: String, val payload: String) : Frame()
    }

    fun parseLine(line: String): Frame? {
        val trimmed = line.trim()
        if (!trimmed.startsWith("data:")) return null
        val payload = trimmed.substring(5).trim()
        if (payload.isEmpty() || !payload.startsWith("{")) return null
        val fields = parseFlatObject(payload)
        return when (val type = fields["type"] ?: "") {
            "delta" -> Frame.Delta(fields["text"] ?: "")
            "activity" -> Frame.Activity(
                ChatTranscript.ActivityEvent(
                    stage = fields["stage"] ?: "tool",
                    label = fields["label"] ?: "",
                    detail = fields["detail"] ?: "",
                    durationMs = fields["duration_ms"]?.toDoubleOrNull() ?: 0.0,
                )
            )
            else -> Frame.RawJson(type, payload)
        }
    }

    fun parseActivityEventJson(jsonText: String): ChatTranscript.ActivityEvent? {
        val fields = parseFlatObject(jsonText)
        val stage = fields["stage"] ?: return null
        return ChatTranscript.ActivityEvent(
            stage = stage,
            label = fields["label"] ?: stage,
            detail = fields["detail"] ?: "",
            durationMs = fields["duration_ms"]?.toDoubleOrNull() ?: 0.0,
        )
    }

    fun parseActivitySummaryFromDone(jsonText: String): List<ChatTranscript.ActivityEvent> {
        val keyIdx = jsonText.indexOf("\"activity_summary\"")
        if (keyIdx < 0) return emptyList()
        val arrStart = jsonText.indexOf('[', keyIdx)
        if (arrStart < 0) return emptyList()
        val arrEnd = findMatchingBracket(jsonText, arrStart, '[', ']')
        if (arrEnd <= arrStart) return emptyList()
        val slice = jsonText.substring(arrStart + 1, arrEnd)
        val out = mutableListOf<ChatTranscript.ActivityEvent>()
        var i = 0
        while (i < slice.length) {
            if (slice[i] == '{') {
                val end = findMatchingBracket(slice, i, '{', '}')
                if (end > i) {
                    parseActivityEventJson(slice.substring(i, end + 1))?.let { out.add(it) }
                    i = end + 1
                    continue
                }
            }
            i++
        }
        return out
    }

    fun formatLivePillText(latest: ChatTranscript.ActivityEvent): String {
        val base = latest.label.ifBlank {
            when (latest.stage) {
                "web_search" -> "Searching web"
                "rerank" -> "Reranking sources"
                "look" -> "Checking desktop"
                "thinking" -> "Reasoning"
                "netsec" -> "Running security check"
                "attachment" -> "Reading attachment"
                else -> "Running tool"
            }
        }
        return if (base.endsWith("…") || base.endsWith("...")) base else "$base…"
    }

    fun formatCollapsedPillText(events: List<ChatTranscript.ActivityEvent>, expanded: Boolean = false): String {
        if (events.isEmpty()) return ""
        val arrow = if (expanded) "▾" else "▸"
        val labels = events.mapNotNull { ev ->
            val lbl = ev.label.trim()
            lbl.ifEmpty { ev.stage.replace('_', ' ') }
        }.distinct().take(3)
        val totalMs = events.sumOf { it.durationMs.coerceAtLeast(0.0) }
        val durPart = when {
            totalMs >= 1000.0 -> String.format(Locale.US, " • %.1fs", totalMs / 1000.0)
            totalMs >= 1.0 -> " • ${totalMs.toInt()}ms"
            else -> ""
        }
        return "$arrow ${labels.joinToString(" • ")}$durPart"
    }

    private fun findMatchingBracket(s: String, start: Int, open: Char, close: Char): Int {
        var depth = 0
        var inStr = false
        var i = start
        while (i < s.length) {
            val ch = s[i]
            if (inStr) {
                if (ch == '\\') {
                    i += 2
                    continue
                }
                if (ch == '"') inStr = false
            } else {
                when (ch) {
                    '"' -> inStr = true
                    open -> depth++
                    close -> {
                        depth--
                        if (depth == 0) return i
                    }
                }
            }
            i++
        }
        return -1
    }

    internal fun parseFlatObject(json: String): Map<String, String> {
        val out = LinkedHashMap<String, String>()
        val s = json.trim()
        if (!s.startsWith("{")) return out
        var i = 1
        val n = s.length
        fun skipWs() {
            while (i < n && s[i].isWhitespace()) i++
        }
        fun readString(): String {
            if (i >= n || s[i] != '"') return ""
            i++
            val sb = StringBuilder()
            while (i < n) {
                val ch = s[i]
                if (ch == '\\' && i + 1 < n) {
                    when (val esc = s[i + 1]) {
                        '"', '\\', '/' -> sb.append(esc)
                        'n' -> sb.append('\n')
                        'r' -> sb.append('\r')
                        't' -> sb.append('\t')
                        'u' -> if (i + 5 < n) {
                            val hex = s.substring(i + 2, i + 6)
                            hex.toIntOrNull(16)?.let { sb.append(it.toChar()) }
                            i += 4
                        }
                        else -> sb.append(esc)
                    }
                    i += 2
                    continue
                }
                if (ch == '"') {
                    i++
                    break
                }
                sb.append(ch)
                i++
            }
            return sb.toString()
        }
        while (i < n) {
            skipWs()
            if (i >= n || s[i] == '}') break
            if (s[i] == ',') { i++; continue }
            if (s[i] != '"') { i++; continue }
            val key = readString()
            skipWs()
            if (i < n && s[i] == ':') i++
            skipWs()
            if (i >= n) break
            when (s[i]) {
                '"' -> out[key] = readString()
                '{' -> {
                    val end = findMatchingBracket(s, i, '{', '}')
                    i = if (end > i) end + 1 else n
                }
                '[' -> {
                    val end = findMatchingBracket(s, i, '[', ']')
                    i = if (end > i) end + 1 else n
                }
                else -> {
                    val start = i
                    while (i < n && s[i] != ',' && s[i] != '}' && !s[i].isWhitespace()) i++
                    out[key] = s.substring(start, i)
                }
            }
        }
        return out
    }
}
