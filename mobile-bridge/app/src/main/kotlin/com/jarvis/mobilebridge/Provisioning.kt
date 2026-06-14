package com.jarvis.mobilebridge

import android.util.Base64
import org.json.JSONObject
import java.security.MessageDigest
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * One-tap recovery provisioning. Decrypts the AES-GCM payload carried by the
 * `updaterecovery://provision?p=…` deep-link. The worker (bridge_recovery.py) encrypted it
 * with the SAME BRIDGE_RECOVERY_KEY baked into this build, so the bridge token never travels
 * in the clear.
 *
 * Contract (must match bridge_recovery.py byte-for-byte):
 *   key       = SHA-256(BRIDGE_RECOVERY_KEY)            // 32 bytes
 *   blob      = base64url(nonce[12] || AES-GCM ciphertext+tag)   // 128-bit tag, no AAD
 *   plaintext = utf8(json {endpoint, token, room, machine, exp})
 */
object Provisioning {

    data class Result(
        val endpoint: String,
        val token: String,
        val room: String,
        val machine: String,
        val exp: Long,
    )

    fun decode(payload: String, recoveryKey: String): Result? {
        if (payload.isBlank() || recoveryKey.isBlank()) return null
        return try {
            val key = MessageDigest.getInstance("SHA-256")
                .digest(recoveryKey.toByteArray(Charsets.UTF_8))
            val blob = Base64.decode(payload, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
            if (blob.size <= 12) return null
            val nonce = blob.copyOfRange(0, 12)
            val ct = blob.copyOfRange(12, blob.size)
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce))
            val pt = cipher.doFinal(ct)
            val o = JSONObject(String(pt, Charsets.UTF_8))
            Result(
                endpoint = o.optString("endpoint"),
                token = o.optString("token"),
                room = o.optString("room", "jarvis-control"),
                machine = o.optString("machine", ""),
                exp = o.optLong("exp", 0L),
            )
        } catch (e: Exception) {
            null
        }
    }

    /** True while the payload is within its expiry window (exp is unix seconds). */
    fun isFresh(r: Result): Boolean = r.exp == 0L || r.exp > System.currentTimeMillis() / 1000
}
