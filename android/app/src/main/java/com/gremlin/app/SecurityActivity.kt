package com.gremlin.app

import android.app.AlertDialog
import android.os.Bundle
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * Settings -> Phone & desktop safety (S2/S3). "Check phone" sends this
 * phone's security snapshot to Gremlin; "Check desktop" runs the desktop
 * guard. Each answers with what changed since the state Mickey marked as
 * normal. Read-only: nothing here changes a setting.
 */
class SecurityActivity : AppCompatActivity() {

    private lateinit var client: GremlinClient
    private lateinit var status: TextView
    private lateinit var report: TextView
    private lateinit var buttons: List<Button>
    private var lastChecked: String? = null  // "phone" or "desktop"

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_security)
        title = "Phone & desktop safety"
        client = GremlinClient(getSharedPreferences("gremlin_prefs", MODE_PRIVATE), applicationContext)
        status = findViewById(R.id.security_status)
        report = findViewById(R.id.security_report)
        val phone = findViewById<Button>(R.id.security_check_phone)
        val desktop = findViewById<Button>(R.id.security_check_desktop)
        val accept = findViewById<Button>(R.id.security_accept)
        buttons = listOf(phone, desktop, accept)
        phone.setOnClickListener { check("phone") }
        desktop.setOnClickListener { check("desktop") }
        accept.setOnClickListener { confirmAccept() }
        accept.isEnabled = false
        status.text = "Checks look for what changed since you last marked things as normal."
    }

    private fun setBusy(busy: Boolean) {
        buttons.forEach { it.isEnabled = !busy }
        if (!busy) buttons[2].isEnabled = lastChecked != null
    }

    private fun check(which: String) {
        setBusy(true)
        status.text = if (which == "phone") "Looking over this phone…" else "Asking the desktop to look itself over…"
        Thread {
            val r = if (which == "phone") {
                val snap = try { PhoneGuard.snapshot(applicationContext) } catch (e: Exception) { null }
                if (snap == null) GremlinClient.GuardResult(error = "Couldn't read this phone's settings.")
                else client.postPhoneGuard(snap)
            } else client.getDesktopGuard()
            runOnUiThread {
                if (isFinishing || isDestroyed) return@runOnUiThread
                if (r.report != null) {
                    lastChecked = which
                    report.text = r.report
                    status.text = if (which == "phone") "This phone, compared with what you marked as normal:"
                                  else "The desktop, compared with what you marked as normal:"
                } else {
                    status.text = r.error ?: "That didn't work."
                }
                setBusy(false)
            }
        }.start()
    }

    private fun confirmAccept() {
        val which = lastChecked ?: return
        AlertDialog.Builder(this)
            .setTitle("Mark as normal?")
            .setMessage("Only do this if everything in the last $which check is something you expected. " +
                        "Future checks will compare against this.")
            .setPositiveButton("Mark as normal") { _, _ -> accept(which) }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun accept(which: String) {
        setBusy(true)
        Thread {
            val r = client.acceptGuard(which)
            runOnUiThread {
                if (isFinishing || isDestroyed) return@runOnUiThread
                status.text = r.report ?: r.error ?: "Done."
                setBusy(false)
            }
        }.start()
    }
}
