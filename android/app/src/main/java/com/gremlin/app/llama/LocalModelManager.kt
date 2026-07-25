package com.gremlin.app.llama

import android.content.Context
import android.content.SharedPreferences
import android.os.StatFs
import org.json.JSONObject
import java.io.File
import java.io.RandomAccessFile
import java.net.HttpURLConnection
import java.net.URL

/**
 * Gets the offline model onto the phone -- and specifically, gets the
 * SAME model the desktop is running.
 *
 * That's the whole point of this file. An offline model that's a
 * different, smaller model than the desktop's primary doesn't just
 * answer more slowly, it answers *differently*: different knowledge,
 * different refusals, different voice. Being away from home would mean
 * talking to a different assistant wearing the same name. So the
 * preferred path here is syncing the desktop's actual primary GGUF byte
 * for byte over the LAN.
 *
 * Two sources, in order of preference:
 *
 *  1. **The desktop** (syncFromDesktop) -- byte-identical clone of
 *     whatever config/models.yaml currently names as primary. Resumable,
 *     because a multi-gigabyte transfer over Wi-Fi WILL get interrupted
 *     and restarting from zero each time makes the feature unusable.
 *     Tracks a version string so a model swap on the desktop is
 *     noticed rather than silently leaving a stale clone behind.
 *
 *  2. **A small fallback model** (downloadFallback) -- only for someone
 *     who has never paired with a desktop and just wants *something*
 *     offline. Clearly labelled as not-the-same-model everywhere it's
 *     surfaced, because quietly substituting a 1B model for a 9B one is
 *     exactly the confusion this class exists to prevent.
 */
object LocalModelManager {

    // Kept for the unpaired case only. Verified real with a HEAD request
    // when it was added (Content-Length 955,445,792) rather than guessed.
    const val FALLBACK_URL =
        "https://huggingface.co/mradermacher/Llama-3.2-1B-Instruct-abliterated-GGUF/resolve/main/Llama-3.2-1B-Instruct-abliterated.Q4_K_M.gguf"
    const val FALLBACK_FILENAME = "gremlin-fallback-llama-3.2-1b.Q4_K_M.gguf"
    const val FALLBACK_SIZE_BYTES = 955_445_792L

    const val SYNCED_FILENAME = "gremlin-desktop-primary.gguf"

    // Prefs keys -- also read by SettingsActivity and GremlinClient.
    const val KEY_ENABLED = "local_model_enabled"
    const val KEY_PATH = "local_model_path"
    const val KEY_SOURCE = "local_model_source"     // "desktop" | "fallback"
    const val KEY_VERSION = "local_model_version"   // desktop's size-mtime
    const val KEY_NAME = "local_model_name"         // e.g. "qwythos-9b"

    data class DesktopModel(val name: String, val filename: String, val sizeBytes: Long, val version: String)

    /** What the desktop is currently running, or null if unreachable/unpaired. */
    fun fetchDesktopModelInfo(prefs: SharedPreferences): DesktopModel? {
        val host = prefs.getString("host", null) ?: return null
        val port = prefs.getInt("port", 0)
        val token = prefs.getString("token", null) ?: return null
        if (port == 0) return null

        return try {
            val conn = (URL("http://$host:$port/model-info").openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                setRequestProperty("Authorization", "Bearer $token")
                connectTimeout = 5_000
                readTimeout = 10_000
            }
            if (conn.responseCode !in 200..299) return null
            val json = JSONObject(conn.inputStream.bufferedReader().use { it.readText() })
            if (!json.optBoolean("ok")) return null
            DesktopModel(
                name = json.optString("model_name"),
                filename = json.optString("filename"),
                sizeBytes = json.optLong("size_bytes"),
                version = json.optString("version"),
            )
        } catch (e: Exception) {
            null
        }
    }

    fun syncedFile(context: Context): File = File(context.filesDir, SYNCED_FILENAME)
    fun fallbackFile(context: Context): File = File(context.filesDir, FALLBACK_FILENAME)

    fun isDownloaded(context: Context): Boolean {
        val path = File(context.filesDir, SYNCED_FILENAME)
        if (path.exists() && path.length() > 0) return true
        val fb = fallbackFile(context)
        return fb.exists() && fb.length() > 0
    }

    /** True when the phone's copy is the desktop's current primary. */
    fun isInSyncWithDesktop(prefs: SharedPreferences, desktop: DesktopModel?): Boolean {
        if (desktop == null) return false
        if (prefs.getString(KEY_SOURCE, null) != "desktop") return false
        return prefs.getString(KEY_VERSION, null) == desktop.version
    }

