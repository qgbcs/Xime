# Copilot 指令（入口）

仓库规则统一维护在根目录 `AGENTS.md`，请先完整阅读。

关键判定：`app/src/main/jni/librime`、`app/src/main/jni/librime-lua`、`app/src/main/jni/librime-lua-deps`
是第三方构建子模块，其中未跟踪/已修改内容属于构建残留，**不要提交、不要清理、不要当成待办**；
判断工作区是否干净用 `git status --short --ignore-submodules=all`。
