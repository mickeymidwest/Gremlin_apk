package com.gremlin.app

import android.content.Context
import android.content.SharedPreferences
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.OutputStream
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * Full-app behavior: at home, the desktop's whole orchestrator (all
 * local models, consult, everything) is one fast LAN hop away, so use
 * it. Away from home, that's not reachable at all -- fall back to
 * calling Claude or Gemini directly with the phone's own stored API
 * keys, in the same persona voice cached from the last time the
 * desktop was reachable. This deliberately does NOT reimplement the
 * router/persona/consult machinery in Kotlin -- that logic stays in
 * one place (gremlin_core), and the phone either borrows it over the
 * network or falls back to a much simpler direct call.
 */
data class ChatResult(val answer: String, val source: String)

/** One project Gremlin has built on the desktop (build_project ->
 * ~/Downloads/<name>/), as listed by GET /builds. */
data class BuildInfo(
    val name: String,
    val goal: String,
    val sizeBytes: Long,
    val fileCount: Int,
    val tooBig: Boolean,
)

/** Result of an admin-token-gated call (slash commands in
 * MainActivity) -- deliberately the same (ok, message) shape for
 * /root, /snapshots, and /rollback so sendMessage() can render all
 * three through one appendSystemTurn call. */
data class AdminResult(val ok: Boolean, val message: String)

class GremlinClient(private val prefs: SharedPreferences, private val appContext: Context) {

    // Short connect timeout for the desktop attempt -- on the home LAN
    // this connects almost instantly, so it costs nothing there. Away
    // from home it means falling back quickly instead of hanging.
    private val desktopConnectTimeoutMs = 4_000
    // Once CONNECTED, give the desktop real time to answer before giving
    // up and falling back to away-mode. A desktop turn can legitimately
    // run minutes: intent classification, a model swap on an 8GB card, a
    // consult across several models, or a natural-language self-edit.
    // Timing out at 2 minutes was the main reason a paired phone kept
    // falling back to away-mode mid-conversation and answering with a
    // different voice. This only extends the wait when the desktop is
    // actually working -- an unreachable desktop still fails fast at
    // connect (desktopConnectTimeoutMs above).
    private val desktopReadTimeoutMs = 300_000

    // Away-mode exchanges the desktop doesn't know about yet -- queued
    // here, sent along with the next message that actually reaches the
    // desktop, then cleared only once the desktop confirms it got them.
    // Never cleared on a failed send, so a dropped connection mid-sync
    // just means it tries again next time rather than losing anything.
    private val pendingSyncFile: File by lazy { File(appContext.filesDir, "pending_sync.jsonl") }

    private fun appendPendingSync(prompt: String, answer: String, source: String) {
        try {
            val entry = JSONObject().apply {
                put("prompt", prompt)
                put("answer", answer)
                put("source", source)
                put("timestamp", System.currentTimeMillis() / 1000.0)
            }
            pendingSyncFile.appendText(entry.toString() + "\n")
        } catch (e: Exception) {
            // Best-effort -- losing a queued sync entry isn't fatal, the
            // away-mode answer itself already succeeded and was shown.
        }
    }

    private fun readPendingSync(): JSONArray {
        val arr = JSONArray()
        if (!pendingSyncFile.exists()) return arr
        try {
            pendingSyncFile.readLines().forEach { line ->
                if (line.isNotBlank()) arr.put(JSONObject(line))
            }
        } catch (e: Exception) {
            // Malformed queue file -- better to skip syncing this round
            // than crash the whole chat call over stale local data.
        }
        return arr
    }

    private fun clearPendingSync() {
        try {
            pendingSyncFile.delete()
        } catch (e: Exception) {
        }
    }

    private fun hasAnyNetwork(): Boolean {
        val cm = appContext.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager ?: return true
        val network = cm.activeNetwork ?: return false
        val capabilities = cm.getNetworkCapabilities(network) ?: return false
        return capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
    }

    fun chat(message: String): ChatResult {
        val host = prefs.getString("host", null)
        val port = prefs.getInt("port", 0)
        val token = prefs.getString("token", null)

        // Skip straight to away-mode when there's clearly no network at
        // all, rather than waiting out a connect timeout for nothing.
        if (hasAnyNetwork() && host != null && port != 0 && token != null) {
            try {
                val pending = readPendingSync()
                val answer = postToDesktop(host, port, token, message, pending)
                if (pending.length() > 0) clearPendingSync() // only after the server actually got them
                refreshCachedPersonaVoice(host, port, token) // best-effort, keeps away-mode voice current
                return ChatResult(answer, "desktop")
            } catch (e: Exception) {
                // Desktop configured but unreachable -- fall through to away-mode.
            }
        }

        val result = chatAway(message)
        if (result.source == "claude" || result.source == "gemini") {
            appendPendingSync(message, result.answer, result.source)
        }
        return result
    }

