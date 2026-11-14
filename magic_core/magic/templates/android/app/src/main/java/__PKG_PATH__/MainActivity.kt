package __PKG__

import android.app.Activity
import android.os.Bundle
import android.view.Gravity
import android.widget.TextView

/**
 * Self-contained launcher. Compiles and runs on its own so the scaffold
 * is always a buildable app before any feature code exists. The scaffold
 * rewrites onCreate() to `setContentView(<PrimaryView>(this))` once the
 * plan names a primary view/screen class.
 */
class MainActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val tv = TextView(this)
        tv.text = getString(R.string.app_name)
        tv.textSize = 22f
        tv.gravity = Gravity.CENTER
        setContentView(tv)
    }
}
