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
 * Gets the phone's ONE offline model into place.
 *
 * One model, both jobs (see [LocalModel]): a vision-language model is a
 * language model, so the same weights answer a question and read a
 * screenshot. There is no separate "chat model" and "vision model" any
 * more, and therefore no second download to keep in sync.
 *
 * Two sources, desktop first:
 *
 *  1. **Synced from the desktop** when paired -- whatever
 *     `persona.phone_model` names in config/models.yaml. That indirection
 *     is the point: the desktop decides what the phone runs, so changing
 *     one config line updates the phone, instead of the phone hardcoding
 *     a model the desktop knows nothing about. Resumable, because a
 *     several-hundred-MB Wi-Fi transfer will get interrupted.
 *
 *  2. **Hugging Face** when there's no desktop to sync from, so an
 *     unpaired phone still gets something rather than nothing.
 *
 * **Hard 2GB ceiling on anything that lands here**, enforced three ways
 * because each catches a different failure: against the size the source
 * advertises before a byte is written, against the running total
 * mid-stream (a server can omit Content-Length and otherwise stream
 * forever, which makes the first check trivially bypassable), and
 * against free space. The desktop's own primary is far above that line --
 * which is exactly why the server exposes a separate `phone_model`
 * rather than letting the phone reach for the primary.
 */
object OfflineModelManager {

    const val MAX_SYNC_BYTES = 2L * 1000 * 1000 * 1000  // 2 GB, hard

    // Fallback only -- used when there's no desktop to sync from.
    private const val FALLBACK_REPO = "ggml-org/SmolVLM-500M-Instruct-GGUF"
    private const val FALLBACK_WEIGHTS = "SmolVLM-500M-Instruct-Q8_0.gguf"
    private const val FALLBACK_MMPROJ = "mmproj-SmolVLM-500M-Instruct-Q8_0.gguf"
    const val FALLBACK_TOTAL_BYTES = 546_000_000L

    const val WEIGHTS_FILENAME = "gremlin-offline-weights.gguf"
    const val MMPROJ_FILENAME = "gremlin-offline-mmproj.gguf"

    const val KEY_ENABLED = "offline_model_enabled"
    const val KEY_WEIGHTS = "offline_model_weights_path"
    const val KEY_MMPROJ = "offline_model_mmproj_path"
    const val KEY_SOURCE = "offline_model_source"    // "desktop" | "huggingface"
    const val KEY_VERSION = "offline_model_version"
    const val KEY_NAME = "offline_model_name"

    data class DesktopModel(
        val name: String,
        val totalBytes: Long,
        val version: String,
        val hasVision: Boolean,
    )

    fun weightsFile(context: Context): File = File(context.filesDir, WEIGHTS_FILENAME)
    fun mmprojFile(context: Context): File = File(context.filesDir, MMPROJ_FILENAME)

    fun isDownloaded(context: Context): Boolean =
        weightsFile(context).let { it.exists() && it.length() > 0 }

    /** Vision needs the projector; without it the model can chat but not see. */
    fun hasVision(context: Context): Boolean =
        mmprojFile(context).let { it.exists() && it.length() > 0 }

    fun totalBytesOnDisk(context: Context): Long =
        (weightsFile(context).takeIf { it.exists() }?.length() ?: 0L) +
            (mmprojFile(context).takeIf { it.exists() }?.length() ?: 0L)

    fun freeSpaceBytes(context: Context): Long = try {
        StatFs(context.filesDir.absolutePath).availableBytes
    } catch (e: Exception) { Long.MAX_VALUE }

    sealed class Result {
        object Success : Result()
        data class Failure(val message: String) : Result()
    }

    /** What the desktop says the phone should run, or null if unreachable. */
    fun fetchDesktopModel(prefs: SharedPreferences): DesktopModel? {
        val host = prefs.getString("host", null) ?: return null
        val port = prefs.getInt("port", 0)
        val token = prefs.getString("token", null) ?: return null
        if (port == 0) return null
        return try {
            val conn = (URL("http://$host:$port/model-info").openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                setRequestProperty("Authorization", "Bearer $token")
                connectTimeout = 5_000; readTimeout = 10_000
            }
            if (conn.responseCode !in 200..299) return null
            val j = JSONObject(conn.inputStream.bufferedReader().use { it.readText() })
            if (!j.optBoolean("ok")) return null
            DesktopModel(
                name = j.optString("model_name"),
                totalBytes = j.optLong("total_bytes"),
                version = j.optString("version"),
                hasVision = j.optBoolean("has_vision"),
            )
        } catch (e: Exception) { null }
    }

    fun isInSyncWithDesktop(prefs: SharedPreferences, desktop: DesktopModel?): Boolean {
        if (desktop == null) return false
        if (prefs.getString(KEY_SOURCE, null) != "desktop") return false
        return prefs.getString(KEY_VERSION, null) == desktop.version
    }