    /**
     * Streams the desktop's POST /chat/stream (Server-Sent Events).
     * `onDelta` fires on this background thread for each token chunk;
     * `onDone` fires once with the final (answer, source). Anything that
     * isn't a reachable desktop with a clean stream -- no network, not
     * paired, pending away-mode sync to flush, a non-2xx response, a
     * parse error, an old server with no /chat/stream -- falls back to
     * the ordinary blocking chat() and delivers the whole answer through
     * onDone with no deltas. So the caller can always rely on exactly one
     * onDone; onDelta is best-effort progress.
     */
    fun chatStream(message: String, onDelta: (String) -> Unit, onDone: (ChatResult) -> Unit) {
        val host = prefs.getString("host", null)
        val port = prefs.getInt("port", 0)
        val token = prefs.getString("token", null)

        if (!hasAnyNetwork() || host == null || port == 0 || token == null) {
            onDone(chat(message)); return
        }
        // A queued away-mode exchange needs the pending_sync ride-along
        // that only the plain /chat path does -- take it this turn.
        if (readPendingSync().length() > 0) {
            onDone(chat(message)); return
        }

        var connection: HttpURLConnection? = null
        try {
            val url = URL("http://$host:$port/chat/stream")
            connection = (url.openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                setRequestProperty("Content-Type", "application/json")
                setRequestProperty("Authorization", "Bearer $token")
                setRequestProperty("Accept", "text/event-stream")
                doOutput = true
                connectTimeout = desktopConnectTimeoutMs
                readTimeout = desktopReadTimeoutMs
            }
            OutputStreamWriter(connection.outputStream).use {
                it.write(JSONObject().put("message", message).put("token", token).toString())
            }
            if (connection.responseCode !in 200..299) {
                connection.disconnect(); connection = null
                onDone(chat(message)); return
            }

            var answer = StringBuilder()
            var source = "desktop"
            connection.inputStream.bufferedReader().useLines { lines ->
                for (line in lines) {
                    if (!line.startsWith("data:")) continue
                    val payload = line.substring(5).trim()
                    if (payload.isEmpty()) continue
                    val obj = try { JSONObject(payload) } catch (e: Exception) { continue }
                    when (obj.optString("type")) {
                        "delta" -> {
                            val t = obj.optString("text")
                            if (t.isNotEmpty()) { answer.append(t); onDelta(t) }
                        }
                        "done" -> {
                            val full = obj.optString("answer", answer.toString())
                            answer = StringBuilder(full)
                            val s = obj.optString("source", "")
                            if (s.isNotEmpty() && s != "gremlin") source = s
                        }
                    }
                }
            }
            connection.disconnect(); connection = null
            refreshCachedPersonaVoice(host, port, token) // keep away-mode voice current, same as chat()
            onDone(ChatResult(answer.toString(), source))
        } catch (e: Exception) {
            try { connection?.disconnect() } catch (_: Exception) {}
            onDone(chat(message)) // any streaming trouble -> the reliable path
        }
    }

    private fun chatAway(message: String): ChatResult {
        val personaPrompt = prefs.getString("cached_persona_prompt", "") ?: ""

        if (!hasAnyNetwork()) {
            return ChatResult(
                "No network connection right now. Connect to Wi-Fi or mobile data to reach the desktop or Claude/Gemini.",
                "no-network",
            )
        }

        val anthropicKey = prefs.getString("anthropic_key", null)
        val geminiKey = prefs.getString("gemini_key", null)
        val preferred = prefs.getString("away_preferred", "claude")

        val order = if (preferred == "gemini") listOf("gemini", "claude") else listOf("claude", "gemini")
        val errors = mutableListOf<String>()

        for (provider in order) {
            try {
                when (provider) {
                    "claude" -> if (!anthropicKey.isNullOrBlank()) {
                        return ChatResult(callClaude(anthropicKey, personaPrompt, message), "claude")
                    }
                    "gemini" -> if (!geminiKey.isNullOrBlank()) {
                        return ChatResult(callGemini(geminiKey, personaPrompt, message), "gemini")
                    }
                }
            } catch (e: Exception) {
                errors.add("$provider: ${e.message}")
            }
        }

        return if (anthropicKey.isNullOrBlank() && geminiKey.isNullOrBlank()) {
            ChatResult(
                "Can't reach the desktop and no API keys are set up. " +
                "Connect to your home Wi-Fi, or add a Claude/Gemini API key in Settings.",
                "none-configured",
            )
        } else {
            ChatResult("Couldn't get an answer from anything: ${errors.joinToString("; ")}", "error")
        }
    }

