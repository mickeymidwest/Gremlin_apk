package com.gremlin.app

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
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
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.ContextCompat
import com.gremlin.app.ChatTranscript.Kind
import com.gremlin.app.ChatTranscript.Turn

/** Draws the conversation as message bubbles. */
class ChatView(private val list: LinearLayout, private val scroll: ScrollView) {
    private val context: Context = list.context
    private val turns = mutableListOf<Turn>()
    private var streamBubble: TextView? = null
    private var streamText = StringBuilder()

    private fun color(id: Int) = ContextCompat.getColor(context, id)
    private fun dp(value: Int) = TypedValue.applyDimension(
        TypedValue.COMPLEX_UNIT_DIP, value.toFloat(), context.resources.displayMetrics).toInt()

    fun load(saved: String) {
        clear()
        val all = ChatTranscript.parse(saved)
        all.takeLast(MAX_SHOWN).forEach { draw(it) }
        turns.addAll(all)
        scrollToEnd()
    }

    fun clear() {
        turns.clear()
        list.removeAllViews()
        streamBubble = null
    }

    fun add(turn: Turn) {
        turns.add(turn)
        draw(turn)
        scrollToEnd()
    }

    fun beginStream() {
        streamText = StringBuilder()
        streamBubble = draw(Turn(Kind.GREMLIN, ""))
    }

    fun appendStream(delta: String) {
        val bubble = streamBubble ?: return
        val nearEnd = scroll.getChildAt(0).bottom - (scroll.scrollY + scroll.height) < dp(120)
        streamText.append(delta)
        bubble.text = styled(streamText.toString())
        if (nearEnd) scrollToEnd()
    }

    fun finishStream(answer: String, note: String?) {
        val bubble = streamBubble ?: return
        val text = answer.ifEmpty { streamText.toString() }
        val turn = Turn(Kind.GREMLIN, text, note)
        turns.add(turn)
        bubble.text = styled(text)
        addFooter(turn)
        streamBubble = null
        scrollToEnd()
    }

    fun plainText(): String = ChatTranscript.serialize(turns)

    private fun draw(turn: Turn): TextView {
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
                bubble.setTextColor(color(R.color.gremlin_text_primary))
                bubble.setPadding(dp(16), dp(11), dp(16), dp(11))
            }
            Kind.GREMLIN -> {
                params.gravity = Gravity.START
                params.marginEnd = dp(24)
                bubble.setBackgroundResource(R.drawable.bg_bubble_bot)
                bubble.setTextColor(color(R.color.gremlin_text_primary))
                bubble.setPadding(dp(16), dp(11), dp(16), dp(11))
            }
            Kind.SYSTEM -> {
                params.gravity = Gravity.CENTER_HORIZONTAL
                bubble.gravity = Gravity.CENTER
                bubble.setTextColor(color(R.color.gremlin_system))
            }
        }
        list.addView(bubble, params)
        bubble.alpha = 0f
        bubble.translationY = dp(14).toFloat()
        bubble.animate().alpha(1f).translationY(0f).setDuration(220).start()
        if (turn.kind == Kind.GREMLIN && turn.text.isNotEmpty()) addFooter(turn)
        return bubble
    }

    private fun addFooter(turn: Turn) {
        val footer = TextView(context).apply {
            text = listOfNotNull(turn.note, "Copy").joinToString("   ")
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
            setTextColor(color(R.color.gremlin_text_secondary))
            setPadding(dp(6), dp(3), dp(6), dp(2))
            setOnClickListener { copy(turn.text) }
        }
        list.addView(footer)
    }

    private fun copy(text: String) {
        val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(ClipData.newPlainText("Gremlin", text))
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
                out.append(part.text).append('\n')
                val end = out.length
                out.setSpan(TypefaceSpan("monospace"), codeStart, end, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
                out.setSpan(RelativeSizeSpan(0.88f), codeStart, end, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
                out.setSpan(BackgroundColorSpan(color(R.color.gremlin_code_bg)), codeStart, end, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            } else {
                appendInline(out, part.text)
            }
        }
        return out
    }

    /** Inline `code` and **bold**. */
    private fun appendInline(out: SpannableStringBuilder, text: String) {
        var i = 0
        for (match in INLINE.findAll(text)) {
            out.append(text, i, match.range.first)
            val token = match.value
            val start = out.length
            if (token.startsWith("`")) {
                out.append(token.substring(1, token.length - 1))
                out.setSpan(TypefaceSpan("monospace"), start, out.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
                out.setSpan(ForegroundColorSpan(color(R.color.gremlin_accent)), start, out.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            } else {
                out.append(token.substring(2, token.length - 2))
                out.setSpan(StyleSpan(Typeface.BOLD), start, out.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            }
            i = match.range.last + 1
        }
        out.append(text, i, text.length)
    }

    private companion object {
        const val MAX_SHOWN = 150
        val INLINE = Regex("`[^`\n]+`|\\*\\*[^*\n]+\\*\\*")
    }
}
