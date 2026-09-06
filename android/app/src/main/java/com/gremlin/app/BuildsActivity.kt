package com.gremlin.app

import android.os.Bundle
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.ListView
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity

/**
 * Settings -> Builds: everything Gremlin built on the desktop (scripts,
 * projects, Android app source), listed here, tap to download the zip to
 * the phone. Backed by the /builds endpoints + GremlinClient. The only
 * app screen beyond chat.
 */
class BuildsActivity : AppCompatActivity() {

    private lateinit var client: GremlinClient
    private lateinit var status: TextView
    private lateinit var list: ListView
    private var builds: List<BuildInfo> = emptyList()
    private var pendingName: String? = null

    private val saveLauncher =
        registerForActivityResult(ActivityResultContracts.CreateDocument("application/zip")) { uri ->
            val name = pendingName; pendingName = null
            if (uri == null || name == null) return@registerForActivityResult
            status.text = "Downloading $name…"
            Thread {
                val err = try {
                    contentResolver.openOutputStream(uri)?.use { out -> client.downloadBuild(name, out) }
                        ?: "Couldn't open the file you picked"
                } catch (e: Exception) { "Save failed: ${e.message}" }
                runOnUiThread { status.text = if (err == null) "Saved $name.zip" else err }
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
            pendingName = b.name
            saveLauncher.launch("${b.name}.zip")
        }
        refresh()
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
                        status.text = "Tap a build to download it to your phone."
                        list.visibility = View.VISIBLE
                        list.adapter = object : ArrayAdapter<BuildInfo>(
                            this@BuildsActivity, android.R.layout.simple_list_item_2,
                            android.R.id.text1, items
                        ) {
                            override fun getView(p: Int, cv: View?, parent: ViewGroup): View {
                                val row = super.getView(p, cv, parent)
                                val b = items[p]
                                row.findViewById<TextView>(android.R.id.text1).text = b.name
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