    /**
     * Fetches the full /status body (not just system_prompt, unlike
     * refreshCachedPersonaVoice) and caches it in prefs -- this is what
     * the hologram's getStatusJson() bridge call reads to label its 4
     * head-slots, and what ModelSettingsActivity reads to show a
     * model's current field values. Best-effort: returns null and
     * leaves any previously cached value in place on failure, same
     * "stale is better than blank" approach as the persona-voice cache.
     */
    fun fetchStatusRaw(): String? {
        val host = prefs.getString("host", null)
        val port = prefs.getInt("port", 0)
        val token = prefs.getString("token", null)
        if (host == null || port == 0 || token == null) return null

        return try {
            val url = URL("http://$host:$port/status")
            val connection = url.openConnection() as HttpURLConnection
            connection.requestMethod = "GET"
            connection.setRequestProperty("Authorization", "Bearer $token")
            connection.connectTimeout = 4_000
            connection.readTimeout = 8_000
            val text = connection.inputStream.bufferedReader().use { it.readText() }
            prefs.edit().putString("cached_status_json", text).apply()
            text
        } catch (e: Exception) {
            null
        }
    }

    /** Quick liveness probe for the "waking Gremlin" hint. Returns:
     *  READY   -- desktop reachable and the model is resident
     *  WARMING -- desktop reachable but the model isn't loaded yet
     *             (first message after a service restart -- a ~90s cold
     *             read of the GGUF off the HDD)
     *  UNKNOWN -- not paired, or the desktop didn't answer in time
     *             (don't show the hint; the normal path handles it) */
    enum class DesktopReadiness { READY, WARMING, UNKNOWN }

    fun desktopReadiness(): DesktopReadiness {
        val host = prefs.getString("host", null)
        val port = prefs.getInt("port", 0)
        val token = prefs.getString("token", null)
        if (host == null || port == 0 || token == null) return DesktopReadiness.UNKNOWN
        return try {
            val url = URL("http://$host:$port/status")
            val c = url.openConnection() as HttpURLConnection
            c.requestMethod = "GET"
            c.setRequestProperty("Authorization", "Bearer $token")
            c.connectTimeout = 3_000
            c.readTimeout = 6_000
            val json = JSONObject(c.inputStream.bufferedReader().use { it.readText() })
            if (json.optBoolean("model_loaded", true)) DesktopReadiness.READY
            else DesktopReadiness.WARMING
        } catch (e: Exception) {
            DesktopReadiness.UNKNOWN
        }
    }

    private fun refreshCachedPersonaVoice(host: String, port: Int, token: String) {
        try {
            val url = URL("http://$host:$port/status")
            val connection = url.openConnection() as HttpURLConnection
            connection.requestMethod = "GET"
            connection.setRequestProperty("Authorization", "Bearer $token")
            connection.connectTimeout = 3_000
            connection.readTimeout = 5_000
            val text = connection.inputStream.bufferedReader().use { it.readText() }
            val prompt = JSONObject(text).optString("system_prompt", "")
            prefs.edit().putString("cached_persona_prompt", prompt).apply()
        } catch (e: Exception) {
            // best-effort only -- an away-mode chat still works with
            // whatever was cached last, or with no persona flavor at all
        }
    }

    /** Backs the `/desktop <command>` and `/root <command>` slash
     * commands -- the only difference is `as_root`, which routes
     * through root_exec.run_as_root on the desktop (cached local sudo
     * password, never sent from the phone) instead of the plain
     * sandbox. Same admin-token gating as the Settings screen's
     * existing admin command box either way. */
    fun runCommand(command: String, asRoot: Boolean): AdminResult {
        val (host, port, adminToken) = adminCreds() ?: return AdminResult(false, adminCredsError())
        return try {
            val url = URL("http://$host:$port/admin/execute")
            val connection = url.openConnection() as HttpURLConnection
            connection.requestMethod = "POST"
            connection.setRequestProperty("Content-Type", "application/json")
            connection.setRequestProperty("X-Admin-Token", adminToken)
            connection.doOutput = true
            connection.connectTimeout = 8_000
            connection.readTimeout = 130_000

            val body = JSONObject().apply { put("command", command); put("as_root", asRoot) }
            OutputStreamWriter(connection.outputStream).use { it.write(body.toString()) }

            val responseCode = connection.responseCode
            val stream = if (responseCode in 200..299) connection.inputStream else connection.errorStream
            val json = JSONObject(stream.bufferedReader().use { it.readText() })

            if (responseCode !in 200..299) {
                AdminResult(false, json.optString("error", "HTTP $responseCode"))
            } else {
                val text = "exit ${json.optInt("exit_code")}\n${json.optString("stdout")}\n${json.optString("stderr")}".trim()
                AdminResult(json.optBoolean("ok"), text)
            }
        } catch (e: Exception) {
            AdminResult(false, "Couldn't reach desktop: ${e.message}")
        }
    }

