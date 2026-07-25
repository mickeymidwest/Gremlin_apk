package com.gremlin.app.overlay

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.os.Handler
import android.os.Looper
import android.util.DisplayMetrics
import android.view.WindowManager
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.latin.TextRecognizerOptions
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Turns "what's currently on the screen" into text Gremlin can actually
 * reason about.
 *
 * Capture is one frame, not a stream: the overlay is meant to answer a
 * question about whatever's on screen right now (a homework page, an
 * error dialog), so holding a continuous VirtualDisplay open would burn
 * battery and keep the screen-recording indicator lit for no benefit.
 * Everything is torn down immediately after the single frame lands.
 *
 * OCR uses ML Kit's *bundled* Latin recognizer, so it works with no
 * network and no Play Services dependency -- the whole point of the
 * offline story elsewhere in this app would be undermined by an overlay
 * that silently needed connectivity to read a page.
 */
object ScreenReader {

    private const val CAPTURE_TIMEOUT_SECONDS = 5L

    /** Grabs a single frame of the screen. Returns null if it doesn't arrive in time. */
    @SuppressLint("WrongConstant")
    fun captureFrame(context: Context, projection: MediaProjection): Bitmap? {
        val metrics = DisplayMetrics()
        val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        @Suppress("DEPRECATION")
        wm.defaultDisplay.getRealMetrics(metrics)

        val width = metrics.widthPixels
        val height = metrics.heightPixels
        val density = metrics.densityDpi
        if (width <= 0 || height <= 0) return null

        // maxImages=2 rather than 1: with a single buffer the producer can
        // stall waiting for us to release the only image, which shows up
        // as a capture that simply never arrives.
        val reader = ImageReader.newInstance(width, height, android.graphics.PixelFormat.RGBA_8888, 2)
        var virtualDisplay: VirtualDisplay? = null
        var bitmap: Bitmap? = null
        val latch = CountDownLatch(1)
        val handler = Handler(Looper.getMainLooper())

        reader.setOnImageAvailableListener({ r ->
            if (latch.count == 0L) return@setOnImageAvailableListener
            var image: Image? = null
            try {
                image = r.acquireLatestImage()
                if (image != null) {
                    bitmap = imageToBitmap(image, width, height)
                    latch.countDown()
                }
            } catch (e: Exception) {
                latch.countDown()
            } finally {
                image?.close()
            }
        }, handler)

        return try {
            virtualDisplay = projection.createVirtualDisplay(
                "gremlin-screen-read",
                width, height, density,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                reader.surface, null, handler,
            )
            latch.await(CAPTURE_TIMEOUT_SECONDS, TimeUnit.SECONDS)
            bitmap
        } catch (e: Exception) {
            null
        } finally {
            try { virtualDisplay?.release() } catch (e: Exception) {}
            try { reader.close() } catch (e: Exception) {}
        }
    }

    /**
     * ImageReader hands back rows padded out to a stride, so the raw
     * buffer is usually wider than the screen -- copying it straight
     * into a width-sized bitmap produces the classic skewed/diagonal
     * image. Allocate at the padded width, then crop back down.
     */
    private fun imageToBitmap(image: Image, width: Int, height: Int): Bitmap {
        val plane = image.planes[0]
        val pixelStride = plane.pixelStride
        val rowStride = plane.rowStride
        val rowPadding = rowStride - pixelStride * width
        val paddedWidth = width + rowPadding / pixelStride

        val full = Bitmap.createBitmap(paddedWidth, height, Bitmap.Config.ARGB_8888)
        full.copyPixelsFromBuffer(plane.buffer)
        return if (paddedWidth != width) {
            val cropped = Bitmap.createBitmap(full, 0, 0, width, height)
            full.recycle()
            cropped
        } else {
            full
        }
    }

    /** Synchronous OCR -- callers here are already on a background thread. */
    fun extractText(bitmap: Bitmap): String {
        val recognizer = TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS)
        val latch = CountDownLatch(1)
        var text = ""
        try {
            recognizer.process(InputImage.fromBitmap(bitmap, 0))
                .addOnSuccessListener { result -> text = result.text; latch.countDown() }
                .addOnFailureListener { latch.countDown() }
            latch.await(15, TimeUnit.SECONDS)
        } catch (e: Exception) {
            // Fall through to whatever (if anything) landed before the failure.
        } finally {
            try { recognizer.close() } catch (e: Exception) {}
        }
        return text
    }

    /** Capture + OCR in one call. Empty string means nothing readable. */
    fun readScreen(context: Context, projection: MediaProjection): String {
        val frame = captureFrame(context, projection) ?: return ""
        return try {
            extractText(frame)
        } finally {
            frame.recycle()
        }
    }
}