    /** Sync the desktop's designated phone model. Blocking. */
    fun syncFromDesktop(
        context: Context,
        prefs: SharedPreferences,
        onProgress: (Long, Long) -> Unit,
    ): Result {
        val host = prefs.getString("host", null)
        val port = prefs.getInt("port", 0)
        val adminToken = prefs.getString("admin_token", null)
        if (host == null || port == 0) return Result.Failure("Not paired with a desktop.")
        if (adminToken.isNullOrBlank()) {
            return Result.Failure("Set the admin token in Settings first -- weights are admin-gated.")
        }

        val info = fetchDesktopModel(prefs)
            ?: return Result.Failure("Couldn't reach the desktop, or it has no phone_model configured.")

        // Guard 1: advertised size, before writing anything.
        if (info.totalBytes > MAX_SYNC_BYTES) {
            return Result.Failure(
                "Refusing: the desktop offers ${info.totalBytes / 1_000_000}MB, over the " +
                    "${MAX_SYNC_BYTES / 1_000_000}MB limit for this phone."
            )
        }
        // Guard 3: free space.
        if (freeSpaceBytes(context) < info.totalBytes + 200_000_000L) {
            return Result.Failure("Not enough space: needs ~${info.totalBytes / 1_000_000}MB plus headroom.")
        }

        // A partial left over from a DIFFERENT version must not be
        // resumed into -- that produces a corrupt hybrid file.
        if (prefs.getString("offline_partial_version", null) != info.version) {
            File(context.filesDir, "$WEIGHTS_FILENAME.part").delete()
            File(context.filesDir, "$MMPROJ_FILENAME.part").delete()
        }
        prefs.edit().putString("offline_partial_version", info.version).apply()

        val parts = mutableListOf(Triple("weights", WEIGHTS_FILENAME, weightsFile(context)))
        if (info.hasVision) parts.add(Triple("mmproj", MMPROJ_FILENAME, mmprojFile(context)))

        var done = 0L
        for ((role, filename, _dest) in parts) {
            val partial = File(context.filesDir, "$filename.part")
            val res = downloadPart(
                "http://$host:$port/admin/model-file?part=$role",
                adminToken, partial, done, info.totalBytes, onProgress,
            )
            if (res is Result.Failure) return res
            done += partial.length()
        }

        // Promote both together, so "chat works but vision silently
        // doesn't" can't result from a half-finished sync.
        LocalModel.unloadModel()
        for ((_role, filename, dest) in parts) {
            val partial = File(context.filesDir, "$filename.part")
            dest.delete()
            if (!partial.renameTo(dest)) return Result.Failure("Couldn't finalize $filename.")
        }
        if (!info.hasVision) mmprojFile(context).delete()

        prefs.edit()
            .putString(KEY_WEIGHTS, weightsFile(context).absolutePath)
            .putString(KEY_MMPROJ, if (info.hasVision) mmprojFile(context).absolutePath else null)
            .putString(KEY_SOURCE, "desktop")
            .putString(KEY_VERSION, info.version)
            .putString(KEY_NAME, info.name)
            .putBoolean(KEY_ENABLED, true)
            .remove("offline_partial_version")
            .apply()

        refreshPersona(prefs, host, port)
        return Result.Success
    }

    /** No desktop to sync from -- pull the fallback from Hugging Face. */
    fun downloadFallback(
        context: Context,
        prefs: SharedPreferences,
        onProgress: (Long, Long) -> Unit,
    ): Result {
        val urls = listOf(
            Triple(WEIGHTS_FILENAME, weightsFile(context),
                "https://huggingface.co/$FALLBACK_REPO/resolve/main/$FALLBACK_WEIGHTS"),
            Triple(MMPROJ_FILENAME, mmprojFile(context),
                "https://huggingface.co/$FALLBACK_REPO/resolve/main/$FALLBACK_MMPROJ"),
        )
        if (freeSpaceBytes(context) < FALLBACK_TOTAL_BYTES + 150_000_000L) {
            return Result.Failure("Not enough free space for the offline model.")
        }

        var done = 0L
        for ((filename, _dest, url) in urls) {
            val partial = File(context.filesDir, "$filename.part")
            partial.delete()
            val res = downloadPart(url, null, partial, done, FALLBACK_TOTAL_BYTES, onProgress)
            if (res is Result.Failure) return res
            done += partial.length()
        }

        LocalModel.unloadModel()
        for ((filename, dest, _u) in urls) {
            val partial = File(context.filesDir, "$filename.part")
            dest.delete()
            if (!partial.renameTo(dest)) return Result.Failure("Couldn't finalize $filename.")
        }

        prefs.edit()
            .putString(KEY_WEIGHTS, weightsFile(context).absolutePath)
            .putString(KEY_MMPROJ, mmprojFile(context).absolutePath)
            .putString(KEY_SOURCE, "huggingface")
            .putString(KEY_NAME, "SmolVLM-500M (fallback)")
            .remove(KEY_VERSION)
            .putBoolean(KEY_ENABLED, true)
            .apply()
        return Result.Success
    }