    /** Backs the `/reboot confirm` slash command -- same endpoint and
     * NOPASSWD-scoped sudoers rule as Settings' existing "Reboot
     * Desktop" button, just reachable from the chat input too. */
    fun reboot(): AdminResult {
        val (host, port, adminToken) = adminCreds() ?: return AdminResult(false, adminCredsError())
        return try {
            val url = URL("http://$host:$port/admin/reboot")
            val connection = url.openConnection() as HttpURLConnection
            connection.requestMethod = "POST"
            connection.setRequestProperty("Content-Type", "application/json")
            connection.setRequestProperty("X-Admin-Token", adminToken)
            connection.doOutput = true
            connection.connectTimeout = 8_000
            connection.readTimeout = 15_000
            OutputStreamWriter(connection.outputStream).use { it.write("{}") }
            val responseCode = connection.responseCode
            if (responseCode in 200..299) {
                AdminResult(true, "Reboot triggered -- it should come back up and reconnect on its own if auto-start is set up.")
            } else {
                AdminResult(false, "Reboot failed (HTTP $responseCode)")
            }
        } catch (e: Exception) {
            // A connection drop here is actually the expected/good
            // outcome once the reboot really starts -- don't treat every
            // exception as a failure worth alarming over (same reasoning
            // as SettingsActivity.triggerReboot).
            AdminResult(true, "Reboot request sent.")
        }
    }

    /** Backs the `/updatecheck` slash command -- regular (non-admin) auth,
     * since this only reads pending package names + a public forum
     * thread, same as /status or /chat. */
    fun checkUpdates(): AdminResult {
        val host = prefs.getString("host", null)
        val port = prefs.getInt("port", 0)
        val token = prefs.getString("token", null)
        if (host == null || port == 0 || token == null) {
            return AdminResult(false, "Not paired with a desktop")
        }
        return try {
            val url = URL("http://$host:$port/update-check")
            val connection = url.openConnection() as HttpURLConnection
            connection.requestMethod = "GET"
            connection.setRequestProperty("Authorization", "Bearer $token")
            connection.connectTimeout = 8_000
            connection.readTimeout = 30_000

            val responseCode = connection.responseCode
            val stream = if (responseCode in 200..299) connection.inputStream else connection.errorStream
            val json = JSONObject(stream.bufferedReader().use { it.readText() })

            if (responseCode !in 200..299 || !json.optBoolean("ok")) {
                AdminResult(false, json.optString("error", "HTTP $responseCode"))
            } else {
                AdminResult(true, json.optString("summary"))
            }
        } catch (e: Exception) {
            AdminResult(false, "Couldn't reach desktop: ${e.message}")
        }
    }

    /** Lists the projects Gremlin has built on the desktop. Regular
     * token, read-only, home-only (the desktop's ~/Downloads isn't
     * reachable away from home). Returns (builds, errorOrNull). */
    /** POST /command -- runs a Magic command (/chat /skill /build /fix
     *  /model) on the desktop and returns its answer text. The desktop
     *  does the work; this is just the messenger. `thread` (optional)
     *  keeps this in one named conversation. */
    fun command(cmd: String, args: String, thread: String? = null): String {
        val host = prefs.getString("host", null)
        val port = prefs.getInt("port", 0)
        val token = prefs.getString("token", null)
        if (host == null || port == 0 || token == null) return "Not paired with a desktop."
        return try {
            val url = URL("http://$host:$port/command")
            val c = url.openConnection() as HttpURLConnection
            c.requestMethod = "POST"
            c.doOutput = true
            c.setRequestProperty("Authorization", "Bearer $token")
            c.setRequestProperty("Content-Type", "application/json")
            c.connectTimeout = 8_000
            c.readTimeout = 480_000
            val body = JSONObject().put("cmd", cmd.trim().removePrefix("/")).put("args", args.trim())
            if (thread != null) body.put("thread", thread)
            c.outputStream.use { it.write(body.toString().toByteArray()) }
            val code = c.responseCode
            val stream = if (code in 200..299) c.inputStream else c.errorStream
            val json = JSONObject(stream.bufferedReader().use { it.readText() })
            json.optString("answer", json.optString("error", "no response"))
        } catch (e: Exception) {
            "Couldn't reach desktop: ${e.message}"
        }
    }

    data class ConversationInfo(val id: String, val title: String, val updated: Double)

    /** GET /conversations -- the recent-conversations list. */
    fun listConversations(): List<ConversationInfo> {
        val host = prefs.getString("host", null) ?: return emptyList()
        val port = prefs.getInt("port", 0)
        val token = prefs.getString("token", null) ?: return emptyList()
        return try {
            val c = (URL("http://$host:$port/conversations").openConnection() as HttpURLConnection)
            c.setRequestProperty("Authorization", "Bearer $token")
            c.connectTimeout = 5_000; c.readTimeout = 8_000
            val arr = JSONObject(c.inputStream.bufferedReader().use { it.readText() })
                .optJSONArray("conversations") ?: JSONArray()
            (0 until arr.length()).map {
                val o = arr.getJSONObject(it)
                ConversationInfo(o.optString("id"), o.optString("title", "chat"), o.optDouble("updated"))
            }
        } catch (e: Exception) { emptyList() }
    }

