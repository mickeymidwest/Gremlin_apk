package com.gremlin.app

import android.app.AlertDialog
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * Settings -> How Mickey likes things: edits ~/Downloads/gremlin_profile.md
 * on the desktop through GET/PUT /profile. Saving sends back the version it
 * loaded; if Gremlin noticed something new in the meantime the desktop
 * refuses, and this offers to reload instead of overwriting the guess.
 */
class ProfileActivity : AppCompatActivity() {

    private lateinit var client: GremlinClient
    private lateinit var editor: EditText
    private lateinit var status: TextView
    private lateinit var save: Button
    private lateinit var reload: Button
    private var version: String? = null
    private var loadedText: String = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_profile)
        title = "How Mickey likes things"
        client = GremlinClient(getSharedPreferences("gremlin_prefs", MODE_PRIVATE), applicationContext)
        editor = findViewById(R.id.profile_editor)
        status = findViewById(R.id.profile_status)
        save = findViewById(R.id.profile_save_button)
        save.setOnClickListener { saveProfile() }
        reload = findViewById(R.id.profile_reload_button)
        reload.setOnClickListener { confirmReload() }
        load()
    }

    private fun setBusy(busy: Boolean) {
        save.isEnabled = !busy && version != null
        reload.isEnabled = !busy
        editor.isEnabled = !busy
    }

    private fun load() {
        status.text = "Loading from the desktop…"
        setBusy(true)
        Thread {
            val r = client.getProfile()
            runOnUiThread {
                if (isFinishing || isDestroyed) return@runOnUiThread
                if (r.text != null) {
                    showText(r.text, r.version)
                    status.text = "Gremlin reads this before every chat. Lines under Noticed are its guesses."
                } else {
                    status.text = r.error ?: "Couldn't load the profile."
                }
                setBusy(false)
            }
        }.start()
    }

    private fun showText(text: String, ver: String?) {
        loadedText = text
        version = ver
        editor.setText(text)
    }

    private fun confirmReload() {
        if (editor.text.toString() == loadedText) { load(); return }
        AlertDialog.Builder(this)
            .setMessage("Throw away your changes and reload from the desktop?")
            .setPositiveButton("Reload") { _, _ -> load() }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun saveProfile() {
        val ver = version ?: return
        val text = editor.text.toString()
        status.text = "Saving…"
        setBusy(true)
        Thread {
            val r = client.putProfile(text, ver)
            runOnUiThread {
                if (isFinishing || isDestroyed) return@runOnUiThread
                setBusy(false)
                when {
                    r.ok -> {
                        loadedText = text
                        version = r.version
                        status.text = "Saved. Gremlin uses it from the next message."
                    }
                    r.conflictText != null -> {
                        status.text = "Gremlin added something while you were editing."
                        AlertDialog.Builder(this)
                            .setMessage("The profile changed on the desktop since you opened it. " +
                                "Reload to see the new version? Your edits here will be lost, " +
                                "so copy anything you want to keep first.")
                            .setPositiveButton("Reload") { _, _ ->
                                showText(r.conflictText, r.version)
                                status.text = "Reloaded the desktop's version."
                            }
                            .setNegativeButton("Keep editing", null)
                            .show()
                    }
                    else -> status.text = r.error ?: "Save failed."
                }
            }
        }.start()
    }

    override fun onBackPressed() {
        if (editor.text.toString() != loadedText && version != null) {
            AlertDialog.Builder(this)
                .setMessage("You have unsaved changes.")
                .setPositiveButton("Save") { _, _ -> saveProfile() }
                .setNegativeButton("Discard") { _, _ -> finish() }
                .setNeutralButton("Cancel", null)
                .show()
            return
        }
        @Suppress("DEPRECATION")
        super.onBackPressed()
    }
}
