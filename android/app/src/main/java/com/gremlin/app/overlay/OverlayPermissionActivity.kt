package com.gremlin.app.overlay

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity

/**
 * Invisible shim that collects the two permissions overlay mode needs
 * and then starts OverlayService.
 *
 * It exists because of an ordering constraint that isn't obvious: the
 * screen-capture consent dialog has to return its result into an
 * Activity, but the MediaProjection it authorizes can only be claimed by
 * an already-running foreground service on API 29+. So the sequence has
 * to be: get "draw over other apps" -> get capture consent here -> start
 * the service -> service claims the projection. Doing it in any other
 * order fails on modern Android.
 *
 * Overlay permission comes first because it's the one that can't be
 * granted in-app at all -- it needs a trip to a system settings screen,
 * and there's no point asking for screen capture if the bubble can't be
 * drawn anyway.
 */
class OverlayPermissionActivity : AppCompatActivity() {

    private val captureLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK && result.data != null) {
            val intent = Intent(this, OverlayService::class.java).apply {
                putExtra(OverlayService.EXTRA_RESULT_CODE, result.resultCode)
                putExtra(OverlayService.EXTRA_RESULT_DATA, result.data)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(intent)
            } else {
                startService(intent)
            }
            Toast.makeText(this, "Overlay on -- tap the bubble on any screen", Toast.LENGTH_LONG).show()
        } else {
            Toast.makeText(
                this,
                "Screen reading declined. The bubble still works, it just can't see the page.",
                Toast.LENGTH_LONG,
            ).show()
            // Still start it -- a bubble that can answer questions
            // without reading the screen is degraded, not useless, and
            // silently doing nothing here would look like a bug.
            startOverlayWithoutCapture()
        }
        finish()
    }

    private val overlayPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) {
        if (canDrawOverlays()) {
            requestCapture()
        } else {
            Toast.makeText(this, "Overlay permission not granted -- can't show the bubble.", Toast.LENGTH_LONG).show()
            finish()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!canDrawOverlays()) {
            Toast.makeText(this, "Allow Gremlin to draw over other apps, then come back", Toast.LENGTH_LONG).show()
            overlayPermissionLauncher.launch(
                Intent(
                    Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    Uri.parse("package:$packageName"),
                )
            )
        } else {
            requestCapture()
        }
    }

    private fun canDrawOverlays(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.canDrawOverlays(this)

    private fun requestCapture() {
        val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        captureLauncher.launch(mpm.createScreenCaptureIntent())
    }

    private fun startOverlayWithoutCapture() {
        val intent = Intent(this, OverlayService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
    }
}
