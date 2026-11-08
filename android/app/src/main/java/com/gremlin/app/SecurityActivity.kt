package com.gremlin.app

import android.app.AlertDialog
import android.os.Bundle
import android.text.InputType
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import com.gremlin.app.adb.DeepScan
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
        val pairAdb = findViewById<Button>(R.id.security_pair_adb)
        val deep = findViewById<Button>(R.id.security_deep_scan)
        buttons = listOf(phone, desktop, accept, pairAdb, deep)
        pairAdb.setOnClickListener { askPairing() }
        deep.setOnClickListener { deepScan(null) }
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

    private fun numberField(hint: String): EditText = EditText(this).apply {
        this.hint = hint
        inputType = InputType.TYPE_CLASS_NUMBER
    }

    /** Pair once: Wireless debugging > Pair device with pairing code shows a port and a 6-digit code. */
    private fun askPairing() {
        val port = numberField("Pairing port (from the popup)")
        val code = numberField("6-digit pairing code")
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 16, 48, 0)
            addView(port); addView(code)
        }
        AlertDialog.Builder(this)
            .setTitle("Pair Gremlin with this phone")
            .setMessage("Settings > Developer options > Wireless debugging > Pair device with pairing code. " +
                        "Use split screen so the popup stays open.")
            .setView(box)
            .setPositiveButton("Pair") { _, _ ->
                val p = port.text.toString().toIntOrNull()
                val c = code.text.toString()
                if (p == null || c.length < 6) {
                    status.text = "That needs the pairing port and the 6-digit code."
                } else {
                    setBusy(true)
                    status.text = "Pairing…"
                    Thread {
                        val err = DeepScan.pair(applicationContext, p, c)
                        runOnUiThread {
                            if (isFinishing || isDestroyed) return@runOnUiThread
                            status.text = err ?: "Paired. Deep scan can now look at what apps actually did."
                            setBusy(false)
                        }
                    }.start()
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    /** Collect appops through the app's own ADB, let the desktop write the report. */
    private fun deepScan(port: Int?) {
        setBusy(true)
        status.text = "Deep scan: connecting to this phone's Wireless debugging…"
        Thread {
            val r = DeepScan.collect(applicationContext, port)
            val g = if (r.ops != null) client.postAppops(r.ops) else null
            runOnUiThread {
                if (isFinishing || isDestroyed) return@runOnUiThread
                when {
                    r.error != null && port == null -> { status.text = r.error; askConnectPort() }
                    r.error != null -> status.text = r.error
                    g?.report != null -> {
                        report.text = g.report
                        status.text = "Deep scan of ${r.count} apps: what they did with the sensitive stuff in the last day."
                    }
                    else -> status.text = g?.error ?: "The desktop didn't answer."
                }
                setBusy(false)
            }
        }.start()
    }

    private fun askConnectPort() {
        val port = numberField("Port under \"IP address & Port\"")
        AlertDialog.Builder(this)
            .setTitle("Couldn't find the port by itself")
            .setMessage("On the Wireless debugging screen, the number after the colon in \"IP address & Port\".")
            .setView(LinearLayout(this).apply { setPadding(48, 16, 48, 0); addView(port) })
            .setPositiveButton("Scan") { _, _ -> port.text.toString().toIntOrNull()?.let { deepScan(it) } }
            .setNegativeButton("Cancel", null)
            .show()
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
