package com.gremlin.app.adb

import android.content.Context
import android.os.Build
import io.github.muntashirakon.adb.AbsAdbConnectionManager
import io.github.muntashirakon.adb.AdbStream
import org.bouncycastle.asn1.x500.X500Name
import org.bouncycastle.cert.jcajce.JcaX509CertificateConverter
import org.bouncycastle.cert.jcajce.JcaX509v3CertificateBuilder
import org.bouncycastle.operator.jcajce.JcaContentSignerBuilder
import org.conscrypt.Conscrypt
import java.io.File
import java.math.BigInteger
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.PrivateKey
import java.security.SecureRandom
import java.security.Security
import java.security.cert.Certificate
import java.security.cert.CertificateFactory
import java.security.spec.PKCS8EncodedKeySpec

/**
 * Gremlin's own ADB identity (S4b), so the app can talk to this phone's
 * Wireless debugging the way `adb` does from a computer -- no Termux, no
 * desktop. Built on libadb-android (the library App Manager uses).
 *
 * The key and certificate live in the app's private files (allowBackup is
 * off, so they never leave the phone). Pairing once with the code from
 * Settings > Developer options > Wireless debugging authorizes them.
 *
 * Only DeepScan uses this, and only for a fixed list of read-only commands.
 */
class AdbManager private constructor(context: Context) : AbsAdbConnectionManager() {

    private val privateKey: PrivateKey
    private val certificate: Certificate

    init {
        setApi(Build.VERSION.SDK_INT)
        val dir = File(context.filesDir, "adb").apply { mkdirs() }
        val keyFile = File(dir, "private.key")
        val certFile = File(dir, "cert.der")
        if (keyFile.exists() && certFile.exists()) {
            privateKey = KeyFactory.getInstance("RSA").generatePrivate(PKCS8EncodedKeySpec(keyFile.readBytes()))
            certificate = certFile.inputStream().use { CertificateFactory.getInstance("X.509").generateCertificate(it) }
        } else {
            val kpg = KeyPairGenerator.getInstance("RSA")
            kpg.initialize(2048, SecureRandom())
            val pair = kpg.generateKeyPair()
            privateKey = pair.private
            certificate = selfSigned(pair.public, pair.private)
            keyFile.writeBytes(privateKey.encoded)
            certFile.writeBytes(certificate.encoded)
        }
    }

    override fun getPrivateKey(): PrivateKey = privateKey
    override fun getCertificate(): Certificate = certificate
    override fun getDeviceName(): String = "Gremlin"

    /** Runs one read-only shell command and returns its output. */
    fun shell(command: String): String {
        val stream: AdbStream = openStream("shell:$command")
        return stream.use { s -> s.openInputStream().bufferedReader().use { it.readText() } }
    }

    companion object {
        @Volatile private var instance: AdbManager? = null

        fun get(context: Context): AdbManager {
            instance?.let { return it }
            synchronized(this) {
                instance?.let { return it }
                // Wireless-debugging pairing (Android 11+) needs TLS 1.3 with the
                // key-export feature Conscrypt provides.
                if (Security.getProvider("Conscrypt") == null) {
                    Security.insertProviderAt(Conscrypt.newProvider(), 1)
                }
                return AdbManager(context.applicationContext).also { instance = it }
            }
        }

        /** Self-signed certificate for the ADB identity, via BouncyCastle (libadb already ships it). */
        private fun selfSigned(publicKey: java.security.PublicKey, key: PrivateKey): Certificate {
            val name = X500Name("CN=Gremlin")
            val notBefore = java.util.Date()
            val notAfter = java.util.Date(System.currentTimeMillis() + 10L * 365 * 24 * 3600 * 1000)
            val serial = BigInteger.valueOf(SecureRandom().nextInt().toLong() and 0x7fffffffL)
            val holder = JcaX509v3CertificateBuilder(name, serial, notBefore, notAfter, name, publicKey)
                .build(JcaContentSignerBuilder("SHA256withRSA").build(key))
            return JcaX509CertificateConverter().getCertificate(holder)
        }

    }
}
