package com.gremlin.app

import android.content.Intent
import android.content.SharedPreferences
import android.os.Bundle
import android.animation.ValueAnimator
import android.content.res.ColorStateList
import android.graphics.Color
import android.os.Handler
import android.os.Looper
import android.text.Editable
import android.text.TextWatcher
import android.view.View
import android.view.ViewGroup
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.widget.ArrayAdapter
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.PopupMenu
import android.widget.ScrollView
import android.widget.ListPopupWindow
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import com.google.zxing.integration.android.IntentIntegrator
import com.google.zxing.integration.android.IntentResult
import com.gremlin.app.ChatTranscript.Kind
import com.gremlin.app.ChatTranscript.Turn
import com.gremlin.app.attach.Attachments
import com.gremlin.app.voice.VoiceOutput
import java.io.File
import org.json.JSONObject
import java.net.URI

class MainActivity : AppCompatActivity() {

    private lateinit var prefs: SharedPreferences
    private lateinit var gremlinClient: GremlinClient
    private lateinit var voiceOutput: VoiceOutput

    private lateinit var connectionLabel: TextView
    private lateinit var tokenUsageLabel: TextView
    private lateinit var chat: ChatView
    private lateinit var statusDot: View
    private lateinit var sendButton: ImageButton
    private lateinit var hologramFrame: FrameLayout
    private lateinit var thinkingStatus: TextView
    private lateinit var messageInput: EditText
    private lateinit var hologramView: WebView

    // Internal app storage -- no permission needed on any Android
    // version, unlike shared/external storage.
    private val historyFile: File by lazy { File(filesDir, "chat_history.txt") }

    // Must be registered as a property, not inside onCreate's body --
    // ActivityResultContracts requires registration before the activity
    // reaches STARTED. Storage Access Framework: writes wherever the
    // user picks via the system file dialog, no manifest permission
    // needed on any Android version.
    private val exportLauncher = registerForActivityResult(ActivityResultContracts.CreateDocument("text/plain")) { uri ->
        if (uri == null) return@registerForActivityResult
        try {
            contentResolver.openOutputStream(uri)?.use { it.write(chat.plainText().toByteArray()) }
            Toast.makeText(this, "Chat exported", Toast.LENGTH_SHORT).show()
        } catch (e: Exception) {
            Toast.makeText(this, "Export failed: ${e.message}", Toast.LENGTH_LONG).show()
        }
    }

