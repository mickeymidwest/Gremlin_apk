package com.gremlin.app

import android.app.KeyguardManager
import android.app.admin.DevicePolicyManager
import android.content.Context
import android.content.pm.ApplicationInfo
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Build
import android.provider.Settings
import androidx.core.app.NotificationManagerCompat
import org.json.JSONArray
import org.json.JSONObject

/**
 * Phone guard (S3): a read-only security snapshot of this phone, sent to
 * Gremlin on the desktop (POST /guard/phone), which compares it with the
 * last snapshot Mickey marked as normal.
 *
 * Works on stock Android with a locked bootloader: no root. Everything here
 * is readable by an ordinary app (package visibility via QUERY_ALL_PACKAGES).
 * For special access (draw over apps, install unknown apps, all-files
 * access) an app can only see that another app ASKS for it, not whether it
 * was granted, so those go in "special", not "perms".
 */
object PhoneGuard {

    // runtime permissions: sent only when GRANTED
    private val RUNTIME = mapOf(
        "android.permission.READ_SMS" to "READ_SMS",
        "android.permission.RECEIVE_SMS" to "RECEIVE_SMS",
        "android.permission.SEND_SMS" to "SEND_SMS",
        "android.permission.READ_CALL_LOG" to "READ_CALL_LOG",
        "android.permission.RECORD_AUDIO" to "RECORD_AUDIO",
        "android.permission.ACCESS_BACKGROUND_LOCATION" to "ACCESS_BACKGROUND_LOCATION",
        "android.permission.READ_CONTACTS" to "READ_CONTACTS",
        "android.permission.CAMERA" to "CAMERA",
    )

    // special access: sent when REQUESTED (grant isn't visible to us)
    private val SPECIAL = mapOf(
        "android.permission.SYSTEM_ALERT_WINDOW" to "SYSTEM_ALERT_WINDOW",
        "android.permission.REQUEST_INSTALL_PACKAGES" to "REQUEST_INSTALL_PACKAGES",
        "android.permission.MANAGE_EXTERNAL_STORAGE" to "MANAGE_EXTERNAL_STORAGE",
    )

    fun snapshot(ctx: Context): JSONObject {
        val cr = ctx.contentResolver
        val snap = JSONObject()
            .put("taken", System.currentTimeMillis())
            .put("device", Build.MODEL)
            .put("android", Build.VERSION.RELEASE)
            .put("sdk", Build.VERSION.SDK_INT)
            .put("security_patch", Build.VERSION.SECURITY_PATCH)
            .put("screen_lock", (ctx.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager).isDeviceSecure)
            .put("dev_options", Settings.Global.getInt(cr, Settings.Global.DEVELOPMENT_SETTINGS_ENABLED, 0) == 1)
            .put("adb", Settings.Global.getInt(cr, Settings.Global.ADB_ENABLED, 0) == 1)
            .put("vpn_active", vpnActive(ctx))

        val a11y = Settings.Secure.getString(cr, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: ""
        snap.put("accessibility", JSONArray(a11y.split(':').filter { it.isNotBlank() }))

        val dpm = ctx.getSystemService(Context.DEVICE_POLICY_SERVICE) as DevicePolicyManager
        snap.put("device_admins", JSONArray((dpm.activeAdmins ?: emptyList()).map { it.packageName }.distinct()))
        snap.put("notification_listeners", JSONArray(NotificationManagerCompat.getEnabledListenerPackages(ctx).toList()))
        snap.put("packages", packages(ctx))
        return snap
    }

    private fun vpnActive(ctx: Context): Boolean = try {
        val cm = ctx.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        cm.getNetworkCapabilities(cm.activeNetwork)?.hasTransport(NetworkCapabilities.TRANSPORT_VPN) == true
    } catch (e: Exception) { false }

    private fun packages(ctx: Context): JSONArray {
        val pm = ctx.packageManager
        val flags = PackageManager.GET_PERMISSIONS or PackageManager.GET_SERVICES
        val list: List<PackageInfo> = if (Build.VERSION.SDK_INT >= 33) {
            pm.getInstalledPackages(PackageManager.PackageInfoFlags.of(flags.toLong()))
        } else {
            @Suppress("DEPRECATION") pm.getInstalledPackages(flags)
        }
        val out = JSONArray()
        for (p in list) {
            val ai = p.applicationInfo ?: continue
            val system = (ai.flags and ApplicationInfo.FLAG_SYSTEM) != 0
            val installer: String? = try {
                if (Build.VERSION.SDK_INT >= 30) pm.getInstallSourceInfo(p.packageName).installingPackageName
                else @Suppress("DEPRECATION") pm.getInstallerPackageName(p.packageName)
            } catch (e: Exception) { null }
            val perms = JSONArray()
            val special = JSONArray()
            val req = p.requestedPermissions
            val reqFlags = p.requestedPermissionsFlags
            if (req != null && reqFlags != null) {
                for (i in req.indices) {
                    val name = req[i] ?: continue
                    RUNTIME[name]?.let { short ->
                        if ((reqFlags[i] and PackageInfo.REQUESTED_PERMISSION_GRANTED) != 0) perms.put(short)
                    }
                    SPECIAL[name]?.let { special.put(it) }
                }
            }
            if (p.services?.any { it.permission == "android.permission.BIND_ACCESSIBILITY_SERVICE" } == true) {
                perms.put("BIND_ACCESSIBILITY_SERVICE")
            }
            out.put(JSONObject()
                .put("pkg", p.packageName)
                .put("label", ai.loadLabel(pm).toString())
                .put("installer", installer ?: JSONObject.NULL)
                .put("system", system)
                .put("first_install", p.firstInstallTime)
                .put("last_update", p.lastUpdateTime)
                .put("perms", perms)
                .put("special", special))
        }
        return out
    }
}
