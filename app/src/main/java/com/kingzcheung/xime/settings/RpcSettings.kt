package com.kingzcheung.xime.settings

import android.util.Log
import com.chaquo.python.Python
import java.io.File
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerialName
import kotlinx.serialization.json.Json

@Serializable
data class RpcSettings(
    @SerialName("http_enabled")
    val httpEnabled: Boolean = false,
    @SerialName("http_port")
    val httpPort: Int = 1144,
    @SerialName("http_host")
    val httpHost: String = "0.0.0.0",
    @SerialName("http_key")
    val httpKey: String = "",
    @SerialName("mqtt_enabled")
    val mqttEnabled: Boolean = false,
    @SerialName("request_topic")
    val mqttRequestTopic: String = "sys/device/request",
    @SerialName("mqtt_reply_topic")
    val mqttResponseTopic: String = "sys/device/response",
    @SerialName("pub_key")
    val mqttPubKey: String? = ""
)

object RpcSettingsStore {
    private const val TAG = "RpcSettingsStore"
    private val json = Json {
        prettyPrint = true
        ignoreUnknownKeys = true
        encodeDefaults = true
    }

    fun configFile(filesDir: File): File {
        check(Python.isStarted()) { "Python RPC is not started" }
        val path = Python.getInstance()
            .getModule("app")
            .callAttr("get_rpc_config_path", filesDir.absolutePath)
            .toString()
        return File(path)
    }

    fun load(filesDir: File): RpcSettings {
        return try {
            val settings = Python.getInstance()
                .getModule("app")
                .callAttr("get_rpc_settings", filesDir.absolutePath)
                .toString()
            json.decodeFromString<RpcSettings>(settings)
        } catch (error: Exception) {
            Log.w(TAG, "Unable to load RPC settings", error)
            RpcSettings()
        }
    }

    fun save(settings: RpcSettings, filesDir: File, configPath: String): Boolean {
        return try {
            Python.getInstance()
                .getModule("app")
                .callAttr(
                    "save_rpc_settings",
                    json.encodeToString(settings),
                    filesDir.absolutePath,
                    configPath
                )
                .toString()
                .toBoolean()
        } catch (error: Exception) {
            Log.e(TAG, "Unable to save RPC settings", error)
            false
        }
    }
}
