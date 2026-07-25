package com.gremlin.app.llama

import android.content.Context
import android.content.SharedPreferences
import android.os.StatFs
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/**
 * Downloads and manages the on-device vision specialist (SmolVLM).
 *
 * **Hard 2GB ceiling on anything synced to this phone.** That limit is
 * enforced three ways, because each catches a different failure:
 *   1. against the size the source advertises, before a byte is written
 *   2. against the running total mid-download, since a server can lie or
 *      omit Content-Length entirely and would otherwise stream forever
 *   3. against free space, so a large download can't fill the device
 *
 * The ceiling is deliberately below the desktop's own vision model
 * (Qwen2.5-VL-3B, ~2.8GB): that model belongs on the desktop's GPU, and
 * dragging it onto a phone would be both refused here and unusably slow
 * if it weren't. The phone gets SmolVLM-256M (~279MB) instead, which is
 * the right trade for "read what's on screen" work.
 *
 * A VLM is TWO files -- language weights plus an `mmproj` projector that
 * turns image patches into embeddings. With only the weights it loads
 * fine and then silently can't see, answering from surrounding text
 * instead, which reads as a stupid model rather than a broken install.
 * Both files are therefore treated as one atomic unit: both download or
 * neither is kept.
 */
object VisionModelManager {

    /** No synced payload may exceed this, ever. */
    const val MAX_SYNC_BYTES = 2L * 1000 * 1000 * 1000  // 2 GB

    private const val REPO = "ggml-org/SmolVLM-256M-Instruct-GGUF"
    const val MODEL_FILE = "SmolVLM-256M-Instruct-Q8_0.gguf"
    const val MMPROJ_FILE = "mmproj-SmolVLM-256M-Instruct-Q8_0.gguf"

    // Verified by real HEAD request: 175MB + 104MB.
    const val EXPECTED_TOTAL_BYTES = 279_000_000L

    const val KEY_ENABLED = "vision_model_enabled"
    const val KEY_MODEL_PATH = "vision_model_path"
    const val KEY_MMPROJ_PATH = "vision_mmproj_path"

    private fun url(file: String) = "https://huggingface.co/$REPO/resolve/main/$file"

    fun modelFile(context: Context): File = File(context.filesDir, MODEL_FILE)
    fun mmprojFile(context: Context): File = File(context.filesDir, MMPROJ_FILE)

    /** Both halves present, or it isn't usable. */
    fun isDownloaded(context: Context): Boolean =
        modelFile(context).let { it.exists() && it.length() > 0 } &&
            mmprojFile(context).let { it.exists() && it.length() > 0 }

    fun totalBytesOnDisk(context: Context): Long =
        (modelFile(context).takeIf { it.exists() }?.length() ?: 0L) +
            (mmprojFile(context).takeIf { it.exists() }?.length() ?: 0L)

    fun freeSpaceBytes(context: Context): Long = try {
        StatFs(context.filesDir.absolutePath).availableBytes
    } catch (e: Exception) {
        Long.MAX_VALUE
    }

    sealed class Result {
        object Success : Result()
        data class Failure(val message: String) : Result()
    }