    /** POST /conversations -- start a fresh thread, returns its id. */
    fun newConversation(): String? {
        val host = prefs.getString("host", null) ?: return null
        val port = prefs.getInt("port", 0)
        val token = prefs.getString("token", null) ?: return null
        return try {
            val c = (URL("http://$host:$port/conversations").openConnection() as HttpURLConnection)
            c.requestMethod = "POST"; c.doOutput = true
            c.setRequestProperty("Authorization", "Bearer $token")
            c.setRequestProperty("Content-Type", "application/json")
            c.outputStream.use { it.write("{}".toByteArray()) }
            JSONObject(c.inputStream.bufferedReader().use { it.readText() }).optString("thread").ifEmpty { null }
        } catch (e: Exception) { null }
    }

    /** GET the command list (name + help) for the "/" autocomplete.
     *  Best-effort -- returns the built-in list if the desktop is
     *  unreachable so autocomplete still works offline. */
    fun commandList(): List<Pair<String, String>> {
        val builtin = listOf(
            "chat" to "Talk to Gremlin",
            "skill" to "Add or improve a Magic skill",
            "build" to "Build a script / project / app on the desktop",
            "fix" to "Gremlin improves its own harness",
            "model" to "Pick / inspect the base model",
            "builds" to "List / fetch desktop builds",
            "claude" to "Hand a problem to a Claude Code session",
        )
        val host = prefs.getString("host", null) ?: return builtin
        val port = prefs.getInt("port", 0)
        val token = prefs.getString("token", null) ?: return builtin
        return try {
            val url = URL("http://$host:$port/command")
            val c = url.openConnection() as HttpURLConnection
            c.requestMethod = "POST"
            c.doOutput = true
            c.setRequestProperty("Authorization", "Bearer $token")
            c.setRequestProperty("Content-Type", "application/json")
            c.connectTimeout = 5_000
            c.readTimeout = 5_000
            c.outputStream.use { it.write(JSONObject().put("cmd", "").toString().toByteArray()) }
            val json = JSONObject(c.inputStream.bufferedReader().use { it.readText() })
            val arr = json.optJSONArray("commands") ?: return builtin
            val out = ArrayList<Pair<String, String>>()
            for (i in 0 until arr.length()) {
                val o = arr.getJSONObject(i)
                out.add(o.optString("name") to o.optString("help"))
            }
            (out + builtin.filter { b -> out.none { it.first == b.first } })
        } catch (e: Exception) {
            builtin
        }
    }

    fun listBuilds(): Pair<List<BuildInfo>, String?> {
        val host = prefs.getString("host", null)
        val port = prefs.getInt("port", 0)
        val token = prefs.getString("token", null)
        if (host == null || port == 0 || token == null) {
            return Pair(emptyList(), "Not paired with a desktop")
        }
        return try {
            val url = URL("http://$host:$port/builds")
            val connection = url.openConnection() as HttpURLConnection
            connection.requestMethod = "GET"
            connection.setRequestProperty("Authorization", "Bearer $token")
            connection.connectTimeout = 8_000
            connection.readTimeout = 15_000

            val code = connection.responseCode
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            val json = JSONObject(stream.bufferedReader().use { it.readText() })
            if (code !in 200..299) {
                return Pair(emptyList(), json.optString("error", "HTTP $code"))
            }
            val arr = json.optJSONArray("builds") ?: JSONArray()
            val out = ArrayList<BuildInfo>(arr.length())
            for (i in 0 until arr.length()) {
                val o = arr.getJSONObject(i)
                out.add(
                    BuildInfo(
                        name = o.optString("name"),
                        goal = o.optString("goal"),
                        sizeBytes = o.optLong("size_bytes"),
                        fileCount = o.optInt("file_count"),
                        tooBig = o.optBoolean("too_big"),
                    )
                )
            }
            Pair(out, null)
        } catch (e: Exception) {
            Pair(emptyList(), "Couldn't reach desktop: ${e.message}")
        }
    }

    /** Downloads one build's .zip and streams it into `dest` (e.g. a
     * Storage Access Framework OutputStream the user picked). Returns an
     * error string, or null on success. Caller owns/closes `dest`. */
    fun downloadBuild(name: String, dest: OutputStream): String? {
        val host = prefs.getString("host", null)
        val port = prefs.getInt("port", 0)
        val token = prefs.getString("token", null)
        if (host == null || port == 0 || token == null) return "Not paired with a desktop"
        return try {
            val safe = URLEncoder.encode(name, "UTF-8")
            val url = URL("http://$host:$port/builds/$safe")
            val connection = url.openConnection() as HttpURLConnection
            connection.requestMethod = "GET"
            connection.setRequestProperty("Authorization", "Bearer $token")
            connection.connectTimeout = 8_000
            connection.readTimeout = 60_000

            val code = connection.responseCode
            if (code !in 200..299) {
                val err = connection.errorStream?.bufferedReader()?.use { it.readText() }.orEmpty()
                return try {
                    JSONObject(err).optString("error", "HTTP $code")
                } catch (_: Exception) {
                    "HTTP $code"
                }
            }
            connection.inputStream.use { input -> input.copyTo(dest) }
            null
        } catch (e: Exception) {
            "Download failed: ${e.message}"
        }
    }

