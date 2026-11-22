# 项目规则（入口）

完整规则见仓库根 `AGENTS.md`，优先阅读它。

- `app/src/main/jni/librime`、`app/src/main/jni/librime-lua`、`app/src/main/jni/librime-lua-deps`
  的未提交内容属构建残留：**不要提交、不要清理、不要当成待办**。
- 判断工作区是否干净用 `git status --short --ignore-submodules=all`；
  仓库自身文件（发布脚本、`app/src/main/python/` 等）的改动仍须正常 add/commit。