    fun freeSpaceBytes(context: Context): Long = try {
        StatFs(context.filesDir.absolutePath).availableBytes
    } catch (e: Exception) {
        Long.MAX_VALUE // don't block a sync just because we couldn't measure
    }

    sealed class SyncResult {
        object Success : SyncResult()
        data class Failure(val message: String) : SyncResult()
    }

    /**
     * Pulls the desktop's primary GGUF into app-private storage.
     *
     * Resumes rather than restarts: a partial `.part` file's existing
     * length becomes a Range request offset, so an interrupted 5GB
     * transfer picks up where it stopped. The server supports this (see
     * /admin/model-file), and without it this feature would be
     * effectively unusable on real Wi-Fi.
     */
    fun syncFromDesktop(
        context: Context,
        prefs: SharedPreferences,
        onProgress: (downloaded: Long, total: Long) -> Unit,
    ): SyncResult {
        val host = prefs.getString("host", null)
        val port = prefs.getInt("port", 0)
        val adminToken = prefs.getString("admin_token", null)
        if (host == null || port == 0) return SyncResult.Failure("Not paired with a desktop.")
        if (adminToken.isNullOrBlank()) {
            return SyncResult.Failure("Set the admin token in Settings first -- model weights are admin-gated.")
        }

        val info = fetchDesktopModelInfo(prefs)
            ?: return SyncResult.Failure("Couldn't reach the desktop, or it has no primary model configured.")

        val dest = syncedFile(context)
        val partial = File(context.filesDir, "$SYNCED_FILENAME.part")

        // A stale partial from a *different* model would otherwise be
        // resumed into, producing a corrupt hybrid file.
        if (partial.exists() && prefs.getString("local_model_partial_version", null) != info.version) {
            partial.delete()
        }
        prefs.edit().putString("local_model_partial_version", info.version).apply()

        val alreadyHave = if (partial.exists()) partial.length() else 0L
        if (alreadyHave >= info.sizeBytes && info.sizeBytes > 0) partial.delete()

        val needed = info.sizeBytes - alreadyHave
        if (needed > 0 && freeSpaceBytes(context) < needed + 200_000_000L) {
            return SyncResult.Failure(
                "Not enough space: needs about ${needed / 1_000_000}MB more, plus headroom."
            )
        }

        var connection: HttpURLConnection? = null
        try {
            connection = (URL("http://$host:$port/admin/model-file").openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                setRequestProperty("X-Admin-Token", adminToken)
                if (alreadyHave > 0) setRequestProperty("Range", "bytes=$alreadyHave-")
                instanceFollowRedirects = true
                connectTimeout = 15_000
                // Deliberately long: this is a multi-GB body over Wi-Fi,
                // and a short read timeout would abort mid-transfer.
                readTimeout = 120_000
            }

            val code = connection.responseCode
            if (code !in 200..299) {
                return SyncResult.Failure(
                    if (code == 401) "Admin token rejected by the desktop." else "Desktop returned HTTP $code."
                )
            }
            // 200 to a Range request means the server ignored it and is
            // sending the whole file -- our existing bytes are then
            // meaningless and appending would corrupt the result.
            val resuming = code == HttpURLConnection.HTTP_PARTIAL && alreadyHave > 0
            if (!resuming && alreadyHave > 0) partial.delete()

            var written = if (resuming) alreadyHave else 0L
            val total = if (info.sizeBytes > 0) info.sizeBytes else (written + connection.contentLengthLong)

            connection.inputStream.use { input ->
                RandomAccessFile(partial, "rw").use { out ->
                    out.seek(written)
                    val buffer = ByteArray(256 * 1024)
                    while (true) {
                        val read = input.read(buffer)
                        if (read == -1) break
                        out.write(buffer, 0, read)
                        written += read
                        onProgress(written, total)
                    }
                }
            }

            if (info.sizeBytes > 0 && written != info.sizeBytes) {
                return SyncResult.Failure("Transfer ended early ($written of ${info.sizeBytes} bytes). Try again -- it resumes.")
            }

            LocalLlama.unloadModel() // can't replace a file that's mapped
            dest.delete()
            if (!partial.renameTo(dest)) {
                return SyncResult.Failure("Couldn't finalize the downloaded file.")
            }

            prefs.edit()
                .putString(KEY_PATH, dest.absolutePath)
                .putString(KEY_SOURCE, "desktop")
                .putString(KEY_VERSION, info.version)
                .putString(KEY_NAME, info.name)
                .putBoolean(KEY_ENABLED, true)
                .remove("local_model_partial_version")
                .apply()

            // Having the desktop's exact weights but a different system
            // prompt would still mean it "thinks different" -- refresh
            // the cached persona voice while we're definitely in range.
            refreshPersona(prefs, host, port)
            return SyncResult.Success
        } catch (e: Exception) {
            // Partial file is intentionally left in place -- that's what
            // makes the next attempt a resume instead of a restart.
            return SyncResult.Failure("Sync failed: ${e.message}. Run it again -- it picks up where it stopped.")
        } finally {
            connection?.disconnect()
        }
    }

