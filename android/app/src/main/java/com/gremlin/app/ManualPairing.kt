package com.gremlin.app

import java.net.HttpURLConnection
import java.net.URI
import java.net.URL

/** Pairing without the QR code: for addresses the QR doesn't advertise,
 *  like a Tailscale IP. Saves the same host/port/token the QR flow does. */
object ManualPairing {
    const val DEFAULT_PORT = 8765

    data class Target(val host: String, val port: Int, val token: String)

    /** [address] is an IP, a host:port, or a whole pairing link. An explicit
     *  port or token field wins over what's in the link. Throws
     *  IllegalArgumentException with a message fit to show the user. */
    fun parse(address: String, portField: String, tokenField: String): Target {
        val text = address.trim()
        var host: String
        var linkPort = -1
        var linkToken: String? = null

        if ("://" in text) {
            val uri = try {
                URI(text)
            } catch (e: Exception) {
                throw IllegalArgumentException("That pairing link isn't valid")
            }
            host = uri.host ?: ""
            linkPort = uri.port
            linkToken = uri.query
                ?.split("&")
                ?.map { it.split("=", limit = 2) }
                ?.firstOrNull { it.getOrNull(0) == "token" }
                ?.getOrNull(1)
        } else {
            val parts = text.split(":")
            host = parts[0]
            if (parts.size > 2) throw IllegalArgumentException("Use an IP like 100.122.58.85")
            if (parts.size == 2) {
                linkPort = parts[1].toIntOrNull()
                    ?: throw IllegalArgumentException("That port isn't a number")
            }
        }

        if (!host.matches(Regex("[A-Za-z0-9.-]+"))) {
            throw IllegalArgumentException("Enter the desktop's IP address")
        }

        val port = when {
            portField.isNotBlank() -> portField.trim().toIntOrNull()
                ?: throw IllegalArgumentException("That port isn't a number")
            linkPort != -1 -> linkPort
            else -> DEFAULT_PORT
        }
        if (port !in 1..65535) throw IllegalArgumentException("Port must be 1-65535")

        val token = tokenField.trim().ifEmpty { linkToken ?: "" }
        if (token.isEmpty()) throw IllegalArgumentException("Enter the token")

        return Target(host, port, token)
    }

    /** Returns null if the desktop accepted [target], else what went wrong. */
    fun check(target: Target): String? {
        val where = "${target.host}:${target.port}"
        return try {
            val connection = URL("http://$where/status").openConnection() as HttpURLConnection
            connection.setRequestProperty("Authorization", "Bearer ${target.token}")
            connection.connectTimeout = 8_000
            connection.readTimeout = 8_000
            try {
                when (val code = connection.responseCode) {
                    in 200..299 -> null
                    401, 403 -> "Reached $where, but the token was rejected"
                    else -> "$where answered with HTTP $code"
                }
            } finally {
                connection.disconnect()
            }
        } catch (e: Exception) {
            "Couldn't reach $where. Is Tailscale on and the desktop awake?"
        }
    }
}