    /** Backs the `/claude <problem> confirm` slash command -- runs the
     * `claude` CLI non-interactively on the desktop with full autonomy
     * (--dangerously-skip-permissions), gated by the admin token plus
     * the app's own required "confirm" step. A real Claude Code session
     * doing actual work can run a while, hence the long read timeout --
     * matches claude_override.py's DEFAULT_TIMEOUT (600s) plus headroom. */
    fun claudeOverride(prompt: String): AdminResult {
        val (host, port, adminToken) = adminCreds() ?: return AdminResult(false, adminCredsError())
        return try {
            val url = URL("http://$host:$port/admin/claude-override")
            val connection = url.openConnection() as HttpURLConnection
            connection.requestMethod = "POST"
            connection.setRequestProperty("Content-Type", "application/json")
            connection.setRequestProperty("X-Admin-Token", adminToken)
            connection.doOutput = true
            connection.connectTimeout = 8_000
            connection.readTimeout = 630_000

            val body = JSONObject().apply { put("prompt", prompt) }
            OutputStreamWriter(connection.outputStream).use { it.write(body.toString()) }

            val responseCode = connection.responseCode
            val stream = if (responseCode in 200..299) connection.inputStream else connection.errorStream
            val json = JSONObject(stream.bufferedReader().use { it.readText() })

            val ok = responseCode in 200..299 && json.optBoolean("ok")
            AdminResult(ok, json.optString(if (ok) "result" else "error", "HTTP $responseCode"))
        } catch (e: Exception) {
            AdminResult(false, "Couldn't reach desktop: ${e.message}")
        }
    }

    /** Backs the `/fix <path> <problem> confirm` slash command -- Gremlin
     * fixing something on the desktop that ISN'T its own code, using its
     * own registered models end to end (not the separate `claude` CLI --
     * see claudeOverride() for that). Long timeout for the same reason
     * as claudeOverride: this involves real model generation plus a
     * compile/verify check, not an instant response. */
    fun scriptFix(path: String, problem: String, verifyCommand: String? = null): AdminResult {
        val (host, port, adminToken) = adminCreds() ?: return AdminResult(false, adminCredsError())
        return try {
            val url = URL("http://$host:$port/admin/script-edit")
            val connection = url.openConnection() as HttpURLConnection
            connection.requestMethod = "POST"
            connection.setRequestProperty("Content-Type", "application/json")
            connection.setRequestProperty("X-Admin-Token", adminToken)
            connection.doOutput = true
            connection.connectTimeout = 8_000
            connection.readTimeout = 310_000

            val body = JSONObject().apply {
                put("path", path)
                put("problem", problem)
                if (!verifyCommand.isNullOrBlank()) put("verify_command", verifyCommand)
            }
            OutputStreamWriter(connection.outputStream).use { it.write(body.toString()) }

            val responseCode = connection.responseCode
            val stream = if (responseCode in 200..299) connection.inputStream else connection.errorStream
            val json = JSONObject(stream.bufferedReader().use { it.readText() })

            if (responseCode !in 200..299 || !json.optBoolean("ok")) {
                AdminResult(false, json.optString("error", "HTTP $responseCode"))
            } else if (json.optBoolean("applied", false)) {
                AdminResult(true, "Applied. Backup: ${json.optString("backup_path")}\n\n${json.optString("diff")}")
            } else {
                AdminResult(true, json.optString("reason", json.optString("message", "No changes applied.")))
            }
        } catch (e: Exception) {
            AdminResult(false, "Couldn't reach desktop: ${e.message}")
        }
    }