    private fun refreshPersona(prefs: SharedPreferences, host: String, port: Int) {
        val token = prefs.getString("token", null) ?: return
        try {
            val conn = (URL("http://$host:$port/status").openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                setRequestProperty("Authorization", "Bearer $token")
                connectTimeout = 4_000
                readTimeout = 8_000
            }
            val text = conn.inputStream.bufferedReader().use { it.readText() }
            val prompt = JSONObject(text).optString("system_prompt", "")
            if (prompt.isNotBlank()) {
                prefs.edit().putString("cached_persona_prompt", prompt).apply()
            }
        } catch (e: Exception) {
            // Best-effort -- an existing cached voice is better than none.
        }
    }

    /**
     * Last-resort small model for someone who has never paired. Kept
     * separate from syncFromDesktop and labelled honestly: this is NOT
     * the same model the desktop runs and will not answer the same way.
     */
    fun downloadFallback(
        context: Context,
        prefs: SharedPreferences,
        onProgress: (downloaded: Long, total: Long) -> Unit,
    ): Boolean {
        val dest = fallbackFile(context)
        val partial = File(context.filesDir, "$FALLBACK_FILENAME.part")
        var connection: HttpURLConnection? = null
        try {
            connection = (URL(FALLBACK_URL).openConnection() as HttpURLConnection).apply {
                instanceFollowRedirects = true
                connectTimeout = 15_000
                readTimeout = 60_000
            }
            val total = connection.contentLengthLong.takeIf { it > 0 } ?: FALLBACK_SIZE_BYTES

            connection.inputStream.use { input ->
                partial.outputStream().use { output ->
                    val buffer = ByteArray(128 * 1024)
                    var downloaded = 0L
                    while (true) {
                        val read = input.read(buffer)
                        if (read == -1) break
                        output.write(buffer, 0, read)
                        downloaded += read
                        onProgress(downloaded, total)
                    }
                }
            }

            if (!partial.renameTo(dest)) {
                partial.delete()
                return false
            }
            prefs.edit()
                .putString(KEY_PATH, dest.absolutePath)
                .putString(KEY_SOURCE, "fallback")
                .putString(KEY_NAME, "Llama-3.2-1B (fallback, NOT your desktop's model)")
                .remove(KEY_VERSION)
                .putBoolean(KEY_ENABLED, true)
                .apply()
            return true
        } catch (e: Exception) {
            partial.delete()
            return false
        } finally {
            connection?.disconnect()
        }
    }

    /** Human-readable description of what's actually on the phone. */
    fun describeLocal(context: Context, prefs: SharedPreferences): String {
        val path = prefs.getString(KEY_PATH, null)
        if (path == null || !File(path).exists()) return "No offline model on this phone yet."
        val sizeMb = File(path).length() / 1_000_000
        val name = prefs.getString(KEY_NAME, "unknown")
        return when (prefs.getString(KEY_SOURCE, null)) {
            "desktop" -> "Synced clone of your desktop's primary: $name (${sizeMb}MB)."
            "fallback" -> "Fallback model: $name (${sizeMb}MB). This is NOT the same model your desktop runs."
            else -> "$name (${sizeMb}MB)."
        }
    }

    /** Removes whatever offline model is present and disables it. */
    fun delete(context: Context, prefs: SharedPreferences) {
        LocalLlama.unloadModel()
        syncedFile(context).delete()
        fallbackFile(context).delete()
        File(context.filesDir, "$SYNCED_FILENAME.part").delete()
        File(context.filesDir, "$FALLBACK_FILENAME.part").delete()
        prefs.edit()
            .putBoolean(KEY_ENABLED, false)
            .remove(KEY_PATH)
            .remove(KEY_SOURCE)
            .remove(KEY_VERSION)
            .remove(KEY_NAME)
            .remove("local_model_partial_version")
            .apply()
    }
}
