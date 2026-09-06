package com.gremlin.app

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.ListView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider

/**
 * Settings -> Builds: everything Gremlin built on the desktop (scripts,
 * projects, Android app source), listed here. Tap to pull it to the
 * phone. A build that's a single .apk downloads raw and goes straight
 * to the system installer -- no unzip. A single script downloads with
 * its real name. A multi-file project still comes down as a .zip.
 * Backed by the /builds endpoints + GremlinClient.
 */
class BuildsActivity : AppCompatActivity() {

    private lateinit var client: GremlinClient
    private lateinit var status: TextView
    private lateinit var list: ListView
    private var builds: List<BuildInfo> = emptyList()
    private var pending: BuildInfo? = null

    // Non-APK downloads (zip, or a single script) go through the system
    // file picker. The APK path skips this and installs directly.
    private val saveLauncher =
        registerForActivityResult(ActivityResultContracts.CreateDocument("application/octet-stream")) { uri ->
            val b = pending; pending = null
            if (uri == null || b == null) return@registerForActivityResult
            val raw = b.singleFile.isNotEmpty()
            status.text = "Downloading ${b.name}…"
            Thread {
                val err = try {
                    contentResolver.openOutputStream(uri)?.use { out ->
                        client.downloadBuild(b.name, out, raw = raw)
                    } ?: "Couldn't open the file you picked"
                } catch (e: Exception) { "Save failed: ${e.message}" }
                runOnUiThread {
                    status.text = if (err == null) "Saved." else err
                }
            }.start()
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_builds)
        title = "Desktop Builds"
        client = GremlinClient(getSharedPreferences("gremlin_prefs", MODE_PRIVATE), applicationContext)
        status = findViewById(R.id.builds_status)
        list = findViewById(R.id.builds_list)
        list.setOnItemClickListener { _, _, pos, _ ->
            val b = builds.getOrNull(pos) ?: return@setOnItemClickListener
            if (b.tooBig) {
                status.text = "${b.name} is too big to download over this link."
                return@setOnItemClickListener
            }
            when {
                b.isApk -> installApk(b)
                b.singleFile.isNotEmpty() -> { pending = b; saveLauncher.launch(b.singleFile) }
                else -> { pending = b; saveLauncher.launch("${b.name}.zip") }
            }
        }
        refresh()
    }

    private fun installApk(b: BuildInfo) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !packageManager.canRequestPackageInstalls()) {
            status.text = "Allow \"Install unknown apps\" for Gremlin, then tap the build again."
            try {
                startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:$packageName")))
            } catch (e: Exception) {
                Toast.makeText(this, "Enable it in Settings → Apps → Gremlin → Install unknown apps", Toast.LENGTH_LONG).show()
            }
            return
        }
        status.text = "Downloading ${b.singleFile}…"
        Thread {
            val file = client.downloadBuildToCache(b.name, b.singleFile)
            runOnUiThread {
                if (file == null) {
                    status.text = "Download failed."
                    return@runOnUiThread
                }
                status.text = "Opening installer…"
                try {
                    val uri = FileProvider.getUriForFile(this, "$packageName.fileprovider", file)
                    startActivity(Intent(Intent.ACTION_VIEW).apply {
                        setDataAndType(uri, "application/vnd.android.package-archive")
                        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
                    })
                } catch (e: Exception) {
                    status.text = "Couldn't open the installer: ${e.message}"
                }
            }
        }.start()
    }

    private fun refresh() {
        status.text = "Loading…"
        list.visibility = View.GONE
        Thread {
            val (items, err) = client.listBuilds()
            runOnUiThread {
                builds = items
                when {
                    err != null -> status.text = err
                    items.isEmpty() -> status.text = "Nothing built on the desktop yet."
                    else -> {
                        status.text = "Tap a build to pull it to your phone."
                        list.visibility = View.VISIBLE
                        list.adapter = object : ArrayAdapter<BuildInfo>(
                            this@BuildsActivity, android.R.layout.simple_list_item_2,
                            android.R.id.text1, items
                        ) {
                            override fun getView(p: Int, cv: View?, parent: ViewGroup): View {
                                val row = super.getView(p, cv, parent)
                                val b = items[p]
                                row.findViewById<TextView>(android.R.id.text1).text =
                                    if (b.isApk) "${b.name}  (install)" else b.name
                                val kb = b.sizeBytes / 1024
                                row.findViewById<TextView>(android.R.id.text2).text =
                                    "${b.goal}  ·  $kb KB, ${b.fileCount} files" +
                                        if (b.tooBig) "  (too big)" else ""
                                return row
                            }
                        }
                    }
                }
            }
        }.start()
    }
}
