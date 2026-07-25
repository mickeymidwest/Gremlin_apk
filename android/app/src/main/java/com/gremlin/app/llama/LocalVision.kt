package com.gremlin.app.llama

import android.graphics.Bitmap
import android.util.Log
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * On-device vision specialist -- describes an image so the general model
 * can reason about it.
 *
 * This is NOT the general offline chat model that was removed (see
 * android/README.md). It does one thing the primary genuinely cannot do
 * at all: look at a screenshot or a photo. Its output is a description
 * that gets handed to whatever answers the actual question, which is the
 * point -- perception gets handled by a small focused model so the big
 * one spends its whole budget on reasoning.
 *
 * Everything runs on ONE dedicated thread. mtmd's eval helper is
 * explicitly documented as not thread-safe, and the native side keeps no
 * locks of its own, so this single-threaded executor IS the concurrency
 * guarantee. Calling in from two threads would corrupt the KV cache or
 * crash the process outright rather than throw.
 */
object LocalVision {

    private const val TAG = "LocalVision"

    // Single thread, not a pool -- see class docs. Also means a second
    // request queues behind the first rather than racing it.
    private val executor = Executors.newSingleThreadExecutor { r ->
        Thread(r, "gremlin-vision").apply { isDaemon = true }
    }

    @Volatile private var libraryLoaded = false
    @Volatile private var loaded = false

    private external fun nativeInit()
    private external fun nativeLoad(modelPath: String, mmprojPath: String, nThreads: Int): Boolean
    private external fun nativeIsReady(): Boolean
    private external fun nativeDescribe(rgb: ByteArray, width: Int, height: Int, prompt: String, maxTokens: Int): String
    private external fun nativeUnload()

    private fun ensureLibrary(): Boolean {
        if (libraryLoaded) return true
        return try {
            System.loadLibrary("gremlin-vision")
            libraryLoaded = true
            true
        } catch (e: Throwable) {
            // A missing/incompatible .so must degrade to "no vision",
            // never take the app down -- this is an optional capability.
            Log.e(TAG, "couldn't load native library: ${e.message}")
            false
        }
    }

    fun isReady(): Boolean = loaded

    /** Blocking. Returns false if the model couldn't be loaded. */
    fun loadModel(modelPath: String, mmprojPath: String): Boolean {
        if (!ensureLibrary()) return false
        return submit {
            nativeInit()
            val threads = Runtime.getRuntime().availableProcessors().coerceIn(2, 4)
            val ok = nativeLoad(modelPath, mmprojPath, threads)
            loaded = ok
            ok
        } ?: false
    }

    /**
     * Describes [bitmap]. Blocking; returns null on any failure.
     *
     * [maxWidth] downscales first: a full-resolution phone screenshot is
     * several megapixels, which turns into far more image tokens than the
     * context can hold and is much slower for no accuracy gain at text-
     * reading sizes.
     */
    fun describe(
        bitmap: Bitmap,
        prompt: String = "Describe everything visible here, transcribing any text exactly.",
        maxTokens: Int = 256,
        maxWidth: Int = 1024,
    ): String? {
        if (!loaded && !ensureLibrary()) return null
        if (!loaded) return null

        val scaled = downscale(bitmap, maxWidth)
        // Read the dimensions BEFORE the bitmap can be recycled below --
        // reading them off a recycled Bitmap throws.
        val width = scaled.width
        val height = scaled.height

        val rgb = try {
            toRgb888(scaled)
        } catch (e: OutOfMemoryError) {
            Log.e(TAG, "out of memory converting bitmap")
            null
        } finally {
            // Only recycle a bitmap this function created; recycling the
            // caller's would break it for anyone else still holding it.
            if (scaled !== bitmap) scaled.recycle()
        } ?: return null

        return submit {
            val text = nativeDescribe(rgb, width, height, prompt, maxTokens)
            text.takeIf { it.isNotBlank() }
        }
    }

    fun unloadModel() {
        if (!libraryLoaded) return
        submit<Unit> {
            nativeUnload()
            loaded = false
        }
    }

    /** Runs on the one vision thread and waits. Null on timeout/failure. */
    private fun <T> submit(block: () -> T): T? {
        return try {
            // Generous: loading a model or describing an image on a phone
            // CPU is genuinely slow, and a timeout here means a lost
            // answer, not a hung UI (callers are already off the main
            // thread).
            executor.submit(block).get(180, TimeUnit.SECONDS)
        } catch (e: Throwable) {
            Log.e(TAG, "vision call failed: ${e.message}")
            null
        }
    }

    private fun downscale(src: Bitmap, maxWidth: Int): Bitmap {
        if (src.width <= maxWidth) return src
        val ratio = maxWidth.toFloat() / src.width
        val h = (src.height * ratio).toInt().coerceAtLeast(1)
        return Bitmap.createScaledBitmap(src, maxWidth, h, true)
    }

    /**
     * ARGB_8888 -> tightly packed RGB888, which is what mtmd_bitmap_init
     * expects. Done here rather than in C++ so the large intermediate
     * array exists once instead of being copied across the JNI boundary
     * twice.
     */
    private fun toRgb888(bmp: Bitmap): ByteArray {
        val w = bmp.width
        val h = bmp.height
        val pixels = IntArray(w * h)
        bmp.getPixels(pixels, 0, w, 0, 0, w, h)
        val out = ByteArray(w * h * 3)
        var j = 0
        for (p in pixels) {
            out[j++] = ((p shr 16) and 0xFF).toByte() // R
            out[j++] = ((p shr 8) and 0xFF).toByte()  // G
            out[j++] = (p and 0xFF).toByte()          // B
        }
        return out
    }
}
