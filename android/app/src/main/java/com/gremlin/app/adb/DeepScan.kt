package com.gremlin.app.adb

import android.content.Context
import android.os.Build
import org.json.JSONObject

/**
 * Deep scan (S4b): through the app's own ADB connection, ask Android which
 * apps used the mic, camera, location, texts, contacts, call log, clipboard
 * and files -- and whether they were in the background -- then let the
 * desktop turn that into a report (POST /guard/appops, same parser as
 * `magic adb-audit`).
 *
 * Read-only by construction: the only commands are the two below, and the
 * only thing ever put into one is a package name that `pm list packages`
 * returned, checked against a strict pattern.
 */
object DeepScan {

    // one pairing or scan at a time on the shared connection, even across activity recreation
    private val lock = Any()

    private val PKG = Regex("^[A-Za-z][A-Za-z0-9_]*(\\.[A-Za-z0-9_]+)+$")

    class Result(val ops: JSONObject? = null, val count: Int = 0, val error: String? = null)

    fun supported(): Boolean = Build.VERSION.SDK_INT >= Build.VERSION_CODES.R  // wireless debugging: Android 11+

    /** One-time pairing with the code from Wireless debugging > Pair device with pairing code. */
    fun pair(context: Context, port: Int, code: String): String? = synchronized(lock) { pairLocked(context, port, code) }

    private fun pairLocked(context: Context, port: Int, code: String): String? = try {
        if (!supported()) "Wireless debugging needs Android 11 or newer."
        else if (AdbManager.get(context).pair("127.0.0.1", port, code.trim())) null
        else "Pairing didn't go through. Check the port and the 6-digit code, then try again."
    } catch (e: Exception) {
        "Pairing failed: ${e.message}"
    }

    /** Connect (finding the port by itself when it can), then collect appops for every app you installed. */
    fun collect(context: Context, port: Int?): Result = synchronized(lock) { collectLocked(context, port) }

    private fun collectLocked(context: Context, port: Int?): Result {
        if (!supported()) return Result(error = "Wireless debugging needs Android 11 or newer.")
        val adb = try { AdbManager.get(context) } catch (e: Exception) {
            return Result(error = "Couldn't set up ADB: ${e.message}")
        }
        val connected = try {
            if (port != null) adb.connect("127.0.0.1", port) else adb.autoConnect(context, 5_000)
        } catch (e: Exception) { false }
        if (!connected) {
            return Result(error = "Couldn't connect. Turn on Wireless debugging (it needs Wi-Fi), pair once, " +
                                  "or enter the port shown under \"IP address & Port\".")
        }
        return try {
            val pkgs = adb.shell("pm list packages -3").lineSequence()
                .filter { it.startsWith("package:") }
                .map { it.removePrefix("package:").trim() }
                .filter { PKG.matches(it) }
                .toList()
            val ops = JSONObject()
            for (p in pkgs) ops.put(p, adb.shell("appops get $p"))
            Result(ops = ops, count = pkgs.size)
        } catch (e: Exception) {
            Result(error = "The scan stopped partway: ${e.message}")
        } finally {
            try { adb.disconnect() } catch (e: Exception) {}
        }
    }
}