    /** Backs the `/snapshots` slash command. */
    fun listSnapshots(): AdminResult {
        val (host, port, adminToken) = adminCreds() ?: return AdminResult(false, adminCredsError())
        return try {
            val url = URL("http://$host:$port/admin/snapshots")
            val connection = url.openConnection() as HttpURLConnection
            connection.requestMethod = "GET"
            connection.setRequestProperty("X-Admin-Token", adminToken)
            connection.connectTimeout = 8_000
            connection.readTimeout = 30_000

            val responseCode = connection.responseCode
            val stream = if (responseCode in 200..299) connection.inputStream else connection.errorStream
            val json = JSONObject(stream.bufferedReader().use { it.readText() })

            if (responseCode !in 200..299 || !json.optBoolean("ok")) {
                AdminResult(false, json.optString("error", "HTTP $responseCode"))
            } else {
                val snapshots = json.optJSONArray("snapshots")
                if (snapshots == null || snapshots.length() == 0) {
                    AdminResult(true, "No snapshots found.")
                } else {
                    val lines = (0 until snapshots.length()).joinToString("\n") { i ->
                        val s = snapshots.getJSONObject(i)
                        "  ${s.optString("number")}  ${s.optString("date")}  ${s.optString("description")}"
                    }
                    AdminResult(true, lines)
                }
            }
        } catch (e: Exception) {
            AdminResult(false, "Couldn't reach desktop: ${e.message}")
        }
    }

    /** Backs the `/rollback <number> confirm` slash command -- stages
     * the BTRFS rollback and reboots the desktop, per snapshots.rollback_to. */
    fun rollback(number: String): AdminResult {
        val (host, port, adminToken) = adminCreds() ?: return AdminResult(false, adminCredsError())
        return try {
            val url = URL("http://$host:$port/admin/rollback")
            val connection = url.openConnection() as HttpURLConnection
            connection.requestMethod = "POST"
            connection.setRequestProperty("Content-Type", "application/json")
            connection.setRequestProperty("X-Admin-Token", adminToken)
            connection.doOutput = true
            connection.connectTimeout = 8_000
            connection.readTimeout = 90_000

            val body = JSONObject().apply { put("number", number) }
            OutputStreamWriter(connection.outputStream).use { it.write(body.toString()) }

            val responseCode = connection.responseCode
            val stream = if (responseCode in 200..299) connection.inputStream else connection.errorStream
            val json = JSONObject(stream.bufferedReader().use { it.readText() })

            val ok = responseCode in 200..299 && json.optBoolean("ok")
            AdminResult(ok, json.optString(if (ok) "message" else "error", "HTTP $responseCode"))
        } catch (e: Exception) {
            AdminResult(false, "Couldn't reach desktop: ${e.message}")
        }
    }

    /** Backs the `/edit <goal> confirm` slash command -- the "just tell
     * it in the app" path onto self_improve.run_self_edit on the
     * desktop: propose a patch, run it through the two-reviewer gate
     * (claude + gemini), and only apply if both approve (compile-checked,
     * auto-reverted on failure, committed to git if it lands). Same
     * admin-token gate as every other slash command here, since this is
     * the one that actually rewrites Gremlin's own source. Long read
     * timeout on purpose -- propose + review + apply is several
     * sequential model calls, not a quick call. */
    fun selfEdit(goal: String, runTests: Boolean): AdminResult {
        val (host, port, adminToken) = adminCreds() ?: return AdminResult(false, adminCredsError())
        return try {
            val url = URL("http://$host:$port/admin/self-edit")
            val connection = url.openConnection() as HttpURLConnection
            connection.requestMethod = "POST"
            connection.setRequestProperty("Content-Type", "application/json")
            connection.setRequestProperty("X-Admin-Token", adminToken)
            connection.doOutput = true
            connection.connectTimeout = 8_000
            connection.readTimeout = 600_000

            val body = JSONObject().apply { put("goal", goal); put("run_tests", runTests) }
            OutputStreamWriter(connection.outputStream).use { it.write(body.toString()) }

            val responseCode = connection.responseCode
            val stream = if (responseCode in 200..299) connection.inputStream else connection.errorStream
            val json = JSONObject(stream.bufferedReader().use { it.readText() })

            if (responseCode !in 200..299) {
                AdminResult(false, json.optString("error", "HTTP $responseCode"))
            } else {
                val applied = json.optBoolean("applied")
                val committed = json.optBoolean("committed")
                val text = when {
                    applied && committed -> "Applied and committed: ${json.optString("commit_message")}\n" +
                        "Files changed: ${json.optJSONArray("files_changed")}"
                    applied -> "Applied but NOT committed -- ${json.optString("warning")}"
                    else -> "NOT applied -- ${json.optString("reason")}"
                }
                AdminResult(applied, text)
            }
        } catch (e: Exception) {
            AdminResult(false, "Couldn't reach desktop: ${e.message}")
        }
    }

    private fun adminCreds(): Triple<String, Int, String>? {
        val host = prefs.getString("host", null)
        val port = prefs.getInt("port", 0)
        val adminToken = prefs.getString("admin_token", null)
        if (host == null || port == 0 || adminToken.isNullOrBlank()) return null
        return Triple(host, port, adminToken)
    }

    private fun adminCredsError(): String {
        val host = prefs.getString("host", null)
        val port = prefs.getInt("port", 0)
        if (host == null || port == 0) return "Not paired with a desktop"
        return "Set the admin token in Settings first"
    }

