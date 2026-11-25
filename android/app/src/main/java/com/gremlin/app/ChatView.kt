package com.gremlin.app

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.text.SpannableStringBuilder
import android.text.Spanned
import android.text.style.BackgroundColorSpan
import android.text.style.ForegroundColorSpan
import android.text.style.RelativeSizeSpan
import android.text.style.StyleSpan
import android.text.style.TypefaceSpan
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.ContextCompat
import com.gremlin.app.ChatTranscript.ActivityEvent
import com.gremlin.app.ChatTranscript.Kind
import com.gremlin.app.ChatTranscript.Turn
import java.util.Locale

/** Draws the conversation as message bubbles, syntax-highlighted code cards, and collapsible activity pills. */
class ChatView(private val list: LinearLayout, private val scroll: ScrollView) {
    private val context: Context = list.context
    private val turns = mutableListOf<Turn>()
    private var streamBubble: TextView? = null
    private var streamPillWrap: LinearLayout? = null
    private var streamPillHeader: TextView? = null
    private val streamActivities = mutableListOf<ActivityEvent>()
    private var lastMagicStart = -1
    private var streamText = StringBuilder()
    private var streamBase = ""
    private var lastUserBubble: TextView? = null

    var onEditLastUser: (() -> Unit)? = null
    var onSaveExemplar: ((
        prompt: String,
        reply: String,
        head: String,
        source: String,
        onDone: (ok: Boolean, total: Int, duplicate: Boolean, error: String?) -> Unit,
    ) -> Unit)? = null

    private fun color(id: Int) = ContextCompat.getColor(context, id)
    private fun dp(value: Int) = TypedValue.applyDimension(
        TypedValue.COMPLEX_UNIT_DIP, value.toFloat(), context.resources.displayMetrics).toInt()

    fun load(saved: String) {
        clear()
        val all = ChatTranscript.parse(saved)
        all.takeLast(MAX_SHOWN).forEach {
            if (it.kind == Kind.MAGIC) lastMagicStart = list.childCount
            if (it.kind == Kind.MAGIC && needsRichLayout(it)) drawRichMagic(it) else draw(it)
        }
        turns.addAll(all)
        scrollToEnd()
    }

    fun loadTurns(items: List<Turn>) {
        clear()
        items.takeLast(MAX_SHOWN).forEach {
            if (it.kind == Kind.MAGIC) lastMagicStart = list.childCount
            if (it.kind == Kind.MAGIC && needsRichLayout(it)) drawRichMagic(it) else draw(it)
        }
        turns.addAll(items)
        scrollToEnd()
    }

    fun clear() {
        turns.clear()
        list.removeAllViews()
        streamBubble = null
        streamPillWrap = null
        streamPillHeader = null
        streamActivities.clear()
        streamBase = ""
        lastMagicStart = -1
        lastUserBubble = null
    }

    fun add(turn: Turn) {
        if (turn.kind == Kind.MAGIC) lastMagicStart = list.childCount
        turns.add(turn)
        if (turn.kind == Kind.MAGIC && needsRichLayout(turn)) drawRichMagic(turn) else draw(turn)
        scrollToEnd()
    }

    fun lastUserText(): String? = turns.lastOrNull { it.kind == Kind.USER }?.text

    fun lastAssistantText(): String? =
        if (turns.isNotEmpty() && turns.last().kind == Kind.MAGIC) turns.last().text else null

    /** Update the most recent USER bubble + record in place (for edit + re-roll). */
    fun updateLastUser(newText: String) {
        val idx = turns.indexOfLast { it.kind == Kind.USER }
        if (idx >= 0) turns[idx] = Turn(Kind.USER, newText)
        lastUserBubble?.text = styled(newText)
    }

    /** Remove the most recent assistant turn's views + record (for a re-roll). */
    fun removeLastAssistant() {
        if (turns.isEmpty() || turns.last().kind != Kind.MAGIC) return
        if (lastMagicStart in 0..list.childCount) {
            while (list.childCount > lastMagicStart) list.removeViewAt(list.childCount - 1)
        }
        turns.removeAt(turns.size - 1)
        lastMagicStart = -1
    }