    private fun downloadPart(
        url: String,
        adminToken: String?,
        partial: File,
        alreadyDone: Long,
        expectedTotal: Long,
        onProgress: (Long, Long) -> Unit,
    ): Result {
        val have = if (partial.exists()) partial.length() else 0L
        var conn: HttpURLConnection? = null
        try {
            conn = (URL(url).openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                adminToken?.let { setRequestProperty("X-Admin-Token", it) }
                if (have > 0) setRequestProperty("Range", "bytes=$have-")
                instanceFollowRedirects = true
                connectTimeout = 15_000
                readTimeout = 120_000  // multi-hundred-MB body over Wi-Fi
            }
            val code = conn.responseCode
            if (code !in 200..299) {
                return Result.Failure(
                    if (code == 401) "Admin token rejected by the desktop." else "Transfer failed (HTTP $code)."
                )
            }
            // A 200 answer to a ranged request means the server ignored
            // the Range -- our existing bytes are then meaningless and
            // appending would corrupt the file.
            val resuming = code == HttpURLConnection.HTTP_PARTIAL && have > 0
            var written = if (resuming) have else 0L

            conn.inputStream.use { input ->
                RandomAccessFile(partial, "rw").use { out ->
                    out.seek(written)
                    val buf = ByteArray(256 * 1024)
                    while (true) {
                        val n = input.read(buf)
                        if (n == -1) break
                        out.write(buf, 0, n)
                        written += n
                        // Guard 2: running total. Catches a server with no
                        // Content-Length, which would otherwise bypass the
                        // advertised-size check entirely.
                        if (alreadyDone + written > MAX_SYNC_BYTES) {
                            return Result.Failure(
                                "Aborted: exceeded the ${MAX_SYNC_BYTES / 1_000_000}MB limit mid-transfer."
                            )
                        }
                        onProgress(alreadyDone + written, expectedTotal)
                    }
                    // Truncate any tail left from a longer previous attempt.
                    out.setLength(written)
                }
            }
            return Result.Success
        } catch (e: Exception) {
            // Partial deliberately left in place -- that's what makes the
            // next attempt a resume rather than a restart.
            return Result.Failure("Transfer failed: ${e.message}. Run it again -- it resumes.")
        } finally {
            conn?.disconnect()
        }
    }

    private fun refreshPersona(prefs: SharedPreferences, host: String, port: Int) {
        val token = prefs.getString("token", null) ?: return
        try {
            val conn = (URL("http://$host:$port/status").openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                setRequestProperty("Authorization", "Bearer $token")
                connectTimeout = 4_000; readTimeout = 8_000
            }
            val prompt = JSONObject(conn.inputStream.bufferedReader().use { it.readText() })
                .optString("system_prompt", "")
            if (prompt.isNotBlank()) prefs.edit().putString("cached_persona_prompt", prompt).apply()
        } catch (e: Exception) {
            // Best-effort: identical weights with a stale persona still
            // works, it just sounds slightly less like Gremlin.
        }
    }

    fun describeLocal(context: Context, prefs: SharedPreferences): String {
        if (!isDownloaded(context)) {
            return "No offline model yet. Without one, Gremlin can't answer or read screenshots " +
                "when the desktop is unreachable and there's no API key."
        }
        val mb = totalBytesOnDisk(context) / 1_000_000
        val name = prefs.getString(KEY_NAME, "unknown")
        val vision = if (hasVision(context)) "chats and reads images" else "chats only (no vision projector)"
        return when (prefs.getString(KEY_SOURCE, null)) {
            "desktop" -> "Synced from your desktop: $name (${mb}MB) -- $vision."
            else -> "$name (${mb}MB) -- $vision. Downloaded directly, not from your desktop."
        }
    }

    fun delete(context: Context, prefs: SharedPreferences) {
        LocalModel.unloadModel()
        weightsFile(context).delete()
        mmprojFile(context).delete()
        File(context.filesDir, "$WEIGHTS_FILENAME.part").delete()
        File(context.filesDir, "$MMPROJ_FILENAME.part").delete()
        prefs.edit()
            .putBoolean(KEY_ENABLED, false)
            .remove(KEY_WEIGHTS).remove(KEY_MMPROJ).remove(KEY_SOURCE)
            .remove(KEY_VERSION).remove(KEY_NAME).remove("offline_partial_version")
            .apply()
    }
}