    // Storage Access Framework -- the user picks any file, PDF, or image
    // and we read it through a content:// URI. No storage permission is
    // needed on any Android version, which is why there's still no
    // READ_EXTERNAL_STORAGE in the manifest.
    private val attachLauncher = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) handleAttachment(uri)
    }

    // Set just before launching downloadLauncher -- which build's zip the
    // user is about to pick a save location for.
    private var pendingBuildName: String? = null

    // The conversation thread /command calls ride on. Null = the default
    // single thread; picked/created via ConversationsActivity.
    private var currentThread: String?
        get() = prefs.getString("current_thread", null)
        set(v) { prefs.edit().putString("current_thread", v).apply() }

    private val conversationsLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { res ->
            if (res.resultCode != android.app.Activity.RESULT_OK) return@registerForActivityResult
            val tid = res.data?.getStringExtra(ConversationsActivity.EXTRA_THREAD)
                ?: return@registerForActivityResult
            val ttl = res.data?.getStringExtra(ConversationsActivity.EXTRA_TITLE) ?: "chat"
            currentThread = tid
            chat.clear()
            historyFile.writeText("")
            appendSystemTurn("— $ttl —", false)
            // this thread's own running total isn't known until its next
            // reply comes back -- showing the PREVIOUS thread's count
            // here would be actively wrong, not just stale.
            updateTokenUsageLabel(null)
        }

    private val downloadLauncher = registerForActivityResult(ActivityResultContracts.CreateDocument("application/zip")) { uri ->
        val name = pendingBuildName
        pendingBuildName = null
        if (uri == null || name == null) return@registerForActivityResult
        thinkingStatus.visibility = View.VISIBLE
        Thread {
            val err = try {
                contentResolver.openOutputStream(uri)?.use { out ->
                    gremlinClient.downloadBuild(name, out)
                } ?: "Couldn't open the file you picked"
            } catch (e: Exception) {
                "Save failed: ${e.message}"
            }
            runOnUiThread {
                thinkingStatus.visibility = View.GONE
                if (err == null) appendSystemTurn("Saved $name.zip", false)
                else appendSystemTurn("Couldn't get $name: $err", true)
            }
        }.start()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        prefs = getSharedPreferences("gremlin_prefs", MODE_PRIVATE)
        gremlinClient = GremlinClient(prefs, applicationContext)
        voiceOutput = VoiceOutput(applicationContext, prefs)

        connectionLabel = findViewById(R.id.connection_label)
        tokenUsageLabel = findViewById(R.id.token_usage_label)
        statusDot = findViewById(R.id.status_dot)
        sendButton = findViewById(R.id.send_button)
        hologramFrame = findViewById(R.id.hologram_frame)
        chat = ChatView(findViewById<LinearLayout>(R.id.chat_list), findViewById<ScrollView>(R.id.chat_scroll))
        if (historyFile.exists()) {
            chat.load(historyFile.readText())
        }
        thinkingStatus = findViewById(R.id.thinking_status)
        messageInput = findViewById(R.id.message_input)

        hologramView = findViewById(R.id.hologram_view)
        hologramView.setBackgroundColor(Color.TRANSPARENT)
        hologramView.settings.javaScriptEnabled = true
        hologramView.addJavascriptInterface(JsBridge(), "Android")
        hologramView.loadUrl("file:///android_asset/hologram.html")

        messageInput.setOnFocusChangeListener { _, focused -> setHologramSmall(focused) }
        findViewById<View>(R.id.more_button).setOnClickListener { showMoreMenu(it) }
        findViewById<View>(R.id.builds_button).setOnClickListener {
            startActivity(Intent(this, BuildsActivity::class.java))
        }
        findViewById<View>(R.id.chats_button).setOnClickListener {
            conversationsLauncher.launch(Intent(this, ConversationsActivity::class.java))
        }
        sendButton.setOnClickListener { if (busy) stopWork() else sendMessage() }
        // A real, always-visible way into Settings -- the hologram WebView
        // can also open it via JsBridge.openSettings() (tapping the
        // hologram itself), but that's JS-driven and its tappable area
        // changes once models are linked/paired, which made Settings
        // effectively unreachable once you'd actually set anything up.
        // This button doesn't depend on the hologram's state at all.
        findViewById<View>(R.id.settings_button).setOnClickListener { openSettings() }
        findViewById<View>(R.id.attach_button).setOnClickListener {
            // Broad filter rather than a narrow allow-list: Attachments
            // sniffs the real type itself, and an over-strict picker
            // filter is how "why can't I select my own file" bugs happen.
            attachLauncher.launch(arrayOf("*/*"))
        }

        // Keyboard was covering the input on API 35 edge-to-edge: pad the
        // root by the system bars + IME so the text box (and caret) stays
        // above the keyboard. Pairs with adjustResize in the manifest.
        val root = findViewById<View>(R.id.root)
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime())
            v.setPadding(v.paddingLeft, bars.top, v.paddingRight, bars.bottom)
            insets
        }

        setupSlashAutocomplete()
    }

    // "/" autocomplete, Claude-style: type "/" and a list of commands
    // pops above the input; tap one to insert it.
    private var commandHints: List<Pair<String, String>> = emptyList()
    private var slashPopup: ListPopupWindow? = null

    private fun setupSlashAutocomplete() {
        Thread { commandHints = gremlinClient.commandList() }.start()
        messageInput.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun afterTextChanged(e: Editable?) {
                val t = e?.toString() ?: ""
                if (!Regex("^/\\S*$").matches(t)) { slashPopup?.dismiss(); return }
                val q = t.removePrefix("/").lowercase()
                val hits = commandHints.filter { it.first.startsWith(q) }
                if (hits.isEmpty()) { slashPopup?.dismiss(); return }
                val pop = slashPopup ?: ListPopupWindow(this@MainActivity).also {
                    it.anchorView = messageInput
                    it.isModal = false
                    slashPopup = it
                }
                val adapter = object : ArrayAdapter<Pair<String, String>>(
                    this@MainActivity, android.R.layout.simple_list_item_2,
                    android.R.id.text1, hits) {
                    override fun getView(pos: Int, cv: View?, parent: android.view.ViewGroup): View {
                        val row = super.getView(pos, cv, parent)
                        row.findViewById<TextView>(android.R.id.text1).text = "/${hits[pos].first}"
                        row.findViewById<TextView>(android.R.id.text2).text = hits[pos].second
                        return row
                    }
                }
                pop.setAdapter(adapter)
                pop.setOnItemClickListener { _, _, pos, _ ->
                    messageInput.setText("/${hits[pos].first} ")
                    messageInput.setSelection(messageInput.text.length)
                    pop.dismiss()
                }
                pop.show()
            }
        })
    }

    /** Reads a picked file/PDF/image into text and sends it to Gremlin
     * along with whatever's already typed in the message box, so
     * "explain question 3" + a worksheet PDF is one turn. */
    private fun handleAttachment(uri: android.net.Uri) {
        val question = messageInput.text.toString().trim()
        val name = Attachments.displayName(this, uri)
        appendUserTurn(if (question.isEmpty()) "[attached $name]" else "$question  [attached $name]")
        messageInput.setText("")
        voiceOutput.stop()

        thinkingStatus.visibility = View.VISIBLE
        hologramView.evaluateJavascript("setTalking(true)", null)

        Thread {
            val extracted = try {
                Attachments.extract(this, uri)
            } catch (e: Exception) {
                null
            }
            if (extracted == null) {
                runOnUiThread {
                    appendSystemTurn("Couldn't read $name.", true)
                    thinkingStatus.visibility = View.GONE
                    hologramView.evaluateJavascript("setTalking(false)", null)
                }
                return@Thread
            }

            val result = gremlinClient.chat(Attachments.buildPrompt(question, extracted))
            runOnUiThread {
                val sub = when (result.source) {
                    "claude" -> "(standalone, via Claude)"
                    "gemini" -> "(standalone, via Gemini)"
                    else -> null
                }
                appendAssistantTurn(result.answer, sub)
                voiceOutput.speak(result.answer)
                thinkingStatus.visibility = View.GONE
                hologramView.evaluateJavascript("setTalking(false)", null)
                updateTokenUsageLabel(result.tokenUsage)
            }
        }.start()
    }

    override fun onResume() {
        super.onResume()
        updateConnectionLabel()
        statusHandler.post(statusPoll)
    }

    override fun onPause() {
        super.onPause()
        statusHandler.removeCallbacks(statusPoll)
    }

    // The status line. Also keeps the cached status fresh for the hologram's
    // head-slot labels and ModelSettingsActivity (see getStatusJson below).
    private val statusHandler = Handler(Looper.getMainLooper())
    private val statusPoll = object : Runnable {
        override fun run() {
            Thread {
                val raw = gremlinClient.fetchStatusRaw()
                runOnUiThread { showStatus(raw) }
            }.start()
            statusHandler.postDelayed(this, 15_000)
        }
    }

    private fun showStatus(raw: String?) {
        if (prefs.getString("host", null) == null) {
            updateConnectionLabel()
            setDot(R.color.gremlin_offline)
            return
        }
        val json = try { raw?.let { JSONObject(it) } } catch (e: Exception) { null }
        if (json == null) {
            connectionLabel.text = "Offline -- can't reach the desktop"
            setDot(R.color.gremlin_offline)
            return
        }
        val model = json.optString("primary_model", "Gremlin")
        val working = json.optBoolean("busy", false) || !json.optBoolean("model_loaded", true)
        connectionLabel.text = "Online  ·  $model" + if (working) "  ·  busy" else ""
        setDot(if (working) R.color.gremlin_busy else R.color.gremlin_online)
    }

    private fun setDot(colorId: Int) {
        statusDot.backgroundTintList = ColorStateList.valueOf(ContextCompat.getColor(this, colorId))
    }

    private var busy = false

    /** The send button doubles as Stop while Gremlin is working. */
    private fun setBusy(value: Boolean) {
        busy = value
        sendButton.setImageResource(if (value) R.drawable.ic_stop else R.drawable.ic_send)
        sendButton.contentDescription = if (value) "Stop" else "Send"
    }

    private fun stopWork() {
        Thread {
            gremlinClient.cancelStream()
            if (!prefs.getString("admin_token", null).isNullOrBlank()) gremlinClient.abortJobs()
        }.start()
    }

    /** Shrinks the hologram while typing. Scaled rather than cropped,
     *  because hologram.html draws at a fixed size. */
    private fun setHologramSmall(small: Boolean) {
        val scale = resources.displayMetrics.density
        val to = ((if (small) 64 else 170) * scale).toInt()
        ValueAnimator.ofInt(hologramFrame.height, to).apply {
            duration = 220
            addUpdateListener {
                val height = it.animatedValue as Int
                hologramFrame.layoutParams = hologramFrame.layoutParams.apply { this.height = height }
                val s = (height / (200f * scale)).coerceAtMost(1f)
                hologramView.scaleX = s
                hologramView.scaleY = s
            }
            start()
        }
    }

    private fun showMoreMenu(anchor: View) {
        val menu = PopupMenu(this, anchor)
        menu.menu.add("Switch model")
        menu.menu.add("Stop running jobs")
        menu.menu.add("Pair with QR code")
        menu.menu.add("Export chat")
        menu.setOnMenuItemClickListener { item ->
            when (item.title) {
                "Switch model" -> pickModel()
                "Stop running jobs" -> Thread {
                    val result = gremlinClient.abortJobs()
                    runOnUiThread { appendSystemTurn(result.message, !result.ok) }
                }.start()
                "Pair with QR code" -> startQrScan()
                "Export chat" -> exportLauncher.launch("gremlin-chat-${System.currentTimeMillis()}.txt")
            }
            true
        }
        menu.show()
    }

    private fun pickModel() {
        val raw = prefs.getString("cached_status_json", "") ?: ""
        val json = try { JSONObject(raw) } catch (e: Exception) { null }
        val models = json?.optJSONArray("models")
        val current = json?.optString("primary_model", "") ?: ""
        val names = (0 until (models?.length() ?: 0)).mapNotNull { models?.optJSONObject(it)?.optString("name") }
            .filter { it.isNotEmpty() && it != "gemini" }
        if (names.isEmpty()) {
            Toast.makeText(this, "Desktop model list isn't available", Toast.LENGTH_SHORT).show()
            return
        }
        val labels = names.map { if (it == current) "$it  (current)" else it }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle("Switch Gremlin's model")
            .setItems(labels) { _, which ->
                val name = names[which]
                if (name == current) return@setItems
                AlertDialog.Builder(this)
                    .setTitle("Switch to $name?")
                    .setMessage("This changes the model Gremlin runs on.")
                    .setNegativeButton("Cancel", null)
                    .setPositiveButton("Switch") { _, _ ->
                        Thread {
                            val answer = gremlinClient.command("/model", "use $name", currentThread)
                            runOnUiThread { appendSystemTurn(answer, false) }
                        }.start()
                    }
                    .show()
            }
            .show()
    }

    private fun openSettings() {
        val intent = Intent(this, SettingsActivity::class.java)
        intent.putExtra("host", prefs.getString("host", null))
        intent.putExtra("port", prefs.getInt("port", 0))
        intent.putExtra("token", prefs.getString("token", null))
        startActivity(intent)
    }

    private inner class JsBridge {
        @JavascriptInterface
        fun openSettings() {
            runOnUiThread { this@MainActivity.openSettings() }
        }

        @JavascriptInterface
        fun openModelSettings(name: String) {
            runOnUiThread {
                val intent = Intent(this@MainActivity, ModelSettingsActivity::class.java)
                intent.putExtra("modelName", name)
                intent.putExtra("host", prefs.getString("host", null))
                intent.putExtra("port", prefs.getInt("port", 0))
                intent.putExtra("token", prefs.getString("token", null))
                intent.putExtra("adminToken", prefs.getString("admin_token", null))
                startActivity(intent)
            }
        }

        // Synchronous by necessity -- addJavascriptInterface calls block
        // the WebView's JS thread for their return value, there's no way
        // to hand back a Promise here. Whatever was last fetched in
        // onResume (or an empty string if nothing's been paired/fetched
        // yet) is what the hologram gets; hologram.html already retries
        // once shortly after load to cover that empty-first-call case.
        @JavascriptInterface
        fun getStatusJson(): String {
            return prefs.getString("cached_status_json", "") ?: ""
        }

        @JavascriptInterface
        fun quit() {
            // No-op on Android -- back/home already covers this. Kept so
            // the shared hologram.html doesn't need Android-specific logic
            // beyond hiding the button, which it already does in JS.
        }
    }

    /** Reflects configuration, not the outcome of the last message --
     * see appendToLog for what actually answered each specific message. */
    private fun updateConnectionLabel() {
        val host = prefs.getString("host", null)
        val port = prefs.getInt("port", 0)
        val hasAnthropicKey = !prefs.getString("anthropic_key", null).isNullOrBlank()
        val hasGeminiKey = !prefs.getString("gemini_key", null).isNullOrBlank()

        connectionLabel.text = when {
            host != null && port != 0 -> "Connecting to $host..."
            hasAnthropicKey || hasGeminiKey -> "Standalone mode -- not paired with a desktop"
            else -> "Not paired yet -- open Settings to set up"
        }
    }

    /** Running token total for this whole conversation thread -- "like
     * Claude". The desktop computes it from the FULL on-disk transcript
     * (gremlin_core/history.py), so it's a real cumulative count, not a
     * per-message stat. Hidden entirely when there's nothing to show
     * (away-mode, an old server, a backend that doesn't report usage)
     * rather than showing a confusing "0 tokens". */
    private fun updateTokenUsageLabel(usage: TokenUsage?) {
        if (usage == null || usage.totalTokens <= 0) {
            tokenUsageLabel.visibility = View.GONE
            return
        }
        tokenUsageLabel.text = "${"%,d".format(usage.totalTokens)} tokens this conversation"
        tokenUsageLabel.visibility = View.VISIBLE
    }

    private fun startQrScan() {
        IntentIntegrator(this)
            .setDesiredBarcodeFormats(listOf(IntentIntegrator.QR_CODE))
            .setPrompt("Scan the pairing code shown by `gremlin serve`")
            .setBeepEnabled(false)
            .initiateScan()
    }

    // onActivityResult is deprecated in favor of the newer Activity Result
    // API, but it's still the standard, documented integration path for
    override fun onDestroy() {
        super.onDestroy()
        voiceOutput.shutdown()
    }

    // zxing-android-embedded's classic IntentIntegrator flow. Android
    // Studio will likely show a deprecation lint warning here -- expected,
    // safe to ignore for this specific library usage.
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: android.content.Intent?) {
        val result: IntentResult? = IntentIntegrator.parseActivityResult(requestCode, resultCode, data)
        if (result?.contents == null) {
            super.onActivityResult(requestCode, resultCode, data)
            return
        }
        handleScannedPairingUrl(result.contents)
    }

    private fun handleScannedPairingUrl(scanned: String) {
        try {
            val uri = URI(scanned)
            val scannedToken = uri.query
                ?.split("&")
                ?.map { it.split("=", limit = 2) }
                ?.firstOrNull { it.getOrNull(0) == "token" }
                ?.getOrNull(1)

            if (uri.host == null || scannedToken == null) {
                Toast.makeText(this, "That QR code doesn't look like a Gremlin pairing code", Toast.LENGTH_LONG).show()
                return
            }

            val resolvedPort = if (uri.port != -1) uri.port else 8765
            prefs.edit()
                .putString("host", uri.host)
                .putInt("port", resolvedPort)
                .putString("token", scannedToken)
                .apply()

            updateConnectionLabel()
            appendToLog("(paired with ${uri.host}:$resolvedPort)")
        } catch (e: Exception) {
            Toast.makeText(this, "Couldn't read that QR code: ${e.message}", Toast.LENGTH_LONG).show()
        }
    }

    private fun saveHistory() {
        try {
            historyFile.writeText(chat.plainText())
        } catch (e: Exception) {
            // Non-fatal -- chat still works in-memory for this session
            // even if persistence fails for some reason (e.g. full disk).
        }
    }

    private fun appendToLog(line: String) {
        chat.add(Turn(Kind.SYSTEM, line))
        saveHistory()
    }

    private fun appendUserTurn(message: String) {
        chat.add(Turn(Kind.USER, message))
        saveHistory()
    }

    private fun appendAssistantTurn(answer: String, subStatus: String?) {
        chat.add(Turn(Kind.GREMLIN, answer, subStatus))
        saveHistory()
    }

    private fun sendMessage() {
        val message = messageInput.text.toString().trim()
        if (message.isEmpty()) return

        if (message.startsWith("/")) {
            messageInput.setText("")
            handleSlashCommand(message)
            return
        }

        appendUserTurn(message)
        messageInput.setText("")
        voiceOutput.stop() // a new message shouldn't have gremlin still talking over it

        // Pushed directly rather than polled -- unlike the desktop
        // window (a separate process from wherever chat actually
        // happens), this WebView lives in the same activity as the
        // chat call itself, so there's no need for the file-based
        // signal gremlin_core.consult uses for the desktop case.
        hologramView.evaluateJavascript("setTalking(true)", null)
        thinkingStatus.text = getString(R.string.thinking)
        thinkingStatus.visibility = View.VISIBLE
        setBusy(true)

        fun subStatusFor(source: String): String? = when (source) {
            "claude" -> "(standalone, via Claude)"
            "gemini" -> "(standalone, via Gemini)"
            "stopped" -> "(stopped)"
            else -> null
        }

        // First message after a service restart: the model is a ~90s cold
        // read off the desktop's HDD. Probe /status in parallel and, if
        // it's still warming, say so instead of leaving a bare spinner.
        Thread {
            if (gremlinClient.desktopReadiness() == GremlinClient.DesktopReadiness.WARMING) {
                runOnUiThread {
                    if (thinkingStatus.visibility == View.VISIBLE) {
                        thinkingStatus.text = getString(R.string.waking_gremlin)
                    }
                }
            }
        }.start()

        Thread {
            val started = java.util.concurrent.atomic.AtomicBoolean(false)
            gremlinClient.chatStream(
                message,
                onDelta = { delta ->
                    runOnUiThread {
                        if (started.compareAndSet(false, true)) {
                            beginAssistantStreamTurn()
                            thinkingStatus.visibility = View.GONE
                        }
                        appendAssistantDelta(delta)
                    }
                },
                onDone = { result ->
                    runOnUiThread {
                        val sub = subStatusFor(result.source)
                        if (started.get()) {
                            finishAssistantStreamTurn(result.answer, sub)
                        } else if (result.source == "stopped") {
                            appendSystemTurn("Stopped.", false)
                        } else {
                            // no deltas arrived (fallback path / action / clear)
                            appendAssistantTurn(result.answer, sub)
                        }
                        setBusy(false)
                        if (result.source != "stopped") voiceOutput.speak(result.answer)
                        hologramView.evaluateJavascript("setTalking(false)", null)
                        thinkingStatus.visibility = View.GONE
                        updateTokenUsageLabel(result.tokenUsage)
                    }
                },
            )
        }.start()
    }

    private fun beginAssistantStreamTurn() = chat.beginStream()

    private fun appendAssistantDelta(delta: String) = chat.appendStream(delta)

    /** The done frame's answer wins: it can differ from the streamed text
     *  (reasoning stripped, or a fallback that replaced everything). */
    private fun finishAssistantStreamTurn(finalAnswer: String, subStatus: String?) {
        chat.finishStream(finalAnswer, subStatus)
        saveHistory()
    }

    /** `/claude` is the ONE remaining typed command, on purpose.
     *
     * Everything else that used to need a slash command (/desktop,
     * /root, /edit, /fix, /reboot, /snapshots, /rollback, /updatecheck)
     * is now just something you say -- "check for updates", "fix my
     * backup script", "reboot the desktop" -- and the desktop's intent
     * router (gremlin_core/intent.py) works out what you meant, finds
     * any file paths itself, and asks for a plain yes/no before anything
     * destructive. See that module for why conversation never pays a
     * latency cost for this.
     *
     * /claude stays explicit because it hands full autonomy to a
     * separate Claude Code session that can read and write anything in
     * the project with no further confirmation. That should require
     * deliberately typing it, never a classifier's best guess. */
    private fun handleSlashCommand(message: String) {
        appendUserTurn(message)
        val parts = message.trim().split(Regex("\\s+"))
        val cmd = parts.getOrNull(0) ?: ""

        when (cmd) {
            "/claude" -> {
                val rest = message.removePrefix("/claude").trim()
                val confirmed = rest.endsWith(" confirm")
                val problem = (if (confirmed) rest.removeSuffix("confirm") else rest).trim()
                if (problem.isEmpty()) {
                    appendSystemTurn("Usage: /claude <problem to fix>  (then /claude <problem> confirm to actually run it)", true)
                } else if (!confirmed) {
                    appendSystemTurn(
                        "This runs a full Claude Code session on the desktop with FULL autonomy -- " +
                            "it can read/write any file and run any command in the project to fix this, " +
                            "no further confirmation once started. Type \"/claude $problem confirm\" to proceed.",
                        false,
                    )
                } else {
                    runAdminSlash { gremlinClient.claudeOverride(problem) }
                }
            }

            "/builds" -> {
                val sub = parts.getOrNull(1)
                if (sub == "get") {
                    val name = parts.drop(2).joinToString(" ").trim()
                    if (name.isEmpty()) {
                        appendSystemTurn("Usage: /builds get <name>", true)
                    } else {
                        pendingBuildName = name
                        downloadLauncher.launch("$name.zip")
                    }
                } else {
                    thinkingStatus.visibility = View.VISIBLE
                    Thread {
                        val (list, err) = gremlinClient.listBuilds()
                        runOnUiThread {
                            thinkingStatus.visibility = View.GONE
                            when {
                                err != null -> appendSystemTurn(err, true)
                                list.isEmpty() -> appendSystemTurn("No builds on the desktop yet.", false)
                                else -> appendSystemTurn(
                                    "Builds on the desktop:\n\n" + list.joinToString("\n") { b ->
                                        val kb = b.sizeBytes / 1024
                                        val over = if (b.tooBig) "  (too big to download)" else ""
                                        "• ${b.name} — ${b.goal} (${kb} KB, ${b.fileCount} files)$over"
                                    } + "\n\nType \"/builds get <name>\" to save one to your phone.",
                                    false,
                                )
                            }
                        }
                    }.start()
                }
            }

            // Kept in sync with gremlin_core/magic/commands.py's COMMANDS
            // dict by hand -- this whitelist is the ONLY thing deciding
            // whether a typed "/whatever" actually reaches the desktop
            // (gremlinClient.command()) or falls into the "unknown
            // command" branch below and goes nowhere. /override shipped
            // on the desktop 2026-09-13 and sat invisible here for a
            // while because this list wasn't updated alongside it --
            // exactly the silent-failure trap to not repeat: when
            // adding a command in commands.py, add its name here too.
            "/chat", "/skill", "/build", "/fix", "/model", "/override", "/defense", "/do", "/memory" -> {
                val args = message.removePrefix(cmd).trim()
                thinkingStatus.visibility = View.VISIBLE
                hologramView.evaluateJavascript("setTalking(true)", null)
                Thread {
                    val answer = gremlinClient.command(cmd, args, currentThread)
                    runOnUiThread {
                        thinkingStatus.visibility = View.GONE
                        hologramView.evaluateJavascript("setTalking(false)", null)
                        appendAssistantTurn(answer, null)
                        voiceOutput.speak(answer)
                    }
                }.start()
            }

            else -> appendSystemTurn(
                "Commands: /chat /skill /build /fix /model /override /defense /do /memory  ·  " +
                    "/claude <problem>  ·  /builds, /builds get <name>\n\n" +
                    "Or just say it normally — the desktop works out what you mean and asks before anything destructive.",
                true,
            )
        }
    }

    private fun runAdminSlash(call: () -> AdminResult) {
        thinkingStatus.visibility = View.VISIBLE
        Thread {
            val result = call()
            runOnUiThread {
                appendSystemTurn(result.message, !result.ok)
                thinkingStatus.visibility = View.GONE
            }
        }.start()
    }

    private fun appendSystemTurn(text: String, isError: Boolean) {
        chat.add(Turn(Kind.SYSTEM, if (isError) "! $text" else text))
        saveHistory()
    }
}
