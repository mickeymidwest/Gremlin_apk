package com.gremlin.app.attach

import android.graphics.Bitmap
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.latin.TextRecognizerOptions
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Reads the text out of an attached image or PDF page. ML Kit's *bundled*
 * Latin recognizer: it ships inside the APK and works with no network and
 * no Play Services. (Moved here from the screen overlay, which was removed
 * on 2026-11-07; attachments are now its only user.)
 */
object ImageText {

    /** Synchronous OCR -- callers are already on a background thread. */
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
            // fall through with whatever (if anything) landed before the failure
        } finally {
            try { recognizer.close() } catch (e: Exception) {}
        }
        return text
    }
}
