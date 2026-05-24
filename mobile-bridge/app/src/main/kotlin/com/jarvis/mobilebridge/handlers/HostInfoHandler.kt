package com.jarvis.mobilebridge.handlers

import android.content.Context
import android.os.Build
import com.jarvis.mobilebridge.Config
import org.json.JSONObject

object HostInfoHandler {
    fun execute(ctx: Context, args: JSONObject): JSONObject = JSONObject()
        .put("machine", Config.machineName(ctx))
        .put("model", "${Build.MANUFACTURER} ${Build.MODEL}")
        .put("android_version", Build.VERSION.RELEASE)
        .put("sdk_int", Build.VERSION.SDK_INT)
}
