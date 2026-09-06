package com.gremlin.app

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.ListView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * The recent-conversations list. Pick one to continue it, or start a new
 * chat. Returns the chosen thread id to MainActivity via setResult.
 */
class ConversationsActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_THREAD = "thread"
        const val EXTRA_TITLE = "title"
    }

    private lateinit var client: GremlinClient
    private lateinit var list: ListView
    private lateinit var status: TextView
    private var items: List<GremlinClient.ConversationInfo> = emptyList()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_conversations)
        title = "Conversations"
        client = GremlinClient(getSharedPreferences("gremlin_prefs", MODE_PRIVATE), applicationContext)
        status = findViewById(R.id.conv_status)
        list = findViewById(R.id.conv_list)

        findViewById<Button>(R.id.new_chat_button).setOnClickListener {
            status.text = "Starting…"
            Thread {
                val tid = client.newConversation()
                runOnUiThread {
                    if (tid == null) { status.text = "Couldn't reach the desktop."; return@runOnUiThread }
                    finishWith(tid, "New chat")
                }
            }.start()
        }
        list.setOnItemClickListener { _, _, pos, _ ->
            items.getOrNull(pos)?.let { finishWith(it.id, it.title) }
        }
        refresh()
    }

    private fun finishWith(thread: String, title: String) {
        setResult(Activity.RESULT_OK, Intent()
            .putExtra(EXTRA_THREAD, thread).putExtra(EXTRA_TITLE, title))
        finish()
    }

    private fun refresh() {
        status.text = "Loading…"
        Thread {
            val convos = client.listConversations()
            runOnUiThread {
                items = convos
                status.text = if (convos.isEmpty()) "No conversations yet — start one." else ""
                list.adapter = object : ArrayAdapter<GremlinClient.ConversationInfo>(
                    this, android.R.layout.simple_list_item_1, android.R.id.text1, convos
                ) {
                    override fun getView(p: Int, cv: View?, parent: ViewGroup): View {
                        val row = super.getView(p, cv, parent)
                        row.findViewById<TextView>(android.R.id.text1).text = convos[p].title
                        return row
                    }
                }
            }
        }.start()
    }
}
