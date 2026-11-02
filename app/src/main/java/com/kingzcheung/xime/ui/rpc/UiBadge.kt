package com.kingzcheung.xime.ui.rpc

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.kingzcheung.xime.util.UiOverrides
import org.json.JSONObject

private data class UiBadgeSpec(
    val kind: String,
    val color: Color,
    val size: Dp,
    val offsetX: Dp,
    val offsetY: Dp,
    val text: String?,
    val textColor: Color,
    val borderColor: Color?,
    val borderWidth: Dp,
)

/**
 * 在任意 UI 槽位右上角叠加 RPC 下发的角标（小红点/数字/文字）。
 * 角标用 offset 绘制，不参与布局测量，不拦截点击。
 */
@Composable
fun UiBadgeHost(
    target: String,
    modifier: Modifier = Modifier,
    content: @Composable () -> Unit,
) {
    val raw = UiOverrides.get(target, "badge")
    val spec = remember(raw) { parseBadge(raw) }
    Box(modifier) {
        content()
        if (spec != null) {
            UiBadge(spec, Modifier.align(Alignment.TopEnd).offset(x = spec.offsetX, y = spec.offsetY))
        }
    }
}

@Composable
private fun UiBadge(spec: UiBadgeSpec, modifier: Modifier = Modifier) {
    val shape = CircleShape
    val borderModifier = if (spec.borderColor != null) {
        Modifier.border(spec.borderWidth, spec.borderColor, shape)
    } else {
        Modifier
    }
    if (spec.kind == "dot") {
        Box(
            modifier
                .size(spec.size)
                .clip(shape)
                .background(spec.color)
                .then(borderModifier)
        )
    } else {
        Box(
            modifier
                .defaultMinSize(minWidth = spec.size, minHeight = spec.size)
                .clip(shape)
                .background(spec.color)
                .then(borderModifier)
                .padding(horizontal = 3.dp),
            contentAlignment = Alignment.Center,
        ) {
            Text(
                text = spec.text.orEmpty(),
                color = spec.textColor,
                fontSize = 8.sp,
                fontWeight = FontWeight.Bold,
                maxLines = 1,
            )
        }
    }
}

private fun parseBadge(raw: String?): UiBadgeSpec? {
    if (raw.isNullOrBlank()) return null
    val json = runCatching { JSONObject(raw) }.getOrNull() ?: return null
    val kind = json.optString("kind", "dot")
    val color = parseColor(json.optString("color"), Color.Red)
    val defaultSize = if (kind == "dot") 6.0 else 14.0
    val size = json.optDouble("sizeDp", defaultSize).toFloat().coerceIn(1f, 24f).dp
    val offsets = json.optJSONArray("offsetDp")
    val offsetX = (offsets?.optDouble(0, 2.0) ?: json.optDouble("offsetXDp", 2.0)).toFloat().dp
    val offsetY = (offsets?.optDouble(1, -2.0) ?: json.optDouble("offsetYDp", -2.0)).toFloat().dp
    val text = when (kind) {
        "count" -> json.optInt("count", 0).toString()
        "text" -> json.optString("text", "")
        else -> null
    }
    val textColor = parseColor(json.optString("textColor"), Color.White)
    val borderColor = json.optString("borderColor")
        .takeIf { it.isNotBlank() }
        ?.let { parseColor(it, Color.White) }
    val borderWidth = json.optDouble("borderWidthDp", 0.0).toFloat().coerceIn(0f, 4f).dp
    return UiBadgeSpec(kind, color, size, offsetX, offsetY, text, textColor, borderColor, borderWidth)
}

private fun parseColor(value: String?, fallback: Color): Color {
    if (value.isNullOrBlank()) return fallback
    return runCatching { Color(android.graphics.Color.parseColor(value)) }.getOrDefault(fallback)
}
