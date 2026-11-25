package com.gremlin.app

import android.content.Intent
import android.content.SharedPreferences
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
import android.animation.ValueAnimator
import android.content.res.ColorStateList
import android.graphics.Color
import android.os.Handler
import android.os.Looper
import android.text.Editable
import android.text.SpannableStringBuilder
import android.text.Spanned
import android.text.TextWatcher
import android.text.style.ForegroundColorSpan
import android.text.style.StyleSpan
import android.view.Gravity
import android.view.KeyEvent
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.PopupMenu
import android.widget.ScrollView
import android.widget.ListPopupWindow
import android.widget.TextView
import android.widget.Toast
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.GravityCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.drawerlayout.widget.DrawerLayout
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
    private lateinit var magicClient: MagicClient
    private lateinit var voiceOutput: VoiceOutput

    private lateinit var connectionLabel: TextView
    private lateinit var tokenUsageLabel: TextView
    private lateinit var chat: ChatView
    private lateinit var statusDot: View
    private lateinit var sendButton: ImageButton
    private lateinit var hologramFrame: FrameLayout
    private lateinit var thinkingStatus: TextView
    private lateinit var approvalCardsContainer: LinearLayout
    private val approvalCards = LinkedHashMap<String, View>()
    private lateinit var messageInput: EditText
    private var lastUserMessage: String? = null
    private lateinit var hologramView: WebView

    private lateinit var drawerLayout: DrawerLayout
    private lateinit var drawerSearchInput: EditText
    private lateinit var drawerStatusLabel: TextView
    private lateinit var drawerThreadList: LinearLayout
    private lateinit var drawerNetsecPill: TextView
    private lateinit var presetFastBtn: TextView
    private lateinit var presetDirectBtn: TextView
    private lateinit var presetDeepBtn: TextView
    private lateinit var retryBanner: View
    private lateinit var attachmentChip: View
    private lateinit var attachmentChipLabel: TextView
    private lateinit var attachmentChipSub: TextView

    private var cachedDirectCvecAvailable = false
    private var pendingAttachment: UploadedAttachment? = null
    private var attachmentUploading = false
    private var lastRetryReroll = false
    private val drawerSearchHandler = Handler(Looper.getMainLooper())
    private var pendingSearchRunnable: Runnable? = null

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
            abandonTurn()
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
                    magicClient.downloadBuild(name, out)
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

        prefs = getSharedPreferences("magic_prefs", MODE_PRIVATE)
        magicClient = MagicClient(prefs, applicationContext)
        voiceOutput = VoiceOutput(applicationContext, prefs)

        connectionLabel = findViewById(R.id.connection_label)
        tokenUsageLabel = findViewById(R.id.token_usage_label)
        statusDot = findViewById(R.id.status_dot)
        sendButton = findViewById(R.id.send_button)
        hologramFrame = findViewById(R.id.hologram_frame)
        chat = ChatView(findViewById<LinearLayout>(R.id.chat_list), findViewById<ScrollView>(R.id.chat_scroll))
        chat.onEditLastUser = { editLastMessage() }
        chat.onSaveExemplar = { prompt, reply, head, source, onDone ->
            Thread {
                val res = magicClient.saveExemplar(prompt, reply, head, source)
                runOnUiThread { onDone(res.ok, res.totalExemplars, res.duplicate, res.error) }
            }.start()
        }
        if (historyFile.exists()) {
            chat.load(historyFile.readText())
        }
        thinkingStatus = findViewById(R.id.thinking_status)
        approvalCardsContainer = findViewById(R.id.approval_cards_container)
        messageInput = findViewById(R.id.message_input)
        drawerLayout = findViewById(R.id.drawer_layout)
        drawerSearchInput = findViewById(R.id.drawer_search_input)
        drawerStatusLabel = findViewById(R.id.drawer_status_label)
        drawerThreadList = findViewById(R.id.drawer_thread_list)
        drawerNetsecPill = findViewById(R.id.drawer_netsec_pill)
        presetFastBtn = findViewById(R.id.preset_fast_btn)
        presetDirectBtn = findViewById(R.id.preset_direct_btn)
        presetDeepBtn = findViewById(R.id.preset_deep_btn)
        retryBanner = findViewById(R.id.retry_banner)
        attachmentChip = findViewById(R.id.attachment_chip)
        attachmentChipLabel = findViewById(R.id.attachment_chip_label)
        attachmentChipSub = findViewById(R.id.attachment_chip_sub)

        hologramView = findViewById(R.id.hologram_view)
        hologramView.setBackgroundColor(Color.TRANSPARENT)
        hologramView.settings.javaScriptEnabled = true
        hologramView.settings.mediaPlaybackRequiresUserGesture = false
        hologramView.addJavascriptInterface(JsBridge(), "Android")
        hologramView.loadUrl("file:///android_asset/hologram.html")

        messageInput.setOnFocusChangeListener { _, focused -> setHologramSmall(focused) }
        findViewById<View>(R.id.drawer_button).setOnClickListener { openThreadDrawer() }
        findViewById<View>(R.id.search_threads_button).setOnClickListener { openThreadDrawer(focusSearch = true) }
        findViewById<View>(R.id.more_button).setOnClickListener { showMoreMenu(it) }
        findViewById<View>(R.id.builds_button).setOnClickListener {
            startActivity(Intent(this, BuildsActivity::class.java))
        }
        findViewById<View>(R.id.chats_button).setOnClickListener { openThreadDrawer() }
        sendButton.setOnClickListener { if (busy) stopWork() else sendMessage() }
        findViewById<View>(R.id.settings_button).setOnClickListener { openSettings() }
        findViewById<View>(R.id.input_attach_button).setOnClickListener {
            attachLauncher.launch(arrayOf("*/*"))
        }
        findViewById<View>(R.id.attach_button).setOnClickListener {
            attachLauncher.launch(arrayOf("*/*"))
        }
        findViewById<View>(R.id.attachment_chip_clear).setOnClickListener { clearPendingAttachment() }
        retryBanner.setOnClickListener { retryInterruptedStream() }

        setupPresetStrip()
        setupThreadDrawer()

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
        handleIncomingShareIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIncomingShareIntent(intent)
    }

    // "/" autocomplete: type "/" and a list of commands
    // pops above the input; tap one to insert it.
    private var commandHints: List<Pair<String, String>> = emptyList()
    private var slashPopup: ListPopupWindow? = null

    private fun setupSlashAutocomplete() {
        Thread { commandHints = magicClient.commandList() }.start()
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

    /** Stage a picked or shared file as a removable attachment chip by uploading it
     *  to `/attachments/upload` in the background; falls back cleanly to local
     *  extraction only when desktop isn't paired. */
    private fun handleAttachment(uri: android.net.Uri) {
        val name = Attachments.displayName(this, uri)
        val sizeBytes = queryFileSize(uri)
        val isApkHint = name.endsWith(".apk", ignoreCase = true)
        pendingAttachment = null
        attachmentUploading = true
        attachmentChip.visibility = View.VISIBLE
        attachmentChipLabel.text = name
        attachmentChipSub.text = buildString {
            if (sizeBytes > 0L) append(formatByteSize(sizeBytes)).append(" · ")
            append("Uploading…")
            if (isApkHint) append(" · APK → Inspect")
        }

        val hasDesktop = !prefs.getString("host", null).isNullOrEmpty() && prefs.getInt("port", 0) != 0
        if (!hasDesktop) {
            // Away/standalone path: extract locally immediately if text/image/PDF
            if (isApkHint) {
                attachmentUploading = false
                attachmentChipSub.text = "Desktop required to inspect APK"
                return
            }
            attachmentUploading = false
            clearPendingAttachment()
            legacyLocalAttachmentSend(uri, name)
            return
        }

        Thread {
            val (uploaded, err) = magicClient.uploadAttachment(uri, name)
            runOnUiThread {
                attachmentUploading = false
                if (uploaded != null) {
                    pendingAttachment = uploaded
                    attachmentChip.visibility = View.VISIBLE
                    attachmentChipLabel.text = uploaded.filename
                    val apkBadge = if (uploaded.kind == "apk" || uploaded.suggestedAction == "inspect") {
                        " · APK → Inspect"
                    } else {
                        " · Ready"
                    }
                    attachmentChipSub.text = "${formatByteSize(uploaded.sizeBytes)}$apkBadge"
                } else {
                    attachmentChipSub.text = "Upload failed: ${err ?: "error"}"
                    Toast.makeText(this@MainActivity, err ?: "Attachment upload failed", Toast.LENGTH_LONG).show()
                }
            }
        }.start()
    }

    private fun clearPendingAttachment() {
        pendingAttachment = null
        attachmentUploading = false
        attachmentChip.visibility = View.GONE
    }

    private fun queryFileSize(uri: Uri): Long {
        try {
            contentResolver.query(uri, null, null, null, null)?.use { cursor ->
                val idx = cursor.getColumnIndex(OpenableColumns.SIZE)
                if (idx >= 0 && cursor.moveToFirst()) {
                    return cursor.getLong(idx)
                }
            }
        } catch (_: Exception) {}
        return -1L
    }

    private fun formatByteSize(bytes: Long): String = when {
        bytes < 1024L -> "$bytes B"
        bytes < 1024L * 1024L -> String.format(Locale.US, "%.1f KB", bytes / 1024.0)
        else -> String.format(Locale.US, "%.2f MB", bytes / (1024.0 * 1024.0))
    }

    private fun legacyLocalAttachmentSend(uri: Uri, name: String) {
        val question = messageInput.text.toString().trim()
        appendUserTurn(if (question.isEmpty()) "[attached $name]" else "$question  [attached $name]")
        messageInput.setText("")
        voiceOutput.stop()
        thinkingStatus.visibility = View.VISIBLE
        setHologramState("thinking")
        ThinkingKeepAliveService.start(applicationContext)
        val id = beginTurn()
        Thread {
            val extracted = try { Attachments.extract(this, uri) } catch (_: Exception) { null }
            if (extracted == null) {
                runOnUiThread {
                    if (id != turnId) return@runOnUiThread
                    appendSystemTurn("Couldn't read $name.", true)
                    setBusy(false)
                    thinkingStatus.visibility = View.GONE
                    ThinkingKeepAliveService.stop(applicationContext)
                    setHologramState("idle")
                }
                return@Thread
            }
            val result = magicClient.chat(Attachments.buildPrompt(question, extracted), threadId = currentThread)
            runOnUiThread {
                if (id != turnId) return@runOnUiThread
                val sub = if (result.source == "gemini") "(standalone, via Gemini)" else null
                appendAssistantTurn(
                    result.answer, sub,
                    prompt = question.ifBlank { name },
                    head = result.head, source = result.source,
                )
                setBusy(false)
                ThinkingKeepAliveService.stop(applicationContext)
                voiceOutput.speak(result.answer)
                thinkingStatus.visibility = View.GONE
                setHologramState("responding")
                finishHologramResponse()
                updateTokenUsageLabel(result.tokenUsage)
            }
        }.start()
    }

    private fun handleIncomingShareIntent(inIntent: Intent?) {
        val act = inIntent?.action ?: return
        if (act != Intent.ACTION_SEND) return
        val streamUri: Uri? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            inIntent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
        } else {
            @Suppress("DEPRECATION")
            inIntent.getParcelableExtra(Intent.EXTRA_STREAM)
        }
        val sharedText = inIntent.getStringExtra(Intent.EXTRA_TEXT)
        if (!sharedText.isNullOrBlank() && messageInput.text.isNullOrBlank()) {
            messageInput.setText(sharedText)
        }
        if (streamUri != null) {
            handleAttachment(streamUri)
        }
    }

    override fun onResume() {
        super.onResume()
        updateConnectionLabel()
        statusHandler.post(statusPoll)
        refreshPendingApprovals()
        refreshQuickPresets()
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
                val raw = magicClient.fetchStatusRaw()
                runOnUiThread { showStatus(raw) }
            }.start()
            statusHandler.postDelayed(this, 15_000)
        }
    }

    private fun showStatus(raw: String?) {
        if (prefs.getString("host", null) == null) {
            updateConnectionLabel()
            setDot(R.color.magic_offline)
            return
        }
        val json = try { raw?.let { JSONObject(it) } } catch (e: Exception) { null }
        if (json == null) {
            connectionLabel.text = "Offline -- can't reach the desktop"
            setDot(R.color.magic_offline)
            return
        }
        val model = json.optString("primary_model", "Magic")
        val working = json.optBoolean("busy", false) || !json.optBoolean("model_loaded", true)
        connectionLabel.text = "Online  ·  $model" + if (working) "  ·  busy" else ""
        setDot(if (working) R.color.magic_busy else R.color.magic_online)
    }

    private fun setDot(colorId: Int) {
        statusDot.backgroundTintList = ColorStateList.valueOf(ContextCompat.getColor(this, colorId))
    }

    private var busy = false

    // Bumped per request. A late callback from an older request is ignored,
    // so a reply can't land in the chat you've since switched to.
    private var turnId = 0

    /** Starts a request and returns its id. */
    private fun beginTurn(): Int {
        setBusy(true)
        return ++turnId
    }

    private fun abandonTurn() {
        turnId++
        Thread { magicClient.cancelStream() }.start()
        setBusy(false)
        thinkingStatus.visibility = View.GONE
        setHologramState("idle")
    }

    /** The send button doubles as Stop while Magic is working. */
    private fun setBusy(value: Boolean) {
        busy = value
        sendButton.setImageResource(if (value) R.drawable.ic_stop else R.drawable.ic_send)
        sendButton.contentDescription = if (value) "Stop" else "Send"
    }

    private fun stopWork() {
        ThinkingKeepAliveService.stop(applicationContext)
        setHologramState("idle")
        Thread {
            magicClient.cancelStream()
            if (!prefs.getString("admin_token", null).isNullOrBlank()) magicClient.abortJobs()
        }.start()
    }

    /** Shrinks the hologram while typing. Scaled rather than cropped,
     *  because hologram.html draws at a fixed size. */
    private var hologramAnimator: ValueAnimator? = null
    private val hologramIdleHandler = Handler(Looper.getMainLooper())
    private val hologramIdleReset = Runnable {
        hologramView.evaluateJavascript("setState('idle')", null)
    }

    private fun setHologramState(state: String) {
        hologramIdleHandler.removeCallbacks(hologramIdleReset)
        hologramView.evaluateJavascript("setState('$state')", null)
    }

    private fun finishHologramResponse(delayMs: Long = 4500L) {
        hologramIdleHandler.removeCallbacks(hologramIdleReset)
        hologramIdleHandler.postDelayed(hologramIdleReset, delayMs)
    }

    private fun setHologramSmall(small: Boolean) {
        val scale = resources.displayMetrics.density
        val to = ((if (small) 64 else 170) * scale).toInt()
        hologramAnimator?.cancel()
        hologramAnimator = ValueAnimator.ofInt(hologramFrame.height, to).apply {
            duration = 220
            addUpdateListener {
                val height = it.animatedValue as Int
                hologramFrame.layoutParams = hologramFrame.layoutParams.apply { this.height = height }
                val s = (height / (170f * scale)).coerceAtMost(1f)
                hologramView.scaleX = s
                hologramView.scaleY = s
            }
            start()
        }
    }

    private fun showMoreMenu(anchor: View) {
        val menu = PopupMenu(this, anchor)
        try {
            Class.forName("com.gremlin.app.local.LocalExtras")
                .getMethod("addMoreMenu", android.app.Activity::class.java, PopupMenu::class.java)
                .invoke(null, this, menu)
        } catch (_: Exception) {}
        menu.menu.add("Re-roll last answer")
        menu.menu.add("Continue")
        menu.menu.add("Clear chat")
        menu.menu.add("Memory")
        menu.menu.add("Switch model")
        menu.menu.add("Stop running jobs")
        menu.menu.add("Pair with QR code")
        menu.menu.add("Export chat")
        menu.setOnMenuItemClickListener { item ->
            val handledLocal = try {
                Class.forName("com.gremlin.app.local.LocalExtras")
                    .getMethod("handleMoreMenuItem", android.app.Activity::class.java, String::class.java)
                    .invoke(null, this, item.title.toString()) as? Boolean ?: false
            } catch (_: Exception) { false }
            if (handledLocal) return@setOnMenuItemClickListener true
            when (item.title) {
                "Re-roll last answer" -> rerollLast()
                "Continue" -> continueLast()
                "Clear chat" -> clearChatScreenAndServer()
                "Memory" -> startActivity(Intent(this, MemoryActivity::class.java))
                "Switch model" -> pickModel()
                "Stop running jobs" -> Thread {
                    val result = magicClient.abortJobs()
                    runOnUiThread { appendSystemTurn(result.message, !result.ok) }
                }.start()
                "Pair with QR code" -> startQrScan()
                "Export chat" -> exportLauncher.launch("magic-chat-${System.currentTimeMillis()}.txt")
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
            .setTitle("Switch Magic's model")
            .setItems(labels) { _, which ->
                val name = names[which]
                if (name == current) return@setItems
                AlertDialog.Builder(this)
                    .setTitle("Switch to $name?")
                    .setMessage("This changes the model Magic runs on.")
                    .setNegativeButton("Cancel", null)
                    .setPositiveButton("Switch") { _, _ ->
                        Thread {
                            val answer = magicClient.command("/model", "use $name", currentThread)
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
        val hasGeminiKey = !prefs.getString("gemini_key", null).isNullOrBlank()

        connectionLabel.text = when {
            host != null && port != 0 -> "Connecting to $host..."
            hasGeminiKey -> "Standalone mode -- not paired with a desktop"
            else -> "Not paired yet -- open Settings to set up"
        }
    }

    /** Running token total for this whole conversation thread. The desktop
     * computes it from the FULL on-disk transcript (magic_core/history.py),
     * (magic_core/history.py), so it's a real cumulative count, not a
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
            .setPrompt("Scan the pairing code shown by `magic serve`")
            .setBeepEnabled(false)
            .initiateScan()
    }

    // onActivityResult is deprecated in favor of the newer Activity Result
    // API, but it's still the standard, documented integration path for
    override fun onDestroy() {
        super.onDestroy()
        hologramIdleHandler.removeCallbacks(hologramIdleReset)
        ThinkingKeepAliveService.stop(applicationContext)
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
                Toast.makeText(this, "That QR code doesn't look like a Magic pairing code", Toast.LENGTH_LONG).show()
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

    private fun appendAssistantTurn(
        answer: String,
        subStatus: String?,
        prompt: String? = lastUserMessage,
        head: String? = null,
        source: String? = null,
        activities: List<ChatTranscript.ActivityEvent> = emptyList(),
    ) {
        chat.add(Turn(Kind.MAGIC, answer, subStatus, prompt = prompt, head = head, source = source, activities = activities))
        saveHistory()
    }

    private val clearCmdRegex = Regex(
        """^(?:/?(?:chat\s+)?)?(?:clear|clear\s+(?:the\s+|this\s+|our\s+|all\s+)?(?:chat|conversation|history|screen|messages?)|start\s+(?:fresh|over)|new\s+(?:chat|conversation)|forget\s+(?:this|our|the)\s+(?:chat|conversation)|wipe\s+(?:the\s+|this\s+|our\s+)?(?:chat|conversation|history|screen|messages?)|reset\s+(?:the\s+|this\s+)?(?:chat|conversation|screen))[.!?\s]*$""",
        RegexOption.IGNORE_CASE
    )

    private fun isClearCommandText(msg: String): Boolean =
        clearCmdRegex.matches(msg.trim())

    private fun isClearServerReply(ans: String): Boolean {
        val t = ans.trim()
        return t.startsWith("Cleared -- fresh start", ignoreCase = true) ||
            t.equals("Conversation cleared.", ignoreCase = true)
    }

    private fun clearChatScreenAndServer() {
        abandonTurn()
        voiceOutput.stop()
        lastUserMessage = null
        chat.clear()
        try { historyFile.writeText("") } catch (_: Exception) {}
        updateTokenUsageLabel(null)
        appendSystemTurn("Cleared — fresh start.", false)
        val tid = currentThread
        Thread {
            try { magicClient.chat("clear") } catch (_: Exception) {}
            if (tid != null) {
                try { magicClient.command("/chat", "clear", tid) } catch (_: Exception) {}
            }
        }.start()
    }

    private fun sendMessage() {
        if (attachmentUploading) {
            Toast.makeText(this, "Attachment is still uploading…", Toast.LENGTH_SHORT).show()
            return
        }
        val rawText = messageInput.text.toString().trim()
        val att = pendingAttachment
        if (rawText.isEmpty() && att == null) return

        if (att == null && isClearCommandText(rawText)) {
            messageInput.setText("")
            clearChatScreenAndServer()
            return
        }

        if (att == null && rawText.startsWith("/")) {
            messageInput.setText("")
            handleSlashCommand(rawText)
            return
        }

        val message = rawText.ifEmpty {
            if (att != null && (att.kind == "apk" || att.suggestedAction == "inspect")) {
                "Inspect ${att.filename}"
            } else if (att != null) {
                "Examine ${att.filename}"
            } else ""
        }
        val displayUser = if (att != null) {
            if (rawText.isEmpty()) "[attached ${att.filename}]" else "$rawText  [attached ${att.filename}]"
        } else {
            message
        }
        val attIds = listOfNotNull(att?.attachmentId?.takeIf { it.isNotEmpty() })
        clearPendingAttachment()

        appendUserTurn(displayUser)
        messageInput.setText("")
        lastUserMessage = message
        runTurn(message, reroll = false, cont = false, attachmentIds = attIds)
    }

    private fun retryInterruptedStream() {
        if (busy) return
        retryBanner.visibility = View.GONE
        if (lastRetryReroll) {
            rerollLast()
        } else {
            val m = lastUserMessage ?: chat.lastUserText() ?: return
            runTurn(m, reroll = false, cont = false)
        }
    }

    private fun rerollLast(editedMsg: String? = null) {
        if (busy) return
        val m = editedMsg ?: lastUserMessage ?: chat.lastUserText()
        if (m.isNullOrEmpty()) { appendSystemTurn("Nothing to re-roll yet.", false); return }
        lastUserMessage = m
        chat.removeLastAssistant()
        runTurn(m, true)
    }

    private fun continueLast() {
        if (busy) return
        if (chat.lastAssistantText().isNullOrEmpty()) {
            appendSystemTurn("Nothing to continue yet.", false)
            return
        }
        runTurn(
            "(Continue from exactly where you left off. Keep going naturally, without repeating or summarizing what you already said.)",
            reroll = false,
            cont = true,
        )
    }

    private fun editLastMessage() {
        if (busy) return
        val cur = lastUserMessage ?: chat.lastUserText() ?: return
        val et = EditText(this).apply { setText(cur); setSelection(cur.length) }
        AlertDialog.Builder(this)
            .setTitle("Edit your message, then re-roll the reply")
            .setView(et)
            .setPositiveButton("Re-roll") { _, _ ->
                val nt = et.text.toString().trim()
                if (nt.isNotEmpty()) {
                    lastUserMessage = nt
                    chat.updateLastUser(nt)
                    saveHistory()
                    rerollLast(nt)
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun runTurn(
        message: String,
        reroll: Boolean,
        cont: Boolean = false,
        attachmentIds: List<String> = emptyList(),
    ) {
        voiceOutput.stop() // a new message shouldn't have magic still talking over it
        retryBanner.visibility = View.GONE

        setHologramState("thinking")
        thinkingStatus.text = getString(R.string.thinking)
        thinkingStatus.visibility = View.VISIBLE
        ThinkingKeepAliveService.start(applicationContext)
        magicClient.prepareStream()
        val id = beginTurn()
        val reqThread = currentThread

        fun subStatusFor(source: String): String? = when (source) {
            "gemini" -> "(standalone, via Gemini)"
            "stopped" -> "(stopped)"
            "interrupted" -> "(stream interrupted)"
            else -> null
        }

        // First message after a service restart: the model is a ~90s cold
        // read off the desktop's HDD. Probe /status in parallel and, if
        // it's still warming, say so instead of leaving a bare spinner.
        Thread {
            if (magicClient.desktopReadiness() == MagicClient.DesktopReadiness.WARMING) {
                runOnUiThread {
                    if (thinkingStatus.visibility == View.VISIBLE) {
                        thinkingStatus.text = getString(R.string.waking_magic)
                    }
                }
            }
        }.start()

        Thread {
            val started = java.util.concurrent.atomic.AtomicBoolean(false)
            val preActivities = mutableListOf<ChatTranscript.ActivityEvent>()
            magicClient.chatStream(
                message = message,
                reroll = reroll,
                cont = cont,
                threadId = reqThread,
                attachmentIds = attachmentIds,
                onDelta = { delta ->
                    runOnUiThread {
                        if (id != turnId) return@runOnUiThread
                        if (started.compareAndSet(false, true)) {
                            if (cont) chat.beginContinueStream() else chat.beginStream(preActivities)
                            thinkingStatus.visibility = View.GONE
                            setHologramState("responding")
                        }
                        appendAssistantDelta(delta)
                    }
                },
                onActivity = { ev ->
                    runOnUiThread {
                        if (id != turnId) return@runOnUiThread
                        preActivities.add(ev)
                        if (started.get()) {
                            chat.addStreamActivity(ev)
                        } else {
                            thinkingStatus.text = SseParser.formatLivePillText(ev)
                            thinkingStatus.visibility = View.VISIBLE
                        }
                    }
                },
                onDone = { result ->
                    runOnUiThread {
                        if (id != turnId) return@runOnUiThread
                        ThinkingKeepAliveService.stop(applicationContext)
                        val sub = subStatusFor(result.source)
                        val acts = result.activities.ifEmpty { preActivities.toList() }
                        if (result.source == "interrupted") {
                            lastRetryReroll = started.get()
                            if (started.get()) {
                                finishAssistantStreamTurn(
                                    result.answer, sub, lastUserMessage, result.head, result.source, acts
                                )
                            } else if (result.answer.isNotBlank()) {
                                appendAssistantTurn(
                                    result.answer, sub, lastUserMessage, result.head, result.source, acts
                                )
                            }
                            setBusy(false)
                            thinkingStatus.visibility = View.GONE
                            setHologramState("idle")
                            retryBanner.visibility = View.VISIBLE
                            return@runOnUiThread
                        }
                        if (isClearServerReply(result.answer)) {
                            lastUserMessage = null
                            chat.clear()
                            try { historyFile.writeText("") } catch (_: Exception) {}
                            updateTokenUsageLabel(null)
                            appendSystemTurn("Cleared — fresh start.", false)
                        } else if (started.get()) {
                            finishAssistantStreamTurn(
                                result.answer, sub, lastUserMessage, result.head, result.source, acts
                            )
                        } else if (result.source == "stopped") {
                            appendSystemTurn("Stopped.", false)
                        } else if (cont) {
                            chat.appendToLastAssistant(
                                result.answer, sub, lastUserMessage, result.head, result.source, acts
                            )
                            saveHistory()
                        } else {
                            appendAssistantTurn(
                                result.answer, sub, lastUserMessage, result.head, result.source, acts
                            )
                        }
                        setBusy(false)
                        if (result.source != "stopped") {
                            voiceOutput.speak(result.answer)
                            if (!started.get()) setHologramState("responding")
                            finishHologramResponse()
                        } else {
                            setHologramState("idle")
                        }
                        thinkingStatus.visibility = View.GONE
                        updateTokenUsageLabel(result.tokenUsage)
                    }
                },
                onApprovalRequired = { ap ->
                    runOnUiThread {
                        if (id != turnId) return@runOnUiThread
                        thinkingStatus.text = "Waiting for your approval..."
                        thinkingStatus.visibility = View.VISIBLE
                        showApprovalCard(ap)
                    }
                },
                onApprovalResolved = { ev ->
                    runOnUiThread {
                        val aid = ev.optString("approval_id", "")
                        val st = ev.optString("status", "resolved")
                        if (aid.isNotEmpty()) markApprovalResolved(aid, st)
                        if (id == turnId && !started.get()) {
                            thinkingStatus.text = getString(R.string.thinking)
                        }
                    }
                },
            )
        }.start()
    }

    private fun beginAssistantStreamTurn() = chat.beginStream()

    private fun appendAssistantDelta(delta: String) = chat.appendStream(delta)

    /** The done frame's answer wins: it can differ from the streamed text
     *  (reasoning stripped, or a fallback that replaced everything). */
    private fun finishAssistantStreamTurn(
        finalAnswer: String,
        subStatus: String?,
        prompt: String? = lastUserMessage,
        head: String? = null,
        source: String? = null,
        activities: List<ChatTranscript.ActivityEvent> = emptyList(),
    ) {
        chat.finishStream(finalAnswer, subStatus, prompt = prompt, head = head, source = source, activities = activities)
        saveHistory()
    }

    /** Handle explicit typed slash commands (`/deep`, `/builds`, etc.). */
    private fun handleSlashCommand(message: String) {
        if (isClearCommandText(message)) {
            clearChatScreenAndServer()
            return
        }
        appendUserTurn(message)
        val parts = message.trim().split(Regex("\\s+"))
        val cmd = parts.getOrNull(0) ?: ""

        when (cmd) {
            "/deep", "/think" -> {
                val q = message.removePrefix(cmd).trim()
                if (q.isEmpty()) {
                    appendSystemTurn("Usage: $cmd <a hard question> \u2014 runs deep reasoning (or set Reasoning Mode in Settings).", true)
                } else {
                    setBusy(true)
                    setHologramState("thinking")
                    thinkingStatus.text = "Summoning the deep head\u2026 (this takes a few minutes)"
                    thinkingStatus.visibility = View.VISIBLE
                    ThinkingKeepAliveService.start(applicationContext)
                    val id = beginTurn()
                    Thread {
                        val r = magicClient.deep(q)
                        runOnUiThread {
                            if (id != turnId) return@runOnUiThread
                            setBusy(false)
                            thinkingStatus.visibility = View.GONE
                            ThinkingKeepAliveService.stop(applicationContext)
                            val ans = r?.optString("answer").orEmpty()
                            if (ans.isNotEmpty()) {
                                appendAssistantTurn(ans, "deep brain")
                                voiceOutput.speak(ans)
                                finishHologramResponse()
                            } else {
                                appendSystemTurn(r?.optString("error")?.ifEmpty { null }
                                    ?: "The deep head couldn\u2019t answer.", true)
                                setHologramState("idle")
                            }
                        }
                    }.start()
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
                    val id = beginTurn()
                    Thread {
                        val (list, err) = magicClient.listBuilds()
                        runOnUiThread {
                            if (id != turnId) return@runOnUiThread
                            setBusy(false)
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

            // Kept in sync with magic_core/magic/commands.py's COMMANDS
            // dict by hand -- this whitelist is the ONLY thing deciding
            // whether a typed "/whatever" actually reaches the desktop
            // (magicClient.command()) or falls into the "unknown
            // command" branch below and goes nowhere. /override shipped
            // on the desktop 2026-09-13 and sat invisible here for a
            // while because this list wasn't updated alongside it --
            // exactly the silent-failure trap to not repeat: when
            // adding a command in commands.py, add its name here too.
            "/chat", "/skill", "/skills", "/build", "/fix", "/model", "/override",
            "/defense", "/do", "/memory", "/mem", "/good", "/profile", "/inspect", "/re",
            "/forge", "/recipe", "/doctor", "/status", "/health", "/check", "/steer",
            "/act", "/hive", "/help", "/remember", "/reset" -> {
                val args = message.removePrefix(cmd).trim()
                thinkingStatus.visibility = View.VISIBLE
                setHologramState("thinking")
                ThinkingKeepAliveService.start(applicationContext)
                val id = beginTurn()
                Thread {
                    val answer = magicClient.command(cmd, args, currentThread)
                    runOnUiThread {
                        if (id != turnId) return@runOnUiThread
                        ThinkingKeepAliveService.stop(applicationContext)
                        setBusy(false)
                        thinkingStatus.visibility = View.GONE
                        setHologramState("responding")
                        finishHologramResponse()
                        appendAssistantTurn(answer, null)
                        voiceOutput.speak(answer)
                    }
                }.start()
            }

            else -> appendSystemTurn(
                "Commands: /chat /skill /build /fix /model /override /defense /do /memory /good  ·  " +
                    "/builds, /builds get <name>\n\n" +
                    "Or just say it normally — the desktop works out what you mean and asks before anything destructive.",
                true,
            )
        }
    }

    private fun runAdminSlash(call: () -> AdminResult) {
        thinkingStatus.visibility = View.VISIBLE
        val id = beginTurn()
        Thread {
            val result = call()
            runOnUiThread {
                if (id != turnId) return@runOnUiThread
                setBusy(false)
                appendSystemTurn(result.message, !result.ok)
                thinkingStatus.visibility = View.GONE
            }
        }.start()
    }

    private fun refreshPendingApprovals() {
        if (prefs.getString("host", null).isNullOrEmpty()) return
        Thread {
            val (pending, err) = magicClient.getPendingApprovals()
            if (err != null) return@Thread
            runOnUiThread {
                val liveIds = pending.map { it.approvalId }.toSet()
                val staleIds = approvalCards.keys.filter { it !in liveIds }
                for (sid in staleIds) {
                    approvalCards.remove(sid)?.let { approvalCardsContainer.removeView(it) }
                }
                for (ap in pending) {
                    showApprovalCard(ap)
                }
                approvalCardsContainer.visibility = if (approvalCards.isEmpty()) View.GONE else View.VISIBLE
            }
        }.start()
    }

    private fun showApprovalCard(ap: PendingApproval) {
        if (ap.approvalId.isEmpty() || approvalCards.containsKey(ap.approvalId)) return
        val card = LayoutInflater.from(this).inflate(R.layout.view_approval_card, approvalCardsContainer, false)
        val badge = card.findViewById<TextView>(R.id.approval_risk_badge)
        val title = card.findViewById<TextView>(R.id.approval_title)
        val meta = card.findViewById<TextView>(R.id.approval_meta)
        val cmdView = card.findViewById<TextView>(R.id.approval_command)
        val reasonView = card.findViewById<TextView>(R.id.approval_reason)
        val approveBtn = card.findViewById<Button>(R.id.approval_approve_btn)
        val denyBtn = card.findViewById<Button>(R.id.approval_deny_btn)
        val statusView = card.findViewById<TextView>(R.id.approval_status)

        val riskUpper = ap.riskLevel.uppercase(Locale.US).ifEmpty { "MEDIUM" }
        badge.text = riskUpper
        badge.setBackgroundColor(
            ContextCompat.getColor(this, if (riskUpper == "HIGH") R.color.magic_error else R.color.magic_system)
        )
        title.text = ap.promptTitle.ifEmpty { "Approval required" }
        meta.text = listOf(ap.toolName, ap.hookName).filter { it.isNotEmpty() }.joinToString(" · ")
        cmdView.text = ap.commandPreview.ifEmpty { "(no preview)" }
        reasonView.text = ap.reason
        reasonView.visibility = if (ap.reason.isEmpty()) View.GONE else View.VISIBLE

        fun submitDecision(decision: String) {
            approveBtn.isEnabled = false
            denyBtn.isEnabled = false
            statusView.visibility = View.VISIBLE
            statusView.text = if (decision == "approve") "Approving..." else "Denying..."
            Thread {
                val (res, err) = magicClient.resolveApproval(ap.approvalId, decision)
                runOnUiThread {
                    if (res != null) {
                        val st = res.optJSONObject("approval")?.optString("status", decision) ?: decision
                        markApprovalResolved(ap.approvalId, st)
                    } else {
                        approveBtn.isEnabled = true
                        denyBtn.isEnabled = true
                        statusView.text = "Failed: ${err ?: "error"}"
                        Toast.makeText(this@MainActivity, err ?: "Approval request failed", Toast.LENGTH_SHORT).show()
                    }
                }
            }.start()
        }

        approveBtn.setOnClickListener { submitDecision("approve") }
        denyBtn.setOnClickListener { submitDecision("deny") }

        approvalCards[ap.approvalId] = card
        approvalCardsContainer.addView(card)
        approvalCardsContainer.visibility = View.VISIBLE
    }

    private fun markApprovalResolved(approvalId: String, status: String) {
        val card = approvalCards[approvalId] ?: return
        val approveBtn = card.findViewById<Button>(R.id.approval_approve_btn)
        val denyBtn = card.findViewById<Button>(R.id.approval_deny_btn)
        val statusView = card.findViewById<TextView>(R.id.approval_status)
        approveBtn.isEnabled = false
        denyBtn.isEnabled = false
        statusView.visibility = View.VISIBLE
        statusView.text = when (status.lowercase(Locale.US)) {
            "approved", "approve" -> "Approved ✓"
            "denied", "deny" -> "Denied ✕"
            "timeout" -> "Timed out (denied)"
            else -> status
        }
        approvalCardsContainer.postDelayed({
            approvalCards.remove(approvalId)?.let { approvalCardsContainer.removeView(it) }
            if (approvalCards.isEmpty()) approvalCardsContainer.visibility = View.GONE
        }, 700L)
    }

    private fun openThreadSearchDialog() {
        val pad = (16 * resources.displayMetrics.density).toInt()
        val container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad / 2, pad, pad / 2)
        }
        val queryRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = android.view.Gravity.CENTER_VERTICAL
        }
        val queryInput = EditText(this).apply {
            hint = "Search messages & threads..."
            imeOptions = EditorInfo.IME_ACTION_SEARCH
            inputType = android.text.InputType.TYPE_CLASS_TEXT
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        val goBtn = Button(this).apply { text = "Go" }
        queryRow.addView(queryInput)
        queryRow.addView(goBtn)
        container.addView(queryRow)

        val statusTv = TextView(this).apply {
            text = "Type keywords and tap Go"
            textSize = 12f
            setTextColor(ContextCompat.getColor(this@MainActivity, R.color.magic_text_secondary))
            setPadding(0, pad / 3, 0, pad / 3)
        }
        container.addView(statusTv)

        val resultsBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val scroll = ScrollView(this).apply { addView(resultsBox) }
        container.addView(scroll, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, (280 * resources.displayMetrics.density).toInt()))

        val dialog = AlertDialog.Builder(this)
            .setTitle("Search threads")
            .setView(container)
            .setNegativeButton("Close", null)
            .create()

        fun highlightSnippet(raw: String): CharSequence {
            val sb = SpannableStringBuilder()
            val accent = ContextCompat.getColor(this, R.color.magic_accent)
            var idx = 0
            while (idx < raw.length) {
                val open = raw.indexOf('«', idx)
                if (open < 0) {
                    sb.append(raw.substring(idx))
                    break
                }
                sb.append(raw.substring(idx, open))
                val close = raw.indexOf('»', open + 1)
                if (close < 0) {
                    sb.append(raw.substring(open + 1))
                    break
                }
                val startSpan = sb.length
                sb.append(raw.substring(open + 1, close))
                val endSpan = sb.length
                sb.setSpan(ForegroundColorSpan(accent), startSpan, endSpan, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
                sb.setSpan(StyleSpan(android.graphics.Typeface.BOLD), startSpan, endSpan, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
                idx = close + 1
            }
            return sb
        }

        fun runSearch() {
            val q = queryInput.text.toString().trim()
            if (q.isEmpty()) {
                statusTv.text = "Enter a search query first."
                return
            }
            statusTv.text = "Searching..."
            resultsBox.removeAllViews()
            Thread {
                val (hits, err) = magicClient.searchThreads(q, limit = 20)
                runOnUiThread {
                    resultsBox.removeAllViews()
                    if (err != null) {
                        statusTv.text = "Search failed: $err"
                        return@runOnUiThread
                    }
                    statusTv.text = if (hits.isEmpty()) "No matches for \"$q\"" else "${hits.size} match(es)"
                    val fmt = SimpleDateFormat("MMM d HH:mm", Locale.US)
                    for (hit in hits) {
                        val row = LinearLayout(this@MainActivity).apply {
                            orientation = LinearLayout.VERTICAL
                            setPadding(pad / 2, pad / 2, pad / 2, pad / 2)
                            setBackgroundResource(R.drawable.bg_row)
                            val lp = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
                            lp.bottomMargin = (6 * resources.displayMetrics.density).toInt()
                            layoutParams = lp
                        }
                        val ts = if (hit.createdAt > 0.0) fmt.format(Date((hit.createdAt * 1000.0).toLong())) else ""
                        val hdr = TextView(this@MainActivity).apply {
                            text = "${hit.threadId} · ${hit.role.uppercase(Locale.US)}" + if (ts.isNotEmpty()) " · $ts" else ""
                            textSize = 11f
                            setTextColor(ContextCompat.getColor(this@MainActivity, R.color.magic_system))
                        }
                        val snip = TextView(this@MainActivity).apply {
                            text = highlightSnippet(hit.snippet)
                            textSize = 13f
                            setTextColor(ContextCompat.getColor(this@MainActivity, R.color.magic_text_primary))
                        }
                        row.addView(hdr)
                        row.addView(snip)
                        row.setOnClickListener {
                            dialog.dismiss()
                            abandonTurn()
                            chat.clear()
                            historyFile.writeText("")
                            currentThread = if (hit.threadId == "desktop" || hit.threadId.startsWith("default:")) null else hit.threadId
                            updateTokenUsageLabel(null)
                            appendSystemTurn("— Thread ${hit.threadId} (${hit.role}) —\n${hit.snippet}", false)
                        }
                        resultsBox.addView(row)
                    }
                }
            }.start()
        }

        goBtn.setOnClickListener { runSearch() }
        queryInput.setOnEditorActionListener { _, actionId, event ->
            if (actionId == EditorInfo.IME_ACTION_SEARCH ||
                (event != null && event.keyCode == KeyEvent.KEYCODE_ENTER && event.action == KeyEvent.ACTION_DOWN)
            ) {
                runSearch()
                true
            } else {
                false
            }
        }
        dialog.show()
    }

    private fun setupPresetStrip() {
        presetFastBtn.setOnClickListener { applyQuickPreset("fast") }
        presetDirectBtn.setOnClickListener { applyQuickPreset("direct") }
        presetDeepBtn.setOnClickListener { applyQuickPreset("deep") }
    }

    private fun renderActivePreset(thinkMode: String, cvActive: Boolean, cvName: String?) {
        val activeKey = when {
            thinkMode == "off" && !cvActive -> "fast"
            thinkMode == "on" -> "deep"
            cvActive && cvName == "gemma4_debug_direct.cvec.gguf" -> "direct"
            thinkMode == "auto" && cachedDirectCvecAvailable && cvActive -> "direct"
            else -> ""
        }
        val accent = ContextCompat.getColor(this, R.color.magic_accent)
        val secondary = ContextCompat.getColor(this, R.color.magic_text_secondary)
        presetFastBtn.setTextColor(if (activeKey == "fast") accent else secondary)
        presetDirectBtn.setTextColor(if (activeKey == "direct") accent else secondary)
        presetDeepBtn.setTextColor(if (activeKey == "deep") accent else secondary)
    }

    private fun refreshQuickPresets() {
        if (prefs.getString("host", null).isNullOrEmpty() || prefs.getInt("port", 0) == 0) return
        Thread {
            val (json, _) = magicClient.getControls()
            if (json == null) return@Thread
            val think = json.optString("thinking_mode", "auto")
            val cv = json.optJSONObject("control_vector") ?: JSONObject()
            val cvActive = cv.optBoolean("active", false)
            val cvName = cv.optString("name", "")
            val avail = cv.optJSONArray("available_vectors")
            var hasDirect = false
            if (avail != null) {
                for (i in 0 until avail.length()) {
                    if (avail.optString(i) == "gemma4_debug_direct.cvec.gguf") {
                        hasDirect = true
                        break
                    }
                }
            }
            cachedDirectCvecAvailable = hasDirect
            runOnUiThread { renderActivePreset(think, cvActive, cvName) }
        }.start()
    }

    private fun applyQuickPreset(preset: String) {
        if (prefs.getString("host", null).isNullOrEmpty()) {
            Toast.makeText(this, "Pair with a desktop first", Toast.LENGTH_SHORT).show()
            return
        }
        val patch = JSONObject()
        when (preset) {
            "fast" -> {
                patch.put("thinking_mode", "off")
                patch.put("control_vector", JSONObject().put("off", true))
            }
            "direct" -> {
                patch.put("thinking_mode", "auto")
                if (cachedDirectCvecAvailable) {
                    patch.put(
                        "control_vector",
                        JSONObject().put("name", "gemma4_debug_direct.cvec.gguf").put("alpha", 1.15)
                    )
                }
            }
            "deep" -> {
                patch.put("thinking_mode", "on")
            }
        }
        Thread {
            val (res, err) = magicClient.postControls(patch)
            runOnUiThread {
                if (res != null) {
                    val think = res.optString("thinking_mode", "auto")
                    val cv = res.optJSONObject("control_vector") ?: JSONObject()
                    renderActivePreset(think, cv.optBoolean("active", false), cv.optString("name", ""))
                    Toast.makeText(
                        this@MainActivity,
                        "Preset: ${preset.replaceFirstChar { it.uppercase(Locale.US) }}",
                        Toast.LENGTH_SHORT,
                    ).show()
                } else {
                    Toast.makeText(this@MainActivity, err ?: "Preset update failed", Toast.LENGTH_SHORT).show()
                }
            }
        }.start()
    }

    private fun setupThreadDrawer() {
        findViewById<Button>(R.id.drawer_new_chat_btn).setOnClickListener { createNewDrawerChat() }
        drawerNetsecPill.setOnClickListener {
            drawerLayout.closeDrawer(GravityCompat.START)
            val msg = "/defense alerts"
            appendUserTurn(msg)
            handleSlashCommand(msg)
        }
        drawerSearchInput.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {}
            override fun afterTextChanged(s: Editable?) {
                pendingSearchRunnable?.let { drawerSearchHandler.removeCallbacks(it) }
                val q = s?.toString()?.trim().orEmpty()
                val task = Runnable {
                    if (q.isEmpty()) refreshDrawerThreads() else runDrawerSearch(q)
                }
                pendingSearchRunnable = task
                drawerSearchHandler.postDelayed(task, 250L)
            }
        })
        drawerLayout.addDrawerListener(object : DrawerLayout.SimpleDrawerListener() {
            override fun onDrawerOpened(drawerView: View) {
                refreshDrawerNetsecBadge()
                val q = drawerSearchInput.text?.toString()?.trim().orEmpty()
                if (q.isEmpty()) refreshDrawerThreads() else runDrawerSearch(q)
            }
        })
    }

    private fun openThreadDrawer(focusSearch: Boolean = false) {
        drawerLayout.openDrawer(GravityCompat.START)
        if (focusSearch) {
            drawerSearchInput.requestFocus()
        }
    }

    private fun createNewDrawerChat() {
        drawerLayout.closeDrawer(GravityCompat.START)
        if (prefs.getString("host", null).isNullOrEmpty()) {
            currentThread = null
            abandonTurn()
            chat.clear()
            historyFile.writeText("")
            updateTokenUsageLabel(null)
            appendSystemTurn("— New chat —", false)
            return
        }
        Thread {
            val tid = magicClient.newConversation()
            runOnUiThread {
                currentThread = tid
                abandonTurn()
                lastUserMessage = null
                chat.clear()
                historyFile.writeText("")
                updateTokenUsageLabel(null)
                appendSystemTurn("— New chat —", false)
            }
        }.start()
    }

    private fun refreshDrawerNetsecBadge() {
        if (prefs.getString("host", null).isNullOrEmpty()) {
            drawerNetsecPill.visibility = View.GONE
            return
        }
        Thread {
            val summary = magicClient.fetchNetsecSummary()
            runOnUiThread {
                if (summary != null && summary.unackedAlerts > 0) {
                    drawerNetsecPill.text = "Security Alerts (${summary.unackedAlerts})"
                    drawerNetsecPill.visibility = View.VISIBLE
                } else {
                    drawerNetsecPill.visibility = View.GONE
                }
            }
        }.start()
    }

    private fun formatRelativeTime(epochSec: Double): String {
        if (epochSec <= 0.0) return ""
        val diff = ((System.currentTimeMillis() / 1000.0) - epochSec).toLong().coerceAtLeast(0L)
        return when {
            diff < 60L -> "now"
            diff < 3600L -> "${diff / 60L}m"
            diff < 86400L -> "${diff / 3600L}h"
            diff < 7L * 86400L -> "${diff / 86400L}d"
            else -> SimpleDateFormat("MMM d", Locale.US).format(Date((epochSec * 1000.0).toLong()))
        }
    }

    private fun refreshDrawerThreads() {
        drawerStatusLabel.text = "Loading threads…"
        Thread {
            val (items, err) = magicClient.listThreads(limit = 100)
            runOnUiThread {
                drawerThreadList.removeAllViews()
                if (err != null) {
                    drawerStatusLabel.text = "Couldn't load threads: $err"
                    return@runOnUiThread
                }
                if (items.isEmpty()) {
                    drawerStatusLabel.text = "No threads yet — tap + New Chat."
                    return@runOnUiThread
                }
                drawerStatusLabel.text = "${items.size} thread(s)"
                val pad = (10 * resources.displayMetrics.density).toInt()
                for (th in items) {
                    val isActive = (currentThread == th.threadId) ||
                        (currentThread == null && th.threadId.startsWith("default:"))
                    val row = LinearLayout(this@MainActivity).apply {
                        orientation = LinearLayout.HORIZONTAL
                        gravity = Gravity.CENTER_VERTICAL
                        setPadding(pad, pad, pad / 2, pad)
                        setBackgroundResource(if (isActive) R.drawable.bg_pill_chip else R.drawable.bg_row)
                        layoutParams = LinearLayout.LayoutParams(
                            ViewGroup.LayoutParams.MATCH_PARENT,
                            ViewGroup.LayoutParams.WRAP_CONTENT,
                        ).apply { bottomMargin = (6 * resources.displayMetrics.density).toInt() }
                    }
                    val col = LinearLayout(this@MainActivity).apply {
                        orientation = LinearLayout.VERTICAL
                        layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
                    }
                    val pinPrefix = if (th.pinned) "★ " else ""
                    val titleTv = TextView(this@MainActivity).apply {
                        text = "$pinPrefix${th.title}"
                        textSize = 13f
                        setTypeface(null, android.graphics.Typeface.BOLD)
                        maxLines = 1
                        ellipsize = android.text.TextUtils.TruncateAt.END
                        setTextColor(
                            ContextCompat.getColor(
                                this@MainActivity,
                                if (th.pinned) R.color.magic_accent else R.color.magic_text_primary,
                            )
                        )
                    }
                    val rel = formatRelativeTime(th.updatedAt)
                    val metaLine = listOfNotNull(
                        th.preview.takeIf { it.isNotBlank() },
                        rel.takeIf { it.isNotBlank() },
                    ).joinToString(" · ")
                    val subTv = TextView(this@MainActivity).apply {
                        text = metaLine.ifEmpty { "${th.messageCount} messages" }
                        textSize = 11f
                        maxLines = 1
                        ellipsize = android.text.TextUtils.TruncateAt.END
                        setTextColor(ContextCompat.getColor(this@MainActivity, R.color.magic_text_secondary))
                    }
                    col.addView(titleTv)
                    col.addView(subTv)

                    val moreIcon = TextView(this@MainActivity).apply {
                        text = "⋮"
                        textSize = 16f
                        setPadding(pad / 2, pad / 4, pad / 2, pad / 4)
                        setTextColor(ContextCompat.getColor(this@MainActivity, R.color.magic_text_secondary))
                        setOnClickListener { v -> showThreadActionsMenu(v, th) }
                    }

                    row.addView(col)
                    row.addView(moreIcon)
                    row.setOnClickListener { switchToThread(th.threadId, th.title) }
                    row.setOnLongClickListener { v -> showThreadActionsMenu(v, th); true }
                    drawerThreadList.addView(row)
                }
            }
        }.start()
    }

    private fun showThreadActionsMenu(anchor: View, th: ThreadSummary) {
        val menu = PopupMenu(this, anchor)
        menu.menu.add(if (th.pinned) "Unpin" else "Pin")
        menu.menu.add("Rename")
        menu.menu.add("Delete")
        menu.setOnMenuItemClickListener { item ->
            when (item.title) {
                "Pin", "Unpin" -> {
                    val targetPin = !th.pinned
                    Thread {
                        val (_, err) = magicClient.updateThreadMeta(th.threadId, pinned = targetPin)
                        runOnUiThread {
                            if (err == null) refreshDrawerThreads()
                            else Toast.makeText(this, err, Toast.LENGTH_SHORT).show()
                        }
                    }.start()
                }
                "Rename" -> {
                    val et = EditText(this).apply {
                        setText(th.title)
                        setSelection(th.title.length)
                    }
                    AlertDialog.Builder(this)
                        .setTitle("Rename thread")
                        .setView(et)
                        .setPositiveButton("Save") { _, _ ->
                            val newTitle = et.text.toString().trim()
                            if (newTitle.isNotEmpty()) {
                                Thread {
                                    val (_, err) = magicClient.updateThreadMeta(th.threadId, title = newTitle)
                                    runOnUiThread {
                                        if (err == null) refreshDrawerThreads()
                                        else Toast.makeText(this, err, Toast.LENGTH_SHORT).show()
                                    }
                                }.start()
                            }
                        }
                        .setNegativeButton("Cancel", null)
                        .show()
                }
                "Delete" -> {
                    AlertDialog.Builder(this)
                        .setTitle("Delete \"${th.title}\"?")
                        .setMessage("Clears conversation history and search index for this thread.")
                        .setPositiveButton("Delete") { _, _ ->
                            Thread {
                                val (ok, err) = magicClient.deleteThread(th.threadId)
                                runOnUiThread {
                                    if (ok) {
                                        if (currentThread == th.threadId) {
                                            currentThread = null
                                            chat.clear()
                                            historyFile.writeText("")
                                            updateTokenUsageLabel(null)
                                            appendSystemTurn("Thread deleted.", false)
                                        }
                                        refreshDrawerThreads()
                                    } else {
                                        Toast.makeText(this, err ?: "Delete failed", Toast.LENGTH_SHORT).show()
                                    }
                                }
                            }.start()
                        }
                        .setNegativeButton("Cancel", null)
                        .show()
                }
            }
            true
        }
        menu.show()
    }

    private fun runDrawerSearch(q: String) {
        drawerStatusLabel.text = "Searching…"
        Thread {
            val (hits, err) = magicClient.searchThreads(q, limit = 25)
            runOnUiThread {
                drawerThreadList.removeAllViews()
                if (err != null) {
                    drawerStatusLabel.text = "Search failed: $err"
                    return@runOnUiThread
                }
                drawerStatusLabel.text = if (hits.isEmpty()) "No matches for \"$q\"" else "${hits.size} match(es)"
                val pad = (10 * resources.displayMetrics.density).toInt()
                val fmt = SimpleDateFormat("MMM d HH:mm", Locale.US)
                for (hit in hits) {
                    val row = LinearLayout(this@MainActivity).apply {
                        orientation = LinearLayout.VERTICAL
                        setPadding(pad, pad / 2, pad, pad / 2)
                        setBackgroundResource(R.drawable.bg_row)
                        layoutParams = LinearLayout.LayoutParams(
                            ViewGroup.LayoutParams.MATCH_PARENT,
                            ViewGroup.LayoutParams.WRAP_CONTENT,
                        ).apply { bottomMargin = (6 * resources.displayMetrics.density).toInt() }
                    }
                    val ts = if (hit.createdAt > 0.0) fmt.format(Date((hit.createdAt * 1000.0).toLong())) else ""
                    val hdr = TextView(this@MainActivity).apply {
                        text = "${hit.threadId} · ${hit.role.uppercase(Locale.US)}" + if (ts.isNotEmpty()) " · $ts" else ""
                        textSize = 11f
                        setTextColor(ContextCompat.getColor(this@MainActivity, R.color.magic_system))
                    }
                    val snip = TextView(this@MainActivity).apply {
                        text = highlightSearchSnippet(hit.snippet)
                        textSize = 12f
                        setTextColor(ContextCompat.getColor(this@MainActivity, R.color.magic_text_primary))
                    }
                    row.addView(hdr)
                    row.addView(snip)
                    row.setOnClickListener { switchToThread(hit.threadId, hit.threadId) }
                    drawerThreadList.addView(row)
                }
            }
        }.start()
    }

    private fun switchToThread(rawTid: String, displayTitle: String) {
        drawerLayout.closeDrawer(GravityCompat.START)
        abandonTurn()
        retryBanner.visibility = View.GONE
        currentThread = if (rawTid == "desktop" || rawTid.startsWith("default:")) null else rawTid
        updateTokenUsageLabel(null)
        chat.clear()
        appendSystemTurn("Loading $displayTitle…", false)
        Thread {
            val (msgs, err) = magicClient.getThreadMessages(rawTid, limit = 120)
            runOnUiThread {
                if (err != null || msgs.isEmpty()) {
                    chat.clear()
                    appendSystemTurn("— $displayTitle —", false)
                    saveHistory()
                    return@runOnUiThread
                }
                val loadedTurns = msgs.map { m ->
                    Turn(
                        kind = if (m.role == "user") Kind.USER else Kind.MAGIC,
                        text = m.content,
                    )
                }
                chat.loadTurns(loadedTurns)
                lastUserMessage = chat.lastUserText()
                saveHistory()
            }
        }.start()
    }

    private fun highlightSearchSnippet(raw: String): CharSequence {
        val sb = SpannableStringBuilder()
        val accent = ContextCompat.getColor(this, R.color.magic_accent)
        var idx = 0
        while (idx < raw.length) {
            val open = raw.indexOf('«', idx)
            if (open < 0) {
                sb.append(raw.substring(idx))
                break
            }
            sb.append(raw.substring(idx, open))
            val close = raw.indexOf('»', open + 1)
            if (close < 0) {
                sb.append(raw.substring(open + 1))
                break
            }
            val startSpan = sb.length
            sb.append(raw.substring(open + 1, close))
            val endSpan = sb.length
            sb.setSpan(ForegroundColorSpan(accent), startSpan, endSpan, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            sb.setSpan(StyleSpan(android.graphics.Typeface.BOLD), startSpan, endSpan, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            idx = close + 1
        }
        return sb
    }

    private fun appendSystemTurn(text: String, isError: Boolean) {
        chat.add(Turn(Kind.SYSTEM, if (isError) "! $text" else text))
        saveHistory()
    }
}