    /**
     * Downloads both files. Blocking -- callers use a background thread.
     * onProgress reports combined bytes across both files.
     */
    fun download(
        context: Context,
        prefs: SharedPreferences,
        onProgress: (downloaded: Long, total: Long) -> Unit,
    ): Result {
        val targets = listOf(MODEL_FILE to modelFile(context), MMPROJ_FILE to mmprojFile(context))

        // --- guard 1: advertised size, before writing anything ---
        var advertisedTotal = 0L
        for ((name, _) in targets) {
            val size = headContentLength(url(name))
            if (size > 0) advertisedTotal += size
        }
        if (advertisedTotal > MAX_SYNC_BYTES) {
            return Result.Failure(
                "Refusing: that's ${advertisedTotal / 1_000_000}MB, over the " +
                    "${MAX_SYNC_BYTES / 1_000_000}MB limit for anything synced to this phone."
            )
        }

        val expected = if (advertisedTotal > 0) advertisedTotal else EXPECTED_TOTAL_BYTES

        // --- guard 3: free space, with headroom ---
        if (freeSpaceBytes(context) < expected + 150_000_000L) {
            return Result.Failure("Not enough free space -- needs about ${expected / 1_000_000}MB plus headroom.")
        }

        var completed = 0L
        val partials = mutableListOf<File>()

        for ((name, dest) in targets) {
            val partial = File(context.filesDir, "$name.part")
            partials.add(partial)
            val result = downloadOne(url(name), partial, completed, expected, onProgress)
            if (result is Result.Failure) {
                partials.forEach { it.delete() }
                return result
            }
            completed += partial.length()
        }

        // Both halves arrived -- promote them together. Doing this only
        // at the end is what keeps "both or neither" true even if the
        // second download fails.
        LocalVision.unloadModel()
        for ((name, dest) in targets) {
            val partial = File(context.filesDir, "$name.part")
            dest.delete()
            if (!partial.renameTo(dest)) {
                partials.forEach { it.delete() }
                targets.forEach { it.second.delete() }
                return Result.Failure("Couldn't finalize $name.")
            }
        }

        prefs.edit()
            .putString(KEY_MODEL_PATH, modelFile(context).absolutePath)
            .putString(KEY_MMPROJ_PATH, mmprojFile(context).absolutePath)
            .putBoolean(KEY_ENABLED, true)
            .apply()
        return Result.Success
    }

    private fun headContentLength(u: String): Long {
        var conn: HttpURLConnection? = null
        return try {
            conn = (URL(u).openConnection() as HttpURLConnection).apply {
                requestMethod = "HEAD"
                instanceFollowRedirects = true
                connectTimeout = 10_000
                readTimeout = 10_000
            }
            if (conn.responseCode in 200..299) conn.contentLengthLong else -1L
        } catch (e: Exception) {
            -1L
        } finally {
            conn?.disconnect()
        }
    }

    private fun downloadOne(
        u: String,
        partial: File,
        alreadyDone: Long,
        expectedTotal: Long,
        onProgress: (Long, Long) -> Unit,
    ): Result {
        var conn: HttpURLConnection? = null
        try {
            conn = (URL(u).openConnection() as HttpURLConnection).apply {
                instanceFollowRedirects = true
                connectTimeout = 15_000
                readTimeout = 60_000
            }
            if (conn.responseCode !in 200..299) {
                return Result.Failure("Download failed (HTTP ${conn.responseCode}).")
            }

            conn.inputStream.use { input ->
                partial.outputStream().use { output ->
                    val buffer = ByteArray(128 * 1024)
                    var written = 0L
                    while (true) {
                        val read = input.read(buffer)
                        if (read == -1) break
                        output.write(buffer, 0, read)
                        written += read

                        // --- guard 2: running total, mid-stream ---
                        // Catches a server with no Content-Length, or one
                        // that under-reported it. Without this the first
                        // guard is trivially bypassed.
                        if (alreadyDone + written > MAX_SYNC_BYTES) {
                            return Result.Failure(
                                "Aborted: exceeded the ${MAX_SYNC_BYTES / 1_000_000}MB sync limit mid-download."
                            )
                        }
                        onProgress(alreadyDone + written, expectedTotal)
                    }
                }
            }
            return Result.Success
        } catch (e: Exception) {
            return Result.Failure("Download failed: ${e.message}")
        } finally {
            conn?.disconnect()
        }
    }

    fun describeLocal(context: Context): String = when {
        isDownloaded(context) ->
            "SmolVLM-256M on this phone (${totalBytesOnDisk(context) / 1_000_000}MB) -- " +
                "reads screenshots and images without needing the desktop."
        else ->
            "Not downloaded (~${EXPECTED_TOTAL_BYTES / 1_000_000}MB). " +
                "Without it, overlay/attachments read text only (OCR), not diagrams or figures."
    }

    fun delete(context: Context, prefs: SharedPreferences) {
        LocalVision.unloadModel()
        modelFile(context).delete()
        mmprojFile(context).delete()
        File(context.filesDir, "$MODEL_FILE.part").delete()
        File(context.filesDir, "$MMPROJ_FILE.part").delete()
        prefs.edit()
            .putBoolean(KEY_ENABLED, false)
            .remove(KEY_MODEL_PATH)
            .remove(KEY_MMPROJ_PATH)
            .apply()
    }
}
