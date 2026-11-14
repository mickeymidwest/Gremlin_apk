package com.gremlin.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat

/**
 * Keeps CPU and Wi-Fi awake via a foreground service + partial wake lock while
 * Gremlin is thinking or streaming a response, so the screen can turn off and
 * lock without Android suspending the network socket.
 */
class ThinkingKeepAliveService : Service() {

    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        ensureChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            releaseLocks()
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            return START_NOT_STICKY
        }
        val notif = buildNotification()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIF_ID, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(NOTIF_ID, notif)
        }
        acquireLocks()
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        releaseLocks()
        super.onDestroy()
    }

    private fun acquireLocks() {
        if (wakeLock?.isHeld != true) {
            val pm = getSystemService(Context.POWER_SERVICE) as? PowerManager
            wakeLock = pm?.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "gremlin:ThinkingWakeLock")?.apply {
                setReferenceCounted(false)
                acquire(15 * 60 * 1000L) // 15-min safety cap
            }
        }
        if (wifiLock?.isHeld != true) {
            val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
            @Suppress("DEPRECATION")
            val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                WifiManager.WIFI_MODE_FULL_LOW_LATENCY
            } else {
                WifiManager.WIFI_MODE_FULL_HIGH_PERF
            }
            wifiLock = wm?.createWifiLock(mode, "gremlin:ThinkingWifiLock")?.apply {
                setReferenceCounted(false)
                acquire()
            }
        }
    }

    private fun releaseLocks() {
        try {
            if (wakeLock?.isHeld == true) wakeLock?.release()
        } catch (_: Exception) {}
        wakeLock = null
        try {
            if (wifiLock?.isHeld == true) wifiLock?.release()
        } catch (_: Exception) {}
        wifiLock = null
    }

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = getSystemService(NotificationManager::class.java) ?: return
            if (nm.getNotificationChannel(CHANNEL_ID) == null) {
                val ch = NotificationChannel(
                    CHANNEL_ID,
                    "Gremlin background responses",
                    NotificationManager.IMPORTANCE_LOW,
                ).apply {
                    description = "Keeps Gremlin connected while your screen is off."
                    setShowBadge(false)
                }
                nm.createNotificationChannel(ch)
            }
        }
    }

    private fun buildNotification(): Notification =
        NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle("Gremlin is working")
            .setContentText("Receiving response in the background")
            .setOngoing(true)
            .setSilent(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

    companion object {
        private const val CHANNEL_ID = "gremlin_thinking"
        private const val NOTIF_ID = 4102
        private const val ACTION_STOP = "com.gremlin.app.ACTION_STOP_KEEPALIVE"

        fun start(context: Context) {
            try {
                val intent = Intent(context, ThinkingKeepAliveService::class.java)
                ContextCompat.startForegroundService(context, intent)
            } catch (_: Exception) {}
        }

        fun stop(context: Context) {
            try {
                context.stopService(Intent(context, ThinkingKeepAliveService::class.java))
            } catch (_: Exception) {}
        }
    }
}