    private fun postToDesktop(host: String, port: Int, token: String, message: String, pendingSync: JSONArray? = null): String {
        val url = URL("http://$host:$port/chat")
        val connection = url.openConnection() as HttpURLConnection
        connection.requestMethod = "POST"
        connection.setRequestProperty("Content-Type", "application/json")
        connection.setRequestProperty("Authorization", "Bearer $token")
        connection.doOutput = true
        connection.connectTimeout = desktopConnectTimeoutMs
        connection.readTimeout = desktopReadTimeoutMs

        val body = JSONObject().apply {
            put("message", message)
            put("token", token)
            if (pendingSync != null && pendingSync.length() > 0) {
                put("pending_sync", pendingSync)
            }
        }
        OutputStreamWriter(connection.outputStream).use { it.write(body.toString()) }

        val responseCode = connection.responseCode
        val stream = if (responseCode in 200..299) connection.inputStream else connection.errorStream
        val json = JSONObject(stream.bufferedReader().use { it.readText() })

        if (responseCode !in 200..299) {
            throw RuntimeException(json.optString("error", "HTTP $responseCode"))
        }
        return json.optString("answer", "[empty response]")
    }

    private fun callClaude(apiKey: String, systemPrompt: String, message: String): String {
        val modelId = prefs.getString("claude_model_id", null) ?: "claude-sonnet-5"
        val url = URL("https://api.anthropic.com/v1/messages")
        val connection = url.openConnection() as HttpURLConnection
        connection.requestMethod = "POST"
        connection.setRequestProperty("Content-Type", "application/json")
        connection.setRequestProperty("x-api-key", apiKey)
        connection.setRequestProperty("anthropic-version", "2023-06-01")
        connection.doOutput = true
        connection.connectTimeout = 10_000
        connection.readTimeout = 60_000

        val body = JSONObject().apply {
            put("model", modelId)
            put("max_tokens", 1024)
            if (systemPrompt.isNotBlank()) put("system", systemPrompt)
            put("messages", org.json.JSONArray().put(
                JSONObject().apply { put("role", "user"); put("content", message) }
            ))
        }
        OutputStreamWriter(connection.outputStream).use { it.write(body.toString()) }

        val responseCode = connection.responseCode
        val stream = if (responseCode in 200..299) connection.inputStream else connection.errorStream
        val json = JSONObject(stream.bufferedReader().use { it.readText() })

        if (responseCode !in 200..299) {
            val errMsg = json.optJSONObject("error")?.optString("message") ?: "HTTP $responseCode"
            throw RuntimeException(errMsg)
        }

        val contentArray = json.optJSONArray("content") ?: return "[empty response]"
        val textParts = mutableListOf<String>()
        for (i in 0 until contentArray.length()) {
            val block = contentArray.getJSONObject(i)
            if (block.optString("type") == "text") textParts.add(block.optString("text"))
        }
        return if (textParts.isEmpty()) "[empty response]" else textParts.joinToString("")
    }

    private fun callGemini(apiKey: String, systemPrompt: String, message: String): String {
        val modelId = prefs.getString("gemini_model_id", null) ?: "gemini-2.5-flash"
        val url = URL("https://generativelanguage.googleapis.com/v1beta/models/$modelId:generateContent?key=$apiKey")
        val connection = url.openConnection() as HttpURLConnection
        connection.requestMethod = "POST"
        connection.setRequestProperty("Content-Type", "application/json")
        connection.doOutput = true
        connection.connectTimeout = 10_000
        connection.readTimeout = 60_000

        val body = JSONObject().apply {
            put("contents", org.json.JSONArray().put(
                JSONObject().apply {
                    put("parts", org.json.JSONArray().put(JSONObject().apply { put("text", message) }))
                }
            ))
            if (systemPrompt.isNotBlank()) {
                put("systemInstruction", JSONObject().apply {
                    put("parts", org.json.JSONArray().put(JSONObject().apply { put("text", systemPrompt) }))
                })
            }
            put("generationConfig", JSONObject().apply { put("maxOutputTokens", 1024) })
        }
        OutputStreamWriter(connection.outputStream).use { it.write(body.toString()) }

        val responseCode = connection.responseCode
        val stream = if (responseCode in 200..299) connection.inputStream else connection.errorStream
        val json = JSONObject(stream.bufferedReader().use { it.readText() })

        if (responseCode !in 200..299) {
            val errMsg = json.optJSONObject("error")?.optString("message") ?: "HTTP $responseCode"
            throw RuntimeException(errMsg)
        }

        val candidates = json.optJSONArray("candidates") ?: return "[empty response]"
        if (candidates.length() == 0) return "[empty response]"
        val parts = candidates.getJSONObject(0).optJSONObject("content")?.optJSONArray("parts")
        if (parts == null || parts.length() == 0) return "[empty response]"
        return parts.getJSONObject(0).optString("text", "[empty response]")
    }
}