    fun beginStream(preSeededActivities: List<ActivityEvent> = emptyList()) {
        streamBase = ""
        streamText = StringBuilder()
        streamActivities.clear()
        streamActivities.addAll(preSeededActivities)
        lastMagicStart = list.childCount

        val (wrap, header) = createLivePill()
        streamPillWrap = wrap
        streamPillHeader = header
        list.addView(wrap)
        if (streamActivities.isNotEmpty()) {
            wrap.visibility = View.VISIBLE
            header.text = SseParser.formatLivePillText(streamActivities.last())
        }

        streamBubble = draw(Turn(Kind.MAGIC, ""))
    }

    /** Adds or updates a live activity pill event while streaming is in progress. */
    fun addStreamActivity(event: ActivityEvent) {
        streamActivities.add(event)
        val wrap = streamPillWrap
        val hdr = streamPillHeader
        if (wrap != null && hdr != null) {
            wrap.visibility = View.VISIBLE
            hdr.text = SseParser.formatLivePillText(event)
        }
    }

    /** Replace the last assistant bubble with a live stream pre-seeded with its text. */
    fun beginContinueStream() {
        val prev = lastAssistantText().orEmpty()
        removeLastAssistant()
        streamBase = if (prev.isNotEmpty()) "$prev " else ""
        streamText = StringBuilder(streamBase)
        streamActivities.clear()
        streamPillWrap = null
        streamPillHeader = null
        lastMagicStart = list.childCount
        streamBubble = draw(Turn(Kind.MAGIC, "")).also {
            if (prev.isNotEmpty()) it.text = styled(prev)
        }
    }

    /** Non-streamed fallback for Continue: merge extra onto the last assistant turn. */
    fun appendToLastAssistant(
        extra: String,
        note: String?,
        prompt: String? = null,
        head: String? = null,
        source: String? = null,
        activities: List<ActivityEvent> = emptyList(),
    ) {
        val prevTurn = if (turns.isNotEmpty() && turns.last().kind == Kind.MAGIC) turns.last() else null
        val prev = prevTurn?.text ?: run {
            add(Turn(Kind.MAGIC, extra, note, prompt = prompt, head = head, source = source, activities = activities))
            return
        }
        val merged = if (extra.isNotBlank()) "$prev ${extra.trimStart()}".trim() else prev
        val mergedPrompt = prompt ?: prevTurn.prompt
        val mergedHead = head ?: prevTurn.head
        val mergedSource = source ?: prevTurn.source
        val mergedActs = (prevTurn.activities + activities).ifEmpty { emptyList() }
        removeLastAssistant()
        add(Turn(Kind.MAGIC, merged, note, prompt = mergedPrompt, head = mergedHead, source = mergedSource, activities = mergedActs))
    }

    fun appendStream(delta: String) {
        val bubble = streamBubble ?: return
        val nearEnd = scroll.getChildAt(0).bottom - (scroll.scrollY + scroll.height) < dp(120)
        streamText.append(delta)
        bubble.text = styled(streamText.toString())
        if (nearEnd) scrollToEnd()
    }

    fun finishStream(
        answer: String,
        note: String?,
        prompt: String? = null,
        head: String? = null,
        source: String? = null,
        activities: List<ActivityEvent> = emptyList(),
    ) {
        val bubble = streamBubble ?: return
        val pillWrap = streamPillWrap
        val raw = answer.ifEmpty { streamText.removePrefix(streamBase).toString() }
        val text = if (streamBase.isNotEmpty()) "${streamBase.trimEnd()} ${raw.trimStart()}".trim() else raw
        streamBase = ""
        val settledActs = activities.ifEmpty { streamActivities.toList() }
        streamActivities.clear()
        val turn = Turn(Kind.MAGIC, text, note, prompt = prompt, head = head, source = source, activities = settledActs)
        turns.add(turn)

        if (needsRichLayout(turn)) {
            if (pillWrap != null) list.removeView(pillWrap)
            list.removeView(bubble)
            drawRichMagic(turn)
        } else {
            if (pillWrap != null) {
                if (settledActs.isEmpty()) {
                    list.removeView(pillWrap)
                } else {
                    val idx = list.indexOfChild(pillWrap)
                    list.removeView(pillWrap)
                    addActivityPill(settledActs, insertAt = idx.coerceAtLeast(0))
                }
            }
            bubble.text = styled(text)
            addFooter(turn)
        }
        streamBubble = null
        streamPillWrap = null
        streamPillHeader = null
        scrollToEnd()
    }

