package com.kingzcheung.xime.ui.feedback

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.view.View
import android.widget.TextView
import com.chaquo.python.Python
import com.kingzcheung.xime.util.UiOverrides

/**
 * 独立反馈页宿主：界面全部由 feature_feedback.py 用原生 View 搭建。
 * windowSoftInputMode=adjustResize 保证键盘弹出时聊天区收缩、输入栏不被遮挡。
 */
class FeedbackActivity : Activity() {
    companion object {
        const val REQUEST_PICK_IMAGE = 1001

        fun start(context: Context) {
            context.startActivity(
                Intent(context, FeedbackActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // 打开反馈页即视为已读：清掉候选栏与菜单项共享的红点
        UiOverrides.clearBadgeChain()
        val content = try {
            Python.getInstance()
                .getModule("feature_feedback")
                .callAttr("build_view", this)
                .toJava(View::class.java)
        } catch (error: Throwable) {
            TextView(this).apply {
                text = "反馈页面加载失败：${error.message ?: error.javaClass.simpleName}"
                textSize = 16f
                setPadding(64, 120, 64, 64)
            }
        }
        setContentView(content)
    }

    override fun onDestroy() {
        try {
            Python.getInstance()
                .getModule("feature_feedback")
                .callAttr("release_page", this)
        } catch (error: Throwable) {
            // Python 侧页面已随 Activity 销毁，解绑失败不影响退出
        }
        super.onDestroy()
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode == REQUEST_PICK_IMAGE) {
            try {
                Python.getInstance()
                    .getModule("feature_feedback")
                    .callAttr("on_activity_result", this, requestCode, resultCode, data)
            } catch (error: Throwable) {
                // Python 侧已记录日志，这里不再二次崩溃
            }
        }
        super.onActivityResult(requestCode, resultCode, data)
    }
}
