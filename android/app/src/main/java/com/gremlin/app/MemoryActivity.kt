package com.gremlin.app

import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * More -> Memory: lists what Magic remembers about Mickey (profile items +
 * learned notes from GET /memory), each with a "Forget" button that calls
 * POST /memory/forget.
 */
class MemoryActivity : AppCompatActivity() {

    private lateinit var client: MagicClient
    private lateinit var status: TextView
    private lateinit var list: ListView
    private var items: List<MagicClient.MemoryItem> = emptyList()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_memory)
        title = "Memory"
        client = MagicClient(getSharedPreferences("magic_prefs", MODE_PRIVATE), applicationContext)
        status = findViewById(R.id.memory_status)
        list = findViewById(R.id.memory_list)
        refresh()
    }

    private fun forget(item: MagicClient.MemoryItem) {
        status.text = "Forgetting…"
        Thread {
            val ok = client.forgetMemory(item.text)
            runOnUiThread {
                if (isFinishing || isDestroyed) return@runOnUiThread
                if (ok) {
                    status.text = "Forgot: ${item.text}"
                    refresh()
                } else {
                    status.text = "Couldn't forget that item."
                }
            }
        }.start()
    }

    private fun refresh() {
        status.text = "Loading…"
        Thread {
            val (loaded, err) = client.listMemory()
            runOnUiThread {
                if (isFinishing || isDestroyed) return@runOnUiThread
                items = loaded
                when {
                    err != null -> status.text = err
                    loaded.isEmpty() -> status.text = "Nothing in memory yet."
                    else -> status.text = "What Magic remembers about you (${loaded.size})."
                }
                val d = resources.displayMetrics.density
                list.adapter = object : ArrayAdapter<MagicClient.MemoryItem>(
                    this@MemoryActivity, 0, loaded
                ) {
                    override fun getView(p: Int, cv: View?, parent: ViewGroup): View {
                        val item = loaded[p]
                        val row = LinearLayout(this@MemoryActivity).apply {
                            orientation = LinearLayout.HORIZONTAL
                            gravity = Gravity.CENTER_VERTICAL
                            val pv = (10 * d).toInt()
                            setPadding(0, pv, 0, pv)
                        }
                        val col = LinearLayout(this@MemoryActivity).apply {
                            orientation = LinearLayout.VERTICAL
                        }
                        val label = TextView(this@MemoryActivity).apply {
                            text = item.section
                            textSize = 11f
                            alpha = 0.7f
                        }
                        val body = TextView(this@MemoryActivity).apply {
                            text = item.text
                            textSize = 15f
                        }
                        col.addView(label)
                        col.addView(body)
                        val btn = Button(this@MemoryActivity).apply {
                            text = "Forget"
                            setOnClickListener { forget(item) }
                        }
                        row.addView(col, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).apply {
                            rightMargin = (8 * d).toInt()
                        })
                        row.addView(btn, LinearLayout.LayoutParams(
                            ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT))
                        return row
                    }
                }
            }
        }.start()
    }
}