    fun plainText(): String = ChatTranscript.serialize(turns)

    private fun needsRichLayout(turn: Turn): Boolean {
        val t = turn.text
        return t.contains("```") || ChatTranscript.parts(t).any { it.isTable }
    }

    private fun createLivePill(): Pair<LinearLayout, TextView> {
        val wrap = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundResource(R.drawable.bg_pill_chip)
            setPadding(dp(10), dp(5), dp(10), dp(5))
            visibility = View.GONE
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply {
                topMargin = dp(8)
                marginEnd = dp(24)
            }
        }
        val header = TextView(context).apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
            setTextColor(color(R.color.magic_accent))
        }
        wrap.addView(header)
        return wrap to header
    }

    private fun addActivityPill(activities: List<ActivityEvent>, insertAt: Int = -1) {
        if (activities.isEmpty()) return
        var expanded = false
        val wrap = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundResource(R.drawable.bg_pill_chip)
            setPadding(dp(11), dp(6), dp(11), dp(6))
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply {
                topMargin = dp(8)
                marginEnd = dp(24)
            }
        }
        val summaryView = TextView(context).apply {
            text = SseParser.formatCollapsedPillText(activities, expanded = false)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
            setTextColor(color(R.color.magic_text_secondary))
        }
        val detailBox = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
            setPadding(dp(10), dp(5), dp(4), dp(2))
        }
        for (ev in activities) {
            val line = TextView(context).apply {
                val msStr = if (ev.durationMs >= 1.0) " · ${ev.durationMs.toInt()} ms" else ""
                val detStr = if (ev.detail.isNotBlank()) " — ${ev.detail}" else ""
                text = "• ${ev.label.ifBlank { ev.stage }}$detStr$msStr"
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
                setTextColor(color(R.color.magic_text_secondary))
                setPadding(0, dp(2), 0, dp(2))
            }
            detailBox.addView(line)
        }
        wrap.addView(summaryView)
        wrap.addView(detailBox)
        wrap.setOnClickListener {
            expanded = !expanded
            detailBox.visibility = if (expanded) View.VISIBLE else View.GONE
            summaryView.text = SseParser.formatCollapsedPillText(activities, expanded = expanded)
        }
        if (insertAt in 0..list.childCount) {
            list.addView(wrap, insertAt)
        } else {
            list.addView(wrap)
        }
    }

    private fun draw(turn: Turn): TextView {
        if (turn.kind == Kind.MAGIC && turn.activities.isNotEmpty()) {
            addActivityPill(turn.activities)
        }
        val bubble = TextView(context).apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, if (turn.kind == Kind.SYSTEM) 12f else 15f)
            setLineSpacing(dp(3).toFloat(), 1.12f)
            setTextIsSelectable(true)
            text = styled(turn.text)
        }
        val params = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT)
        params.topMargin = dp(8)
        when (turn.kind) {
            Kind.USER -> {
                params.gravity = Gravity.END
                params.marginStart = dp(48)
                bubble.setBackgroundResource(R.drawable.bg_bubble_user)
                bubble.setTextColor(color(R.color.magic_text_primary))
                bubble.setPadding(dp(16), dp(11), dp(16), dp(11))
                lastUserBubble = bubble
                bubble.setOnLongClickListener {
                    if (bubble === lastUserBubble) { onEditLastUser?.invoke(); true } else false
                }
            }
            Kind.MAGIC -> {
                params.gravity = Gravity.START
                params.marginEnd = dp(24)
                bubble.setBackgroundResource(R.drawable.bg_bubble_bot)
                bubble.setTextColor(color(R.color.magic_text_primary))
                bubble.setPadding(dp(16), dp(11), dp(16), dp(11))
            }
            Kind.SYSTEM -> {
                params.gravity = Gravity.CENTER_HORIZONTAL
                bubble.gravity = Gravity.CENTER
                bubble.setTextColor(color(R.color.magic_system))
            }
        }
        list.addView(bubble, params)
        bubble.alpha = 0f
        bubble.translationY = dp(14).toFloat()
        bubble.animate().alpha(1f).translationY(0f).setDuration(220).start()
        if (turn.kind == Kind.MAGIC && turn.text.isNotEmpty()) addFooter(turn)
        return bubble
    }

    private fun resolvePromptForTurn(turn: Turn): String {
        val explicit = turn.prompt?.trim().orEmpty()
        if (explicit.isNotEmpty()) return explicit
        val idx = turns.indexOfLast { it === turn }.let { if (it >= 0) it else turns.lastIndex }
        for (i in (idx - 1) downTo 0) {
            if (turns[i].kind == Kind.USER) {
                val t = turns[i].text.trim()
                if (t.isNotEmpty()) return t
            }
        }
        return ""
    }

    private fun addFooter(turn: Turn) {
        val row = LinearLayout(context).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(6), dp(3), dp(6), dp(2))
        }
        if (!turn.note.isNullOrEmpty()) {
            val noteView = TextView(context).apply {
                text = turn.note
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
                setTextColor(color(R.color.magic_text_secondary))
                setPadding(0, 0, dp(12), 0)
            }
            row.addView(noteView)
        }
        val copyView = TextView(context).apply {
            text = "Copy"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
            setTextColor(color(R.color.magic_text_secondary))
            setPadding(0, dp(2), dp(6), dp(2))
            setOnClickListener {
                copy(turn.text)
                text = "Copied ✓"
                postDelayed({ text = "Copy" }, 1500L)
            }
        }
        row.addView(copyView)

        val saveView = TextView(context).apply {
            text = if (turn.saved) "Saved ✓" else "Save"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
            setTextColor(color(if (turn.saved) R.color.magic_accent else R.color.magic_text_secondary))
            setPadding(dp(8), dp(2), dp(6), dp(2))
            isEnabled = !turn.saved
            setOnClickListener {
                if (turn.saved || onSaveExemplar == null) return@setOnClickListener
                val prompt = resolvePromptForTurn(turn)
                if (prompt.isEmpty()) {
                    Toast.makeText(context, "No prompt found for this turn", Toast.LENGTH_SHORT).show()
                    return@setOnClickListener
                }
                isEnabled = false
                text = "Saving…"
                onSaveExemplar?.invoke(
                    prompt,
                    turn.text,
                    turn.head.orEmpty(),
                    turn.source.orEmpty(),
                ) { ok, total, _, err ->
                    if (ok) {
                        turn.saved = true
                        text = "Saved ✓"
                        setTextColor(color(R.color.magic_accent))
                        Toast.makeText(context, "Saved exemplar ($total/200)", Toast.LENGTH_SHORT).show()
                    } else {
                        isEnabled = true
                        text = "Save"
                        Toast.makeText(
                            context,
                            "Save failed: ${err ?: "network error"}",
                            Toast.LENGTH_SHORT,
                        ).show()
                    }
                }
            }
        }
        row.addView(saveView)
        list.addView(row)
    }

    private fun fadeIn(v: View) {
        v.alpha = 0f
        v.translationY = dp(14).toFloat()
        v.animate().alpha(1f).translationY(0f).setDuration(220).start()
    }

    /** A MAGIC turn with code or tables: collapsible activity pill, prose bubbles,
     *  syntax-highlighted code cards (with Copy), and scrollable tables. */
    private fun drawRichMagic(turn: Turn) {
        if (turn.activities.isNotEmpty()) {
            addActivityPill(turn.activities)
        }
        for (part in ChatTranscript.parts(turn.text)) {
            when {
                part.isCode -> addCodeCard(part.text, part.language)
                part.isTable -> addTableCard(part.text)
                else -> {
                    val t = part.text.trim()
                    if (t.isNotEmpty()) addProseBubble(t)
                }
            }
        }
        addFooter(turn)
    }

    private fun addProseBubble(text: String) {
        val sb = SpannableStringBuilder()
        appendInline(sb, text)
        val bubble = TextView(context).apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f)
            setLineSpacing(dp(3).toFloat(), 1.12f)
            setTextIsSelectable(true)
            setText(sb)
            setBackgroundResource(R.drawable.bg_bubble_bot)
            setTextColor(color(R.color.magic_text_primary))
            setPadding(dp(16), dp(11), dp(16), dp(11))
        }
        val params = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT)
        params.topMargin = dp(8)
        params.marginEnd = dp(24)
        params.gravity = Gravity.START
        list.addView(bubble, params)
        fadeIn(bubble)
    }

    private fun highlightedCode(code: String, language: String): CharSequence {
        val sb = SpannableStringBuilder(code)
        val spans = CodeHighlighter.tokenize(code, language)
        val kwColor = color(R.color.magic_blue)
        val strColor = color(R.color.magic_online)
        val numColor = color(R.color.magic_system)
        val cmtColor = color(R.color.magic_text_secondary)
        for (sp in spans) {
            val fg = when (sp.kind) {
                CodeHighlighter.TokenKind.KEYWORD -> kwColor
                CodeHighlighter.TokenKind.STRING -> strColor
                CodeHighlighter.TokenKind.NUMBER -> numColor
                CodeHighlighter.TokenKind.COMMENT -> cmtColor
            }
            sb.setSpan(ForegroundColorSpan(fg), sp.start, sp.end, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            if (sp.kind == CodeHighlighter.TokenKind.COMMENT) {
                sb.setSpan(StyleSpan(Typeface.ITALIC), sp.start, sp.end, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            } else if (sp.kind == CodeHighlighter.TokenKind.KEYWORD) {
                sb.setSpan(StyleSpan(Typeface.BOLD), sp.start, sp.end, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            }
        }
        return sb
    }

    private fun addCodeCard(code: String, language: String = "") {
        val card = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundResource(R.drawable.bg_code_card)
            setPadding(dp(12), dp(8), dp(12), dp(10))
        }
        val langLabel = TextView(context).apply {
            text = language.ifBlank { "code" }.uppercase(Locale.US)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
            typeface = Typeface.create(Typeface.MONOSPACE, Typeface.BOLD)
            letterSpacing = 0.08f
            setTextColor(color(R.color.magic_text_secondary))
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        }
        val copyBtn = TextView(context).apply {
            text = "Copy"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(color(R.color.magic_accent))
            setPadding(dp(10), dp(2), dp(4), dp(4))
            setOnClickListener {
                copy(code)
                text = "Copied ✓"
                postDelayed({ text = "Copy" }, 1500L)
            }
        }
        val header = LinearLayout(context).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, 0, 0, dp(4))
            addView(langLabel)
            addView(copyBtn)
        }
        card.addView(header, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))

        val divider = View(context).apply {
            setBackgroundColor(color(R.color.magic_line))
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dp(1).coerceAtLeast(1)
            ).apply { bottomMargin = dp(6) }
        }
        card.addView(divider)

        val codeView = TextView(context).apply {
            text = highlightedCode(code, language)
            typeface = Typeface.MONOSPACE
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
            setLineSpacing(dp(2).toFloat(), 1.15f)
            setTextColor(color(R.color.magic_text_primary))
            setTextIsSelectable(true)
            setHorizontallyScrolling(true)
        }
        val hs = HorizontalScrollView(context).apply {
            isHorizontalScrollBarEnabled = true
            isFillViewport = false
            addView(codeView)
        }
        card.addView(hs)
        val params = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)
        params.topMargin = dp(8)
        params.marginEnd = dp(16)
        list.addView(card, params)
        fadeIn(card)
    }

    private fun addTableCard(tableText: String) {
        val card = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundResource(R.drawable.bg_code_card)
            setPadding(dp(10), dp(8), dp(10), dp(8))
        }
        val tableView = TextView(context).apply {
            text = formatTableColumns(tableText)
            typeface = Typeface.MONOSPACE
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
            setTextColor(color(R.color.magic_text_primary))
            setTextIsSelectable(true)
            setHorizontallyScrolling(true)
        }
        val hs = HorizontalScrollView(context).apply {
            isHorizontalScrollBarEnabled = true
            addView(tableView)
        }
        card.addView(hs)
        val params = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)
        params.topMargin = dp(8)
        params.marginEnd = dp(16)
        list.addView(card, params)
        fadeIn(card)
    }

    private fun formatTableColumns(raw: String): String {
        val rows = raw.lines().filter { it.isNotBlank() }.map { line ->
            line.trim().removePrefix("|").removeSuffix("|").split("|").map { it.trim() }
        }
        if (rows.isEmpty()) return raw
        val cols = rows.maxOf { it.size }
        val widths = IntArray(cols) { c ->
            rows.maxOf { r -> r.getOrElse(c) { "" }.length }.coerceAtMost(36)
        }
        return rows.joinToString("\n") { r ->
            (0 until cols).joinToString(" │ ") { c ->
                r.getOrElse(c) { "" }.padEnd(widths[c])
            }
        }
    }

    private fun copy(text: String) {
        val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(ClipData.newPlainText("Magic", text))
        Toast.makeText(context, "Copied", Toast.LENGTH_SHORT).show()
    }

    private fun scrollToEnd() {
        scroll.post { scroll.fullScroll(View.FOCUS_DOWN) }
    }

    private fun styled(text: String): CharSequence {
        val out = SpannableStringBuilder()
        for (part in ChatTranscript.parts(text)) {
            if (part.isCode) {
                if (out.isNotEmpty() && out.last() != '\n') out.append('\n')
                val codeStart = out.length
                out.append(highlightedCode(part.text, part.language)).append('\n')
                val end = out.length
                out.setSpan(TypefaceSpan("monospace"), codeStart, end, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
                out.setSpan(RelativeSizeSpan(0.88f), codeStart, end, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
                out.setSpan(BackgroundColorSpan(color(R.color.magic_code_bg)), codeStart, end, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            } else {
                appendInline(out, part.text)
            }
        }
        return out
    }

    /** Inline `code`, ***bold-italic***, **bold**, and *italic*, preserving URLs. */
    private fun appendInline(out: SpannableStringBuilder, text: String) {
        var i = 0
        for (match in INLINE.findAll(text)) {
            out.append(text, i, match.range.first)
            val token = match.value
            val start = out.length
            when {
                token.startsWith("`") -> {
                    out.append(token.substring(1, token.length - 1))
                    out.setSpan(TypefaceSpan("monospace"), start, out.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
                    out.setSpan(ForegroundColorSpan(color(R.color.magic_accent)), start, out.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
                }
                token.startsWith("http://") || token.startsWith("https://") -> {
                    out.append(token)
                }
                token.startsWith("***") && token.endsWith("***") && token.length > 6 -> {
                    out.append(token.substring(3, token.length - 3))
                    out.setSpan(StyleSpan(Typeface.BOLD_ITALIC), start, out.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
                }
                token.startsWith("**") && token.endsWith("**") && token.length > 4 -> {
                    out.append(token.substring(2, token.length - 2))
                    out.setSpan(StyleSpan(Typeface.BOLD), start, out.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
                }
                token.startsWith("*") && token.endsWith("*") && token.length > 2 -> {
                    out.append(token.substring(1, token.length - 1))
                    out.setSpan(StyleSpan(Typeface.ITALIC), start, out.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
                }
                else -> out.append(token)
            }
            i = match.range.last + 1
        }
        out.append(text, i, text.length)
    }

    private companion object {
        const val MAX_SHOWN = 150
        val INLINE = Regex(
            "`[^`\n]+`" +
                "|https?://\\S+" +
                "|\\*\\*\\*(?!\\s)(?:[^*\n]*?[^*\\s\n])\\*\\*\\*(?!\\*)" +
                "|\\*\\*(?!\\s)(?:[^*\n]*?[^*\\s\n])\\*\\*(?!\\*)" +
                "|\\*(?!\\s)(?:[^*\n]*?[^*\\s\n])\\*(?!\\*)"
        )
    }
}
