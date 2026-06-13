package com.jarvis.mobilebridge

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat

/**
 * Tiny config + status UI. First-launch flow:
 *   1) User types in token-endpoint URL + bridge-token + phone label.
 *   2) Grants SMS / Contacts / Phone / Notifications permissions.
 *   3) Taps Connect — BridgeService starts; persistent notification appears.
 *
 * No fancy theming; this isn't a consumer app, it's a sideloaded utility.
 */
class MainActivity : AppCompatActivity() {

    private val requestPerms = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { results ->
        val denied = results.filterValues { !it }.keys
        if (denied.isNotEmpty()) {
            Toast.makeText(this, "Denied: ${denied.joinToString()}", Toast.LENGTH_LONG).show()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        val endpoint = findViewById<EditText>(R.id.token_endpoint)
        val token = findViewById<EditText>(R.id.bridge_token)
        val machine = findViewById<EditText>(R.id.machine_name)
        val room = findViewById<EditText>(R.id.control_room)
        val status = findViewById<TextView>(R.id.status)

        endpoint.setText(Config.tokenEndpoint(this))
        token.setText(Config.bridgeToken(this))
        machine.setText(Config.machineName(this))
        room.setText(Config.controlRoom(this))

        findViewById<Button>(R.id.btn_save).setOnClickListener {
            Config.set(this, "token_endpoint", endpoint.text.toString().trim())
            Config.set(this, "bridge_token", token.text.toString().trim())
            Config.set(this, "machine_name", machine.text.toString().trim().lowercase())
            Config.set(this, "control_room", room.text.toString().trim())
            Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show()
        }

        findViewById<Button>(R.id.btn_perms).setOnClickListener {
            requestNeededPermissions()
        }

        findViewById<Button>(R.id.btn_connect).setOnClickListener {
            BridgeService.start(this)
            status.text = "Service starting — see the notification for live status."
        }

        findViewById<Button>(R.id.btn_disconnect).setOnClickListener {
            BridgeService.stop(this)
            status.text = "Disconnected."
        }

        findViewById<Button>(R.id.btn_jarvis).setOnClickListener {
            // Save first so the UI URL reflects the latest token endpoint, then open the HUD.
            Config.set(this, "token_endpoint", endpoint.text.toString().trim())
            val ui = Config.uiUrl(this)
            if (ui.isBlank()) {
                Toast.makeText(this, "Enter the token endpoint URL first, sir.", Toast.LENGTH_LONG).show()
            } else {
                startActivity(Intent(this, JarvisActivity::class.java).putExtra("url", ui))
            }
        }
    }

    private fun requestNeededPermissions() {
        val perms = mutableListOf(
            Manifest.permission.READ_SMS,
            Manifest.permission.SEND_SMS,
            Manifest.permission.READ_CONTACTS,
            Manifest.permission.CALL_PHONE,
            Manifest.permission.READ_PHONE_STATE,
            Manifest.permission.READ_CALL_LOG,
            Manifest.permission.RECORD_AUDIO,
            Manifest.permission.CAMERA,
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION,
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            perms += Manifest.permission.POST_NOTIFICATIONS
        }
        val missing = perms.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }.toTypedArray()
        if (missing.isNotEmpty()) {
            requestPerms.launch(missing)
        }
        // SYSTEM_ALERT_WINDOW is a SPECIAL permission (granted on a Settings screen, not via a
        // runtime dialog) and it's the one that lets the BACKGROUND bridge actually LAUNCH apps,
        // set alarms, open the calendar, etc. Without it those commands silently no-op. Send the
        // user to the overlay-permission screen if it isn't granted yet.
        if (!Settings.canDrawOverlays(this)) {
            Toast.makeText(
                this,
                "IMPORTANT: turn ON 'Display over other apps' so Jarvis can open apps & set alarms.",
                Toast.LENGTH_LONG,
            ).show()
            try {
                startActivity(
                    Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName"))
                )
            } catch (_: Exception) {
                startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION))
            }
        } else if (missing.isEmpty()) {
            Toast.makeText(this, "All permissions granted ✓", Toast.LENGTH_SHORT).show()
        }
    }
}
