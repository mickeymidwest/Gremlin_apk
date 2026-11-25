package com.gremlin.app

import android.content.Intent
import android.content.SharedPreferences
import android.os.Bundle
import android.provider.Settings
import android.speech.tts.TextToSpeech
import android.view.Gravity
import android.view.View
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.SeekBar
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.switchmaterial.SwitchMaterial
import com.gremlin.app.voice.VoiceOutput
import org.json.JSONArray
import org.json.JSONObject
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale
import kotlin.math.abs
import kotlin.math.roundToInt

class SettingsActivity : AppCompatActivity() {

    // Separate from MainActivity's VoiceOutput -- this one only exists to
    // back the "Test voice" button here, scoped to this activity's own
    // lifecycle rather than reaching into MainActivity's instance.
    private var testTts: TextToSpeech? = null

    override fun onDestroy() {
        super.onDestroy()
        testTts?.shutdown()
    }


    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_settings)

        val prefs = getSharedPreferences("magic_prefs", MODE_PRIVATE)
        val host = intent.getStringExtra("host") ?: ""
        val port = intent.getIntExtra("port", 0)
        val token = intent.getStringExtra("token") ?: ""

        findViewById<TextView>(R.id.settings_connection_label).text =
            if (host.isNotEmpty()) "Paired with $host:$port" else "Not paired with a desktop"

        findViewById<Button>(R.id.settings_repair_button).setOnClickListener {
            prefs.edit().remove("host").remove("port").remove("token").apply()
            startQrScan()
        }
        setUpManualPairing(prefs, host, port, token)

        findViewById<Button>(R.id.show_commands_button).setOnClickListener { showCommandsReference() }
        findViewById<Button>(R.id.show_memory_button).setOnClickListener { showDesktopInfo("What Magic remembers", "/memory", "list") }
        findViewById<Button>(R.id.open_profile_button).setOnClickListener {
            startActivity(android.content.Intent(this, ProfileActivity::class.java))
        }
        // Per-box extras compiled only into local builds (kept out of git) can add rows here.
        try {
            Class.forName("com.gremlin.app.local.LocalExtras")
                .getMethod("addSettings", android.app.Activity::class.java).invoke(null, this)
        } catch (e: ClassNotFoundException) {
        } catch (e: Exception) {
        }
        findViewById<Button>(R.id.open_security_button).setOnClickListener {
            startActivity(android.content.Intent(this, SecurityActivity::class.java))
        }
        findViewById<Button>(R.id.show_skills_button).setOnClickListener { showDesktopInfo("Magic's skills", "/skill", "list") }
        findViewById<Button>(R.id.open_builds_button).setOnClickListener {
            startActivity(android.content.Intent(this, BuildsActivity::class.java))
        }

        // Away-mode API key fields, pre-filled with whatever's already saved
        val geminiInput = findViewById<EditText>(R.id.gemini_key_input)
        val geminiModelSpinner = findViewById<Spinner>(R.id.gemini_model_spinner)

        geminiInput.setText(prefs.getString("gemini_key", ""))
        setUpModelSpinner(geminiModelSpinner, R.array.gemini_model_choices, prefs.getString("gemini_model_id", null))

        // Admin token field lives further down this screen (see below),
        // but it's grabbed here too so the one "Save" button covers
        // everything on this screen -- it used to only get persisted as
        // a side effect of clicking "Run on Desktop"/"Reboot Desktop",
        // which meant typing a token (or a *new* token) and just
        // navigating away silently never saved it at all.
        val adminTokenInputForSave = findViewById<EditText>(R.id.admin_token_input)

        findViewById<Button>(R.id.save_keys_button).setOnClickListener {
            prefs.edit()
                .putString("gemini_key", geminiInput.text.toString().trim())
                .putString("gemini_model_id", geminiModelSpinner.selectedItem as String)
                .putString("admin_token", adminTokenInputForSave.text.toString().trim())
                .apply()
            Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show()
        }

        setUpVoiceSection(prefs)
        setUpTuningControls(prefs)

        if (host.isNotEmpty()) {
            loadStatus(host, port, token)
        } else {
            findViewById<TextView>(R.id.settings_persona_info).text = "(not paired -- pair with a desktop to see this)"
            findViewById<TextView>(R.id.settings_models_list).text = ""
        }

        // Admin section -- deliberately a separate token from the regular
        // pairing token above (see server.py's get_or_create_admin_token
        // for why). Nothing here is reachable without the user manually
        // entering this token themselves.
        val adminTokenInput = findViewById<EditText>(R.id.admin_token_input)
        val adminCommandInput = findViewById<EditText>(R.id.admin_command_input)
        val adminResultOutput = findViewById<TextView>(R.id.admin_result_output)
        adminTokenInput.setText(prefs.getString("admin_token", ""))

        findViewById<Button>(R.id.admin_run_button).setOnClickListener {
            val adminToken = adminTokenInput.text.toString().trim()
            val command = adminCommandInput.text.toString().trim()
            if (adminToken.isEmpty()) {
                Toast.makeText(this, "Enter the admin token first", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            if (command.isEmpty()) {
                Toast.makeText(this, "Enter a command to run", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            if (host.isEmpty()) {
                Toast.makeText(this, "Not paired with a desktop", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            prefs.edit().putString("admin_token", adminToken).apply()
            adminResultOutput.text = "Running..."
            runAdminCommand(host, port, adminToken, command, adminResultOutput)
        }

        findViewById<Button>(R.id.admin_reboot_button).setOnClickListener {
            val adminToken = adminTokenInput.text.toString().trim()
            if (adminToken.isEmpty()) {
                Toast.makeText(this, "Enter the admin token first", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            if (host.isEmpty()) {
                Toast.makeText(this, "Not paired with a desktop", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            prefs.edit().putString("admin_token", adminToken).apply()
            AlertDialog.Builder(this)
                .setTitle("Reboot desktop?")
                .setMessage("This will reboot $host right now. It should come back up and reconnect on its own if auto-start is set up.")
                .setPositiveButton("Reboot") { _, _ -> triggerReboot(host, port, adminToken) }
                .setNegativeButton("Cancel", null)
                .show()
        }

        // Override (sudo) toggle + Magic's can-do list (added programmatically)
        val root = findViewById<LinearLayout>(R.id.settings_root)
        val overrideBtn = Button(this).apply { text = "Override (sudo)" }
        root.addView(overrideBtn)
        overrideBtn.setOnClickListener { toggleOverride(prefs, overrideBtn) }
        val canDoBtn = Button(this).apply { text = "Magic's can-do list" }
        root.addView(canDoBtn)
        canDoBtn.setOnClickListener { showCanDo(prefs) }
        refreshOverrideLabel(prefs, overrideBtn)
    }

    // ----------------------------------------- override (sudo) + can-do list

    private fun creds(prefs: SharedPreferences): Triple<String, Int, String>? {
        val h = prefs.getString("host", null); val p = prefs.getInt("port", 0)
        val t = prefs.getString("token", null)
        return if (h != null && p != 0 && t != null) Triple(h, p, t) else null
    }

    private fun httpJson(host: String, port: Int, token: String, path: String,
                         method: String, body: String?): JSONObject? = try {
        val c = URL("http://$host:$port$path").openConnection() as HttpURLConnection
        c.requestMethod = method
        c.setRequestProperty("Authorization", "Bearer $token")
        c.connectTimeout = 6_000; c.readTimeout = 10_000
        if (body != null) {
            c.doOutput = true
            c.setRequestProperty("Content-Type", "application/json")
            OutputStreamWriter(c.outputStream).use { it.write(body) }
        }
        val code = c.responseCode
        val stream = if (code in 200..299) c.inputStream else c.errorStream
        JSONObject(stream.bufferedReader().use { it.readText() })
    } catch (e: Exception) { null }

    private fun refreshOverrideLabel(prefs: SharedPreferences, btn: Button) {
        val c = creds(prefs) ?: return
        Thread {
            val st = httpJson(c.first, c.second, c.third, "/override/status", "GET", null)
            val active = st?.optBoolean("active") ?: false
            runOnUiThread {
                btn.text = if (active) "Override: ON — tap to turn off" else "Override (sudo) — tap to turn on"
            }
        }.start()
    }

    private fun toggleOverride(prefs: SharedPreferences, btn: Button) {
        val c = creds(prefs) ?: run { Toast.makeText(this, "Pair with a desktop first", Toast.LENGTH_SHORT).show(); return }
        Thread {
            val st = httpJson(c.first, c.second, c.third, "/override/status", "GET", null)
            val active = st?.optBoolean("active") ?: false
            val res = httpJson(c.first, c.second, c.third, if (active) "/override/off" else "/override/on", "POST", "{}")
            runOnUiThread {
                val nowOn = res?.optBoolean("active") ?: false
                Toast.makeText(this,
                    if (nowOn) "Override ON — sudo mode. Auto-off after 5 min idle."
                    else "Override OFF — normal rules are back.", Toast.LENGTH_LONG).show()
                refreshOverrideLabel(prefs, btn)
            }
        }.start()
    }

    private fun showCanDo(prefs: SharedPreferences) {
        val c = creds(prefs) ?: run { Toast.makeText(this, "Pair with a desktop first", Toast.LENGTH_SHORT).show(); return }
        val pad = (16 * resources.displayMetrics.density).toInt()
        val header = TextView(this).apply { setPadding(pad, pad, pad, pad / 2); text = "Loading…" }
        val listBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, 0, pad, pad / 2) }
        val scroll = ScrollView(this).apply { addView(listBox) }
        val input = EditText(this).apply { hint = "add a service (e.g. magic-zoid.timer)" }
        val inputWrap = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, 0, pad, 0); addView(input) }
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(header)
            addView(scroll, LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                (220 * resources.displayMetrics.density).toInt()
            ))
            addView(inputWrap)
        }

        fun renderItems(arr: JSONArray?) {
            listBox.removeAllViews()
            if (arr == null || arr.length() == 0) {
                header.text = "Magic can control:"
                listBox.addView(TextView(this).apply { text = "(nothing yet)" })
                return
            }
            header.text = "Magic can control (tap Delete to remove):"
            for (i in 0 until arr.length()) {
                val itemName = arr.getString(i)
                val row = LinearLayout(this).apply {
                    orientation = LinearLayout.HORIZONTAL
                    gravity = Gravity.CENTER_VERTICAL
                    setPadding(0, pad / 4, 0, pad / 4)
                }
                val label = TextView(this).apply {
                    text = "• $itemName"
                    setTextIsSelectable(true)
                    layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
                }
                val delBtn = Button(this).apply {
                    text = "Delete"
                    setOnClickListener {
                        isEnabled = false
                        Thread {
                            val res = httpJson(
                                c.first, c.second, c.third, "/allowlist", "POST",
                                JSONObject().put("action", "remove").put("name", itemName).toString()
                            )
                            val updated = res?.optJSONArray("allowed")
                            runOnUiThread {
                                if (updated != null) {
                                    renderItems(updated)
                                    Toast.makeText(this@SettingsActivity, "Removed $itemName", Toast.LENGTH_SHORT).show()
                                } else {
                                    isEnabled = true
                                    Toast.makeText(this@SettingsActivity, "Failed to remove $itemName", Toast.LENGTH_SHORT).show()
                                }
                            }
                        }.start()
                    }
                }
                row.addView(label)
                row.addView(delBtn)
                listBox.addView(row)
            }
        }

        AlertDialog.Builder(this).setTitle("Magic's can-do list").setView(box)
            .setPositiveButton("Add") { _, _ ->
                val name = input.text.toString().trim()
                if (name.isNotEmpty()) Thread {
                    val kind = if (name.contains(".")) "systemd" else "docker"
                    httpJson(c.first, c.second, c.third, "/allowlist", "POST",
                        JSONObject().put("kind", kind).put("name", name).toString())
                    runOnUiThread { Toast.makeText(this, "Added $name", Toast.LENGTH_SHORT).show() }
                }.start()
            }
            .setNegativeButton("Close", null).show()
        Thread {
            val j = httpJson(c.first, c.second, c.third, "/allowlist", "GET", null)
            val arr = j?.optJSONArray("allowed")
            runOnUiThread { renderItems(arr) }
        }.start()
    }

    private fun setUpTuningControls(prefs: SharedPreferences) {
        val statusLabel = findViewById<TextView>(R.id.controls_status_label)
        val cvecSpinner = findViewById<Spinner>(R.id.controls_cvec_spinner)
        val alphaValue = findViewById<TextView>(R.id.controls_alpha_value)
        val alphaResetBtn = findViewById<Button>(R.id.controls_alpha_reset_btn)
        val alphaSeekBar = findViewById<SeekBar>(R.id.controls_alpha_seekbar)
        val alphaWarning = findViewById<TextView>(R.id.controls_alpha_warning)
        val layersDefaultBtn = findViewById<Button>(R.id.controls_layers_default)
        val layersFocusedBtn = findViewById<Button>(R.id.controls_layers_focused)
        val layersWideBtn = findViewById<Button>(R.id.controls_layers_wide)
        val thinkAutoBtn = findViewById<Button>(R.id.controls_think_auto)
        val thinkOnBtn = findViewById<Button>(R.id.controls_think_on)
        val thinkOffBtn = findViewById<Button>(R.id.controls_think_off)
        val hooksMasterSwitch = findViewById<SwitchMaterial>(R.id.controls_hooks_master_switch)
        val hooksList = findViewById<LinearLayout>(R.id.controls_hooks_list)
        val rerankerSwitch = findViewById<SwitchMaterial>(R.id.controls_reranker_switch)

        val client = MagicClient(prefs, applicationContext)
        val layerButtons = listOf(layersDefaultBtn, layersFocusedBtn, layersWideBtn)
        val thinkButtons = listOf(thinkAutoBtn, thinkOnBtn, thinkOffBtn)

        var hydrating = false
        var vecOptions = listOf("Off (none)")
        var syncedVecLabel = "Off (none)"
        var selectedLayerPreset = "default"
        var currentNLayers = 0
        var lastRenderedSnapshot: JSONObject? = null

        fun setControlsEnabled(enabled: Boolean) {
            cvecSpinner.isEnabled = enabled
            alphaResetBtn.isEnabled = enabled
            alphaSeekBar.isEnabled = enabled
            layerButtons.forEach { it.isEnabled = enabled }
            thinkButtons.forEach { it.isEnabled = enabled }
            hooksMasterSwitch.isEnabled = enabled
            rerankerSwitch.isEnabled = enabled
        }

        fun formatAlpha(a: Double): String =
            if (abs(a) < 0.005) "0.00" else String.format(Locale.US, "%+.2f", a)

        fun progressToAlpha(progress: Int): Double =
            ((progress - 30) * 0.05 * 100.0).roundToInt() / 100.0

        fun alphaToProgress(alpha: Double): Int =
            ((alpha / 0.05) + 30.0).roundToInt().coerceIn(0, 60)

        fun updateAlphaReadout(alpha: Double, serverWarning: String = "") {
            alphaValue.text = formatAlpha(alpha)
            val warnText = when {
                serverWarning.isNotEmpty() -> serverWarning
                abs(alpha) > 1.25 -> "High steering strength (|alpha| > 1.25) may degrade coherence."
                else -> ""
            }
            if (warnText.isNotEmpty()) {
                alphaWarning.text = warnText
                alphaWarning.visibility = View.VISIBLE
            } else {
                alphaWarning.text = ""
                alphaWarning.visibility = View.GONE
            }
        }

        fun styleButtonGroup(active: Button, group: List<Button>) {
            group.forEach { btn ->
                val isSelected = btn === active
                btn.setBackgroundResource(if (isSelected) R.drawable.bg_btn_primary else R.drawable.bg_btn)
                btn.alpha = if (isSelected) 1.0f else 0.72f
            }
        }

        fun layerBoundsForPreset(preset: String, nLayers: Int): Pair<Int?, Int?> {
            if (nLayers <= 0 || preset == "default") return null to null
            return when (preset) {
                "focused" -> {
                    val s = nLayers / 2
                    val e = maxOf(s, (nLayers * 5) / 6)
                    s to e
                }
                "wide" -> {
                    val s = nLayers / 4
                    val e = maxOf(s, (nLayers * 7) / 8)
                    s to e
                }
                else -> null to null
            }
        }

        fun inferLayerPreset(ilStart: Int, ilEnd: Int, nLayers: Int): String {
            if (nLayers <= 0 || ilStart < 0 || ilEnd < 0) return "default"
            val focused = layerBoundsForPreset("focused", nLayers)
            if (ilStart == focused.first && ilEnd == focused.second) return "focused"
            val wide = layerBoundsForPreset("wide", nLayers)
            if (ilStart == wide.first && ilEnd == wide.second) return "wide"
            return "default"
        }

        fun renderControlsSnapshot(json: JSONObject) {
            hydrating = true
            try {
                lastRenderedSnapshot = json
                val cv = json.optJSONObject("control_vector") ?: JSONObject()
                val cvActive = cv.optBoolean("active", false)
                val cvName = if (cv.isNull("name")) "" else cv.optString("name", "")
                val alpha = if (cvActive) cv.optDouble("alpha", 0.0) else 0.0
                val ilStart = cv.optInt("il_start", -1)
                val ilEnd = cv.optInt("il_end", -1)
                currentNLayers = cv.optInt("n_layers", 0)
                val cvWarning = if (cv.isNull("warning")) "" else cv.optString("warning", "")

                val availArr = cv.optJSONArray("available_vectors")
                val nextOptions = mutableListOf("Off (none)")
                if (availArr != null) {
                    for (i in 0 until availArr.length()) {
                        val item = availArr.optString(i, "")
                        if (item.isNotEmpty()) nextOptions.add(item)
                    }
                }
                if (nextOptions != vecOptions || cvecSpinner.adapter == null) {
                    vecOptions = nextOptions
                    val adapter = ArrayAdapter(this, android.R.layout.simple_spinner_item, vecOptions)
                    adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item)
                    cvecSpinner.adapter = adapter
                }
                val selIdx = if (cvActive && cvName.isNotEmpty() && vecOptions.contains(cvName)) {
                    vecOptions.indexOf(cvName)
                } else {
                    0
                }
                syncedVecLabel = vecOptions[selIdx]
                cvecSpinner.setSelection(selIdx, false)

                alphaSeekBar.progress = alphaToProgress(alpha)
                updateAlphaReadout(alpha, cvWarning)

                if (cvActive) {
                    selectedLayerPreset = inferLayerPreset(ilStart, ilEnd, currentNLayers)
                }
                val activeLayerBtn = when (selectedLayerPreset) {
                    "focused" -> layersFocusedBtn
                    "wide" -> layersWideBtn
                    else -> layersDefaultBtn
                }
                styleButtonGroup(activeLayerBtn, layerButtons)

                val thinkMode = json.optString("thinking_mode", "auto")
                val activeThinkBtn = when (thinkMode) {
                    "on" -> thinkOnBtn
                    "off" -> thinkOffBtn
                    else -> thinkAutoBtn
                }
                styleButtonGroup(activeThinkBtn, thinkButtons)

                val hooksObj = json.optJSONObject("hooks") ?: JSONObject()
                val hooksMaster = hooksObj.optBoolean("enabled", true)
                hooksMasterSwitch.isChecked = hooksMaster

                val hooksArr = hooksObj.optJSONArray("items")
                hooksList.removeAllViews()
                if (hooksArr != null) {
                    val pad = (4 * resources.displayMetrics.density).toInt()
                    for (i in 0 until hooksArr.length()) {
                        val h = hooksArr.optJSONObject(i) ?: continue
                        val hName = h.optString("name", "")
                        if (hName.isEmpty()) continue
                        val hEvent = h.optString("event", "")
                        val hEnabled = h.optBoolean("enabled", true)
                        val hDesc = h.optString("description", "")

                        val row = LinearLayout(this).apply {
                            orientation = LinearLayout.VERTICAL
                            setPadding(0, pad, 0, pad)
                        }
                        val sw = SwitchMaterial(this).apply {
                            text = hName
                            textSize = 13f
                            isChecked = hEnabled
                            isEnabled = hooksMaster
                            setOnCheckedChangeListener { _, checked ->
                                if (hydrating) return@setOnCheckedChangeListener
                                val patch = JSONObject().put(
                                    "hooks",
                                    JSONObject().put("hook_states", JSONObject().put(hName, checked))
                                )
                                statusLabel.text = "Updating hook $hName..."
                                Thread {
                                    val (updated, err) = client.postControls(patch)
                                    runOnUiThread {
                                        if (updated != null) {
                                            renderControlsSnapshot(updated)
                                        } else {
                                            lastRenderedSnapshot?.let { renderControlsSnapshot(it) }
                                            statusLabel.text = "Failed: ${err ?: "error"}"
                                            Toast.makeText(this@SettingsActivity, err ?: "Failed", Toast.LENGTH_SHORT).show()
                                        }
                                    }
                                }.start()
                            }
                        }
                        row.addView(sw)
                        if (hDesc.isNotEmpty() || hEvent.isNotEmpty()) {
                            val caption = TextView(this).apply {
                                text = if (hDesc.isNotEmpty()) "$hEvent · $hDesc" else hEvent
                                textSize = 11f
                                this.alpha = 0.7f
                            }
                            row.addView(caption)
                        }
                        hooksList.addView(row)
                    }
                }

                rerankerSwitch.isChecked = json.optBoolean("reranker_enabled", true)

                val steerSummary = if (cvActive && cvName.isNotEmpty()) {
                    "$cvName (${formatAlpha(alpha)}, L$ilStart–$ilEnd)"
                } else {
                    "off"
                }
                val hookLabel = if (hooksMaster) "on" else "off"
                statusLabel.text = "Live · Steer: $steerSummary · Think: $thinkMode · Hooks: $hookLabel"
                setControlsEnabled(true)
            } finally {
                hydrating = false
            }
        }

        fun postPatch(patch: JSONObject, busyMessage: String) {
            statusLabel.text = busyMessage
            Thread {
                val (updated, err) = client.postControls(patch)
                runOnUiThread {
                    if (updated != null) {
                        renderControlsSnapshot(updated)
                    } else {
                        lastRenderedSnapshot?.let { renderControlsSnapshot(it) }
                        statusLabel.text = "Failed: ${err ?: "error"}"
                        Toast.makeText(this@SettingsActivity, err ?: "Controls update failed", Toast.LENGTH_SHORT).show()
                    }
                }
            }.start()
        }

        fun buildVectorPatch(vecLabel: String, alpha: Double, preset: String): JSONObject {
            if (vecLabel == "Off (none)" || abs(alpha) < 0.005) {
                return JSONObject().put("control_vector", JSONObject().put("off", true))
            }
            val cvPatch = JSONObject()
                .put("name", vecLabel)
                .put("alpha", alpha)
            val (ilStart, ilEnd) = layerBoundsForPreset(preset, currentNLayers)
            if (ilStart != null && ilEnd != null) {
                cvPatch.put("il_start", ilStart).put("il_end", ilEnd)
            }
            return JSONObject().put("control_vector", cvPatch)
        }

        if (creds(prefs) == null) {
            statusLabel.text = "Pair with a desktop to adjust live tuning controls."
            setControlsEnabled(false)
            return
        }

        setControlsEnabled(false)
        statusLabel.text = "Loading controls from desktop..."
        Thread {
            val (snap, err) = client.getControls()
            runOnUiThread {
                if (snap != null) {
                    renderControlsSnapshot(snap)
                } else {
                    statusLabel.text = "Couldn't load controls: ${err ?: "desktop unreachable"}"
                }
            }
        }.start()

        cvecSpinner.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: AdapterView<*>?, view: View?, pos: Int, id: Long) {
                if (hydrating || pos !in vecOptions.indices) return
                val chosen = vecOptions[pos]
                if (chosen == syncedVecLabel) return
                syncedVecLabel = chosen
                val currentAlpha = progressToAlpha(alphaSeekBar.progress)
                val targetAlpha = if (chosen == "Off (none)") 0.0 else if (abs(currentAlpha) < 0.005) 0.5 else currentAlpha
                postPatch(buildVectorPatch(chosen, targetAlpha, selectedLayerPreset), "Updating control vector...")
            }

            override fun onNothingSelected(parent: AdapterView<*>?) {}
        }

        alphaSeekBar.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: SeekBar?, progress: Int, fromUser: Boolean) {
                val a = progressToAlpha(progress)
                updateAlphaReadout(a)
            }

            override fun onStartTrackingTouch(sb: SeekBar?) {}

            override fun onStopTrackingTouch(sb: SeekBar?) {
                if (hydrating || sb == null) return
                val a = progressToAlpha(sb.progress)
                val chosen = vecOptions.getOrElse(cvecSpinner.selectedItemPosition) { "Off (none)" }
                if (chosen == "Off (none)") {
                    if (abs(a) >= 0.005) {
                        Toast.makeText(this@SettingsActivity, "Pick a control vector above first.", Toast.LENGTH_SHORT).show()
                        hydrating = true
                        sb.progress = 30
                        updateAlphaReadout(0.0)
                        hydrating = false
                    }
                    return
                }
                postPatch(buildVectorPatch(chosen, a, selectedLayerPreset), "Applying steering (${formatAlpha(a)})...")
            }
        })

        alphaResetBtn.setOnClickListener {
            postPatch(JSONObject().put("control_vector", JSONObject().put("off", true)), "Clearing steering...")
        }

        val onLayerPresetClick: (String, Button) -> Unit = { preset, btn ->
            selectedLayerPreset = preset
            styleButtonGroup(btn, layerButtons)
            val chosen = vecOptions.getOrElse(cvecSpinner.selectedItemPosition) { "Off (none)" }
            val a = progressToAlpha(alphaSeekBar.progress)
            if (chosen != "Off (none)" && abs(a) >= 0.005) {
                postPatch(buildVectorPatch(chosen, a, preset), "Applying layer range ($preset)...")
            }
        }
        layersDefaultBtn.setOnClickListener { onLayerPresetClick("default", layersDefaultBtn) }
        layersFocusedBtn.setOnClickListener { onLayerPresetClick("focused", layersFocusedBtn) }
        layersWideBtn.setOnClickListener { onLayerPresetClick("wide", layersWideBtn) }

        thinkAutoBtn.setOnClickListener {
            postPatch(JSONObject().put("thinking_mode", "auto"), "Setting thinking mode: auto...")
        }
        thinkOnBtn.setOnClickListener {
            postPatch(JSONObject().put("thinking_mode", "on"), "Setting thinking mode: on...")
        }
        thinkOffBtn.setOnClickListener {
            postPatch(JSONObject().put("thinking_mode", "off"), "Setting thinking mode: off...")
        }

        hooksMasterSwitch.setOnCheckedChangeListener { _, checked ->
            if (hydrating) return@setOnCheckedChangeListener
            postPatch(
                JSONObject().put("hooks", JSONObject().put("enabled", checked)),
                if (checked) "Enabling hooks..." else "Disabling hooks..."
            )
        }

        rerankerSwitch.setOnCheckedChangeListener { _, checked ->
            if (hydrating) return@setOnCheckedChangeListener
            postPatch(
                JSONObject().put("reranker_enabled", checked),
                if (checked) "Enabling CPU reranker..." else "Disabling CPU reranker..."
            )
        }
    }

    /** Just a reference -- consolidated chat commands + desktop admin
     * commands. Chat commands run from MainActivity.handleSlashCommand;
     * live toggles live right above in Tuning & Controls. */
    private fun showCommandsReference() {
        val text = """
            Core & Reasoning (in main chat):
            - /chat [clear]  Plain talk or reset thread (/clear, /reset also work)
            - /deep <q>      Deep reasoning turn (/think <q> is an alias)
            - /do <task>     Live read-only system probe (/do learn distills skill)
            - /inspect <t>   Reverse-engineer binary, APK, JS/WASM, or URL
            - /defense <k>   Security audit: surface | updates | ssh | secrets | report
            - /override <m>  Unfiltered local reply for one message

            Build, Repair & Tools:
            - /build <spec>  Build a script, project, or Android APK on desktop
            - /fix <goal>    Run battle loop on Magic's harness
            - /forge / /recipe   Custom sandboxed tools & saved workflows

            Memory, Skills & System:
            - /memory | /profile | /skill | /model | /doctor
            - Steering, thinking mode, hooks & reranker live in Settings -> Tuning & Controls.

            Desktop admin commands (require Admin token below):
            - /desktop <command>   Run sandboxed desktop command
            - /root <command>      Run command with sudo
            - /reboot              Reboot desktop (confirm required)
            - /snapshots           List BTRFS snapshots
            - /rollback <num>      Roll back to BTRFS snapshot and reboot
            - /updatecheck         Check pending pacman updates against Manjaro forum
            - /fix <path> <issue>  Fix a user script/config on desktop
        """.trimIndent()

        AlertDialog.Builder(this)
            .setTitle("Magic commands")
            .setMessage(text)
            .setPositiveButton("Close", null)
            .show()
    }

    private fun runAdminCommand(host: String, port: Int, adminToken: String, command: String, output: TextView) {
        Thread {
            try {
                val url = URL("http://$host:$port/admin/execute")
                val connection = url.openConnection() as HttpURLConnection
                connection.requestMethod = "POST"
                connection.setRequestProperty("Content-Type", "application/json")
                connection.setRequestProperty("X-Admin-Token", adminToken)
                connection.doOutput = true
                connection.connectTimeout = 8_000
                connection.readTimeout = 130_000

                val body = JSONObject().apply { put("command", command) }
                OutputStreamWriter(connection.outputStream).use { it.write(body.toString()) }

                val responseCode = connection.responseCode
                val stream = if (responseCode in 200..299) connection.inputStream else connection.errorStream
                val json = JSONObject(stream.bufferedReader().use { it.readText() })

                runOnUiThread {
                    output.text = if (responseCode in 200..299) {
                        "exit ${json.optInt("exit_code")}\n${json.optString("stdout")}\n${json.optString("stderr")}".trim()
                    } else {
                        "Error: ${json.optString("error", "HTTP $responseCode")}"
                    }
                }
            } catch (e: Exception) {
                runOnUiThread { output.text = "Couldn't reach desktop: ${e.message}" }
            }
        }.start()
    }

    private fun triggerReboot(host: String, port: Int, adminToken: String) {
        Thread {
            try {
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
                runOnUiThread {
                    Toast.makeText(
                        this,
                        if (responseCode in 200..299) "Reboot triggered" else "Reboot failed (HTTP $responseCode)",
                        Toast.LENGTH_LONG,
                    ).show()
                }
            } catch (e: Exception) {
                // A connection drop here is actually the expected/good
                // outcome once the reboot really starts -- don't treat
                // every exception as a failure worth alarming over.
                runOnUiThread { Toast.makeText(this, "Reboot request sent", Toast.LENGTH_LONG).show() }
            }
        }.start()
    }



    /** Speaks replies aloud via Android's built-in TTS -- see
     * VoiceOutput.kt for the actual speak-on-reply logic (that lives in
     * MainActivity, since that's where replies actually arrive). This
     * just edits the prefs it reads (voice_enabled/voice_pitch/voice_rate)
     * and offers a way to preview them before leaving this screen. */
    private fun setUpVoiceSection(prefs: SharedPreferences) {
        val enabledCheckbox = findViewById<CheckBox>(R.id.voice_enabled_checkbox)
        val pitchSeekBar = findViewById<SeekBar>(R.id.voice_pitch_seekbar)
        val rateSeekBar = findViewById<SeekBar>(R.id.voice_rate_seekbar)
        val testButton = findViewById<Button>(R.id.voice_test_button)

        enabledCheckbox.isChecked = prefs.getBoolean("voice_enabled", false)
        pitchSeekBar.progress = (prefs.getFloat("voice_pitch", VoiceOutput.DEFAULT_PITCH) * 100).toInt()
        rateSeekBar.progress = (prefs.getFloat("voice_rate", VoiceOutput.DEFAULT_RATE) * 100).toInt()

        enabledCheckbox.setOnCheckedChangeListener { _, checked ->
            prefs.edit().putBoolean("voice_enabled", checked).apply()
        }

        // Saved on every move rather than only e.g. on stopTrackingTouch --
        // simpler, and a SeekBar drag is cheap enough that there's no
        // real cost to just always being up to date.
        pitchSeekBar.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(seekBar: SeekBar, progress: Int, fromUser: Boolean) {
                prefs.edit().putFloat("voice_pitch", (progress.coerceAtLeast(10)) / 100f).apply()
            }
            override fun onStartTrackingTouch(seekBar: SeekBar) {}
            override fun onStopTrackingTouch(seekBar: SeekBar) {}
        })
        rateSeekBar.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(seekBar: SeekBar, progress: Int, fromUser: Boolean) {
                prefs.edit().putFloat("voice_rate", (progress.coerceAtLeast(10)) / 100f).apply()
            }
            override fun onStartTrackingTouch(seekBar: SeekBar) {}
            override fun onStopTrackingTouch(seekBar: SeekBar) {}
        })

        testButton.setOnClickListener {
            if (testTts == null) {
                testTts = TextToSpeech(applicationContext) { status ->
                    if (status == TextToSpeech.SUCCESS) {
                        testTts?.language = Locale.US
                        speakTest(prefs)
                    } else {
                        Toast.makeText(this, "No text-to-speech engine available", Toast.LENGTH_LONG).show()
                    }
                }
            } else {
                speakTest(prefs)
            }
        }
    }

    private fun speakTest(prefs: SharedPreferences) {
        val engine = testTts ?: return
        engine.setPitch(prefs.getFloat("voice_pitch", VoiceOutput.DEFAULT_PITCH))
        engine.setSpeechRate(prefs.getFloat("voice_rate", VoiceOutput.DEFAULT_RATE))
        engine.speak("This is what I sound like.", TextToSpeech.QUEUE_FLUSH, null, "voice-test")
    }

    private fun setUpModelSpinner(spinner: Spinner, choicesArrayRes: Int, savedValue: String?) {
        val adapter = ArrayAdapter.createFromResource(this, choicesArrayRes, android.R.layout.simple_spinner_item)
        adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item)
        spinner.adapter = adapter

        if (savedValue != null) {
            val choices = resources.getStringArray(choicesArrayRes)
            val index = choices.indexOf(savedValue)
            if (index >= 0) spinner.setSelection(index)
            // if savedValue isn't one of the current choices (e.g. an
            // older saved model id), just leaves the default (index 0)
            // selected rather than crashing on a bad index
        }
    }

    private fun setUpManualPairing(prefs: SharedPreferences, host: String, port: Int, token: String) {
        val addressInput = findViewById<EditText>(R.id.manual_address_input)
        val portInput = findViewById<EditText>(R.id.manual_port_input)
        val tokenInput = findViewById<EditText>(R.id.manual_token_input)
        val saveButton = findViewById<Button>(R.id.manual_pair_button)
        val result = findViewById<TextView>(R.id.manual_pair_result)

        addressInput.setText(host)
        if (port != 0) portInput.setText(port.toString())
        tokenInput.setText(token)

        saveButton.setOnClickListener {
            val target = try {
                ManualPairing.parse(
                    addressInput.text.toString(),
                    portInput.text.toString(),
                    tokenInput.text.toString(),
                )
            } catch (e: IllegalArgumentException) {
                result.text = e.message
                return@setOnClickListener
            }

            saveButton.isEnabled = false
            result.text = "Testing ${target.host}:${target.port}..."
            Thread {
                val problem = ManualPairing.check(target)
                runOnUiThread {
                    saveButton.isEnabled = true
                    if (problem != null) {
                        // The old pairing stays in place until a new one works.
                        result.text = problem
                    } else {
                        prefs.edit()
                            .putString("host", target.host)
                            .putInt("port", target.port)
                            .putString("token", target.token)
                            .apply()
                        Toast.makeText(this, "Paired with ${target.host}:${target.port}", Toast.LENGTH_LONG).show()
                        // Everything on this screen captured the old host in onCreate.
                        intent.putExtra("host", target.host)
                        intent.putExtra("port", target.port)
                        intent.putExtra("token", target.token)
                        recreate()
                    }
                }
            }.start()
        }
    }

    /** Read-only view of a desktop command's output. */
    private fun showDesktopInfo(title: String, command: String, args: String) {
        val text = TextView(this)
        val pad = (16 * resources.displayMetrics.density).toInt()
        text.setPadding(pad, pad, pad, pad)
        text.setTextIsSelectable(true)
        text.text = "Loading..."
        val scroll = android.widget.ScrollView(this)
        scroll.addView(text)
        AlertDialog.Builder(this).setTitle(title).setView(scroll).setPositiveButton("Close", null).show()
        val prefs = getSharedPreferences("magic_prefs", MODE_PRIVATE)
        val client = MagicClient(prefs, applicationContext)
        Thread {
            val answer = client.command(command, args)
            runOnUiThread { text.text = answer }
        }.start()
    }

    private fun startQrScan() {
        // Re-pairing from Settings hands off to the same scan flow
        // MainActivity uses -- simplest way to reuse it without
        // duplicating the zxing integration is to just finish back to
        // MainActivity and let the user tap "Pair with Desktop" there.
        Toast.makeText(this, "On the main screen, tap the ⋮ menu, then Pair with QR code", Toast.LENGTH_LONG).show()
        finish()
    }

    private fun loadStatus(host: String, port: Int, token: String) {
        Thread {
            try {
                val json = fetchStatus(host, port, token)
                runOnUiThread { renderStatus(json) }
            } catch (e: Exception) {
                runOnUiThread {
                    findViewById<TextView>(R.id.settings_persona_info).text = "Couldn't reach magic: ${e.message}"
                }
            }
        }.start()
    }

    private fun fetchStatus(host: String, port: Int, token: String): JSONObject {
        val url = URL("http://$host:$port/status")
        val connection = url.openConnection() as HttpURLConnection
        connection.requestMethod = "GET"
        connection.setRequestProperty("Authorization", "Bearer $token")
        connection.connectTimeout = 8_000
        connection.readTimeout = 8_000

        val responseCode = connection.responseCode
        val stream = if (responseCode in 200..299) connection.inputStream else connection.errorStream
        val text = stream.bufferedReader().use { it.readText() }
        return JSONObject(text)
    }

    private fun renderStatus(json: JSONObject) {
        val fallback = json.optJSONArray("fallback_models")
        val consult = json.optJSONArray("consult_models")

        val personaText = buildString {
            append("Primary: ").append(json.optString("primary_model", "(none)")).append("\n")
            append("Fallback: ").append(joinOrNone(fallback)).append("\n")
            append("Consult group: ").append(joinOrNone(consult)).append("\n")
            append("Last resort: ").append(json.optString("last_resort_model", "(none)"))
        }
        findViewById<TextView>(R.id.settings_persona_info).text = personaText

        val models = json.optJSONArray("models")
        val modelsText = buildString {
            if (models == null || models.length() == 0) {
                append("(none)")
            } else {
                for (i in 0 until models.length()) {
                    val m = models.getJSONObject(i)
                    append("• ").append(m.optString("name")).append("  (").append(m.optString("type")).append(")")
                    if (i != models.length() - 1) append("\n")
                }
            }
        }
        findViewById<TextView>(R.id.settings_models_list).text = modelsText
    }

    private fun joinOrNone(arr: org.json.JSONArray?): String {
        if (arr == null || arr.length() == 0) return "(none)"
        return (0 until arr.length()).joinToString(", ") { arr.getString(it) }
    }
}
