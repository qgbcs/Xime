#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
WORKSPACE_DIR="$(dirname "$PROJECT_DIR")"

if (($#)); then
    echo "错误: 本脚本不使用命令行参数，请修改文件开头的“构建参数”配置区。" >&2
    exit 2
fi

# Python 运行时版本在解析 BUILD_ABIS 后确定（arm32 需要 3.11）

# ============================================================
# 构建参数：需要调整时只修改本段。
# APP_ICON_PATH 默认使用项目原始矢量图标；可改为 PNG、WebP 或矢量 XML 文件。
# ============================================================
# 注：BUILD_ABIS 可用环境变量覆盖（如 BUILD_ABIS=armeabi-v7a ./debug_build_secexp.sh 出 arm32 包），默认 arm64-v8a。
unset APPLICATION_ID VERSION_CODE VERSION_NAME APP_NAME
VERSION_CODE="${VERSION_CODE:-20261128}"

APP_CHINESE_NAME="${APP_CHINESE_NAME:-随手}"
APP_NAME="${APP_CHINESE_NAME}输入法"
APP_ICON_PATH="$PROJECT_DIR/app/icons/iFly_xunfei/1760.png"

#APP_NAME="${VERSION_CODE: -4}输入法"
#APP_ICON_PATH="${APP_ICON_PATH:-$PROJECT_DIR/app/src/main/res/drawable/ic_launcher_foreground.xml}"

PROJECT_NAME="${PROJECT_DIR##*/}"

SECEXP="${SECEXP:-1}"
APPLICATION_ID="${APPLICATION_ID:-com.qgb.${PROJECT_NAME}}"
VERSION_NAME="${VERSION_NAME:-${VERSION_CODE}有问题请联系作者  }"

APK_BASENAME="${APPLICATION_ID}"
BUILD_ABIS="${BUILD_ABIS:-arm64-v8a}"

# Chaquopy 限制：Python 3.12+ 只支持 64 位，armeabi-v7a 只能用 3.11。
if [[ "$BUILD_ABIS" == *"armeabi-v7a"* ]]; then
    PYTHON_VERSION="${PYTHON_VERSION:-3.11}"
else
    PYTHON_VERSION="${PYTHON_VERSION:-3.13}"
fi
if [[ ! -x "/usr/bin/python${PYTHON_VERSION}" ]]; then
    if [[ "$PYTHON_VERSION" == "3.13" ]]; then
        "$PROJECT_DIR/scripts/install-python313.sh"
    else
        echo "错误: 缺少 /usr/bin/python${PYTHON_VERSION}（arm32 需要 Python 3.11）" >&2
        exit 1
    fi
fi
OUT_DIR="${OUT_DIR:-$PROJECT_DIR/out}"

GRADLE_USER_HOME="$WORKSPACE_DIR/.gradle"
ANDROID_USER_HOME="$WORKSPACE_DIR/.android"
# ============================================================

if [[ ! -f "$APP_ICON_PATH" || ! -r "$APP_ICON_PATH" || ! -s "$APP_ICON_PATH" ]]; then
    echo "错误: 图标文件不存在、不可读或为空：$APP_ICON_PATH" >&2
    exit 1
fi

APP_ICON_PATH="$(realpath -- "$APP_ICON_PATH")"
printf '%s 应用图标文件：%s\n' $VERSION_CODE "$APP_ICON_PATH"

# 启动图标：直接落到 debug 专属资源目录，保证一定参与资源合并。
# 历史问题：app/build.gradle.kts 的 generateAppIconResources 会生成
#   build/generated/appIcon/res/drawable-nodpi/ic_launcher_foreground.png，
# 但该目录曾未进入 mergeDebugResources，最终 APK 里仍是默认矢量图标。
# 这里由脚本自己写入 app/src/debug/res/drawable-nodpi/，debug 包必定使用该图标；
# 该文件是构建产物，已在 .gitignore 中忽略。
ICON_RES_DIR="$PROJECT_DIR/app/src/debug/res/drawable-nodpi"
mkdir -p "$ICON_RES_DIR"
rm -f "$ICON_RES_DIR"/ic_launcher_foreground.*
python3 - "$APP_ICON_PATH" "$ICON_RES_DIR/ic_launcher_foreground.png" <<'PYICON'
import sys
src, dst = sys.argv[1], sys.argv[2]
try:
    from PIL import Image
    im = Image.open(src).convert("RGBA")
    scale = 432.0 / max(im.size)
    if scale < 1:
        size = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
        im = im.resize(size, Image.LANCZOS)
    im.save(dst, "PNG")
except Exception:
    import shutil
    shutil.copyfile(src, dst)
PYICON
printf '%s 已注入 debug 启动图标：%s\n' $VERSION_CODE "$ICON_RES_DIR/ic_launcher_foreground.png"

# ============================================================
# 新增环境隔离：强制让 Gradle 和 Android 工具依赖外层工作区目录
# 显式 mkdir -p 预先创建目录，防止 AGP 的 AndroidDirectoryCreator 崩溃。
# ============================================================
export GRADLE_USER_HOME ANDROID_USER_HOME

mkdir -p "$GRADLE_USER_HOME" "$ANDROID_USER_HOME"
# ============================================================

# 任何一次变更都必须显式传入；禁止把 SECEXP 或构建环境写进版本名/包名。
export APP_NAME APP_CHINESE_NAME APK_BASENAME APPLICATION_ID VERSION_CODE VERSION_NAME BUILD_ABIS SECEXP

mkdir -p "$OUT_DIR"

# 1)定位 Android SDK（兼容 build.sh 的默认路径）
ANDROID_HOME_DEFAULT=""
for candidate in \
    "${ANDROID_HOME:-}" \
    "${ANDROID_SDK_ROOT:-}" \
    "$PROJECT_DIR/../.cache/briefcase/tools/android_sdk" \
    "$PROJECT_DIR/../.buildozer/android/platform/android-sdk" \
    "$PROJECT_DIR/../sdk"; do
    if [[ -n "$candidate" && -f "$candidate/platforms/android-36/android.jar" ]]; then
        ANDROID_HOME_DEFAULT="$(cd "$candidate" && pwd)"
        break
    fi
done

if [[ -z "$ANDROID_HOME_DEFAULT" ]]; then
    platform_jar="$(find / -type f -path '*/platforms/android-36/android.jar' -print -quit 2>/dev/null || true)"
    if [[ -n "$platform_jar" ]]; then
        ANDROID_HOME_DEFAULT="$(cd "$(dirname "$(dirname "$(dirname "$platform_jar")")")" && pwd)"
    fi
fi

if [[ -z "$ANDROID_HOME_DEFAULT" ]]; then
    echo "错误: 未找到 Android SDK，请先设置 ANDROID_HOME 或确保存在 android-36 平台。" >&2
    exit 1
fi
export ANDROID_HOME="$ANDROID_HOME_DEFAULT"
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$ANDROID_HOME_DEFAULT}"

# ============================================================
# 可重复构建：工具链指纹检测（只告警，永不阻断构建）
# 首次运行写入 repro_env_baseline.json；以后不一致只提醒。
# ============================================================
check_toolchain_fingerprint() {
    local baseline_file="$PROJECT_DIR/repro_env_baseline.json"
    local jdk_version agp_version kotlin_version gradle_version
    local build_tools platform ndk cmake d8_sha py_path py_version py_sha

    jdk_version="$(java -version 2>&1 | head -n 1 | tr -d '\r')"
    agp_version="$(sed -n 's/^agp[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' gradle/libs.versions.toml 2>/dev/null | head -n 1)"
    kotlin_version="$(sed -n 's/^kotlin[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' gradle/libs.versions.toml 2>/dev/null | head -n 1)"
    gradle_version="$(sed -n 's#^distributionUrl=.*gradle-\([0-9.]*\)-bin\.zip#\1#p' gradle/wrapper/gradle-wrapper.properties 2>/dev/null | head -n 1)"
    build_tools="$(find "$ANDROID_HOME/build-tools" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort -V | tail -n 1)"
    platform="$(find "$ANDROID_HOME/platforms" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort -V | tail -n 1)"
    ndk="$(find "$ANDROID_HOME/ndk" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort -V | tail -n 1)"
    cmake="$(find "$ANDROID_HOME/cmake" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort -V | tail -n 1)"
    d8_sha=""
    if [[ -n "$build_tools" && -f "$ANDROID_HOME/build-tools/$build_tools/lib/d8.jar" ]]; then
        d8_sha="$(sha256sum "$ANDROID_HOME/build-tools/$build_tools/lib/d8.jar" | cut -c1-16)"
    fi
    if [[ -x "/usr/bin/python$PYTHON_VERSION" ]]; then
        py_path="$(readlink -f "/usr/bin/python$PYTHON_VERSION" 2>/dev/null || printf '%s' "/usr/bin/python$PYTHON_VERSION")"
        py_version="$(/usr/bin/python$PYTHON_VERSION -V 2>&1 | tr -d '\r')"
        py_sha="$(sha256sum "$py_path" 2>/dev/null | cut -c1-16)"
    else
        py_path="/usr/bin/python$PYTHON_VERSION"
        py_version="missing"
        py_sha=""
    fi

    REPRO_PROJECT_DIR="$PROJECT_DIR" \
    REPRO_JDK="$jdk_version" \
    REPRO_AGP="$agp_version" \
    REPRO_KOTLIN="$kotlin_version" \
    REPRO_GRADLE="$gradle_version" \
    REPRO_BUILD_TOOLS="$build_tools" \
    REPRO_PLATFORM="$platform" \
    REPRO_NDK="$ndk" \
    REPRO_CMAKE="$cmake" \
    REPRO_D8_SHA256_16="$d8_sha" \
    REPRO_PYTHON_PATH="$py_path" \
    REPRO_PYTHON_VERSION="$py_version" \
    REPRO_PYTHON_SHA256_16="$py_sha" \
    python3 - "$baseline_file" <<'PYREPRO'
import json, os, sys

baseline_path = sys.argv[1]
current = {
    "project_dir": os.environ["REPRO_PROJECT_DIR"],
    "jdk": os.environ["REPRO_JDK"],
    "agp": os.environ["REPRO_AGP"],
    "kotlin": os.environ["REPRO_KOTLIN"],
    "gradle": os.environ["REPRO_GRADLE"],
    "build_tools": os.environ["REPRO_BUILD_TOOLS"],
    "platform": os.environ["REPRO_PLATFORM"],
    "ndk": os.environ["REPRO_NDK"],
    "cmake": os.environ["REPRO_CMAKE"],
    "d8_sha256_16": os.environ["REPRO_D8_SHA256_16"],
    "python_path": os.environ["REPRO_PYTHON_PATH"],
    "python_version": os.environ["REPRO_PYTHON_VERSION"],
    "python_sha256_16": os.environ["REPRO_PYTHON_SHA256_16"],
}

if not os.path.exists(baseline_path):
    with open(baseline_path, "w", encoding="utf-8") as handle:
        json.dump(current, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    print(f"[可重复构建] 已写入工具链基线: {baseline_path}")
else:
    with open(baseline_path, encoding="utf-8") as handle:
        baseline = json.load(handle)
    diffs = [(key, baseline.get(key), value) for key, value in current.items()
             if baseline.get(key) != value]
    if not diffs:
        print("[可重复构建] 工具链与基线一致")
    else:
        print("[可重复构建][警告] 工具链与基线不一致（仅告警，继续构建）:")
        for key, old, new in diffs:
            print(f"  - {key}: 基线={old!r} 当前={new!r}")
PYREPRO
}
check_toolchain_fingerprint || echo "警告: 工具链指纹检测失败，继续构建。" >&2


# ============================================================
# 子模块同步：只认 .gitmodules 里的 path/url，rsync 权威覆盖
# ============================================================
relpath=.gradle/wrapper/dists/gradle-9.5.0-bin/estug5qhtw5xqrldjyqnn9yr5
# 由于设置了 GRADLE_USER_HOME 为外层目录，在 WORKSPACE_DIR 确保对应的目录结构存在
mkdir -p "$WORKSPACE_DIR/$relpath"

SUBMODULE_SECTION="[submodule \"app/src/main/python/multi_mqtt\"]"
SUBMODULE_PATH_VALUE=""
SUBMODULE_URL_VALUE=""
current_section=""

if [[ -f "$PWD/.gitmodules" ]]; then
    while IFS= read -r line; do
        if [[ "$line" =~ ^\[submodule\ \".*\"\]$ ]]; then
            current_section="$line"
        fi
        if [[ "$current_section" == "$SUBMODULE_SECTION" ]]; then
            if [[ "$line" =~ ^[[:space:]]*path[[:space:]]*=[[:space:]]*(.*)$ ]]; then
                # 用捕获组取完整值；不能用 ${line##*=}，那是最长前缀匹配，
                # 会截断含 '=' 的值（如带查询参数的 URL token=abc 只剩 abc）。
                SUBMODULE_PATH_VALUE="${BASH_REMATCH[1]}"
                SUBMODULE_PATH_VALUE="${SUBMODULE_PATH_VALUE//[[:space:]]/}"
            elif [[ "$line" =~ ^[[:space:]]*url[[:space:]]*=[[:space:]]*(.*)$ ]]; then
                SUBMODULE_URL_VALUE="${BASH_REMATCH[1]}"
                SUBMODULE_URL_VALUE="${SUBMODULE_URL_VALUE//[[:space:]]/}"
            fi
        fi
    done < "$PWD/.gitmodules"
fi

if [[ -z "$SUBMODULE_PATH_VALUE" || -z "$SUBMODULE_URL_VALUE" ]]; then
    echo "错误: 无法从 .gitmodules 解析 app/src/main/python/multi_mqtt 的 path/url，构建中止。" >&2
    exit 1
fi

target_dir="$PWD/$SUBMODULE_PATH_VALUE"
source_dir="$SUBMODULE_URL_VALUE"
# 相对路径统一转成绝对路径（相对脚本所在目录）
if [[ "$source_dir" != /* ]]; then
    source_dir="$PWD/$source_dir"
fi
# 规范化路径（去掉 ../ 之类）
if [[ -d "$source_dir" ]]; then
    source_dir="$(cd "$source_dir" && pwd)"
fi

if [[ -d "$source_dir" ]]; then
    echo "同步目录: $source_dir -> $target_dir"
    mkdir -p "$target_dir"
    # 清掉目标里可能残留的 .git（旧的 submodule 痕迹），否则它还是个 git 仓库
    rm -rf "$target_dir/.git"
    rsync --delete-excluded \
          --include='/*.py' --exclude='*' \
          -a "$source_dir/" "$target_dir/" \
        || echo "警告: rsync 返回非零，忽略并继续构建。" >&2
    # 强制清理：同步后只保留根级 .py，删除子目录和其他文件
    find "$target_dir" -mindepth 1 -maxdepth 1 ! -name "*.py" -exec rm -rf -- {} +
else
    echo "警告: 同步源目录不存在: $source_dir，跳过 rsync。" >&2
fi

# ============================================================
# 可重复构建：统一 Python 源码 mtime、清理 __pycache__
# Chaquopy 打包 assets/chaquopy/app.imy 时会带上源文件时间戳，mtime 不稳
# 会让同一份源码产出不同的 app.imy。在 gradle 之前钉死时间。
# ============================================================
normalize_python_sources() {
    local stamp="2026-11-11 00:00:00 UTC"
    local roots=("$PROJECT_DIR/app/src/main/python")
    # site-packages 如果被解包到源码树，一并纳入（当前默认由 chaquopy 管理）
    [[ -d "$PROJECT_DIR/app/src/main/python/site-packages" ]] && \
        roots+=("$PROJECT_DIR/app/src/main/python/site-packages")
    local root
    for root in "${roots[@]}"; do
        [[ -d "$root" ]] || continue
        find "$root" -type d -name '__pycache__' -prune -exec rm -rf -- {} + 2>/dev/null || true
        find "$root" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete 2>/dev/null || true
        find "$root" -type f -name '*.py' -exec touch -d "$stamp" -- {} + 2>/dev/null || true
    done
    echo "已统一 Python 源码 mtime: $stamp，并清理 __pycache__"
}
normalize_python_sources

ensure_native_dependency() {
    local repository="$1"
    local destination="$2"
    local marker="$destination/CMakeLists.txt"
    if [[ -f "$marker" ]]; then
        return 0
    fi

    local temporary_directory
    temporary_directory="$(mktemp -d)"
    # 显式清理，不用 RETURN trap：本函数在 `|| true` 下调用，set -e 会被禁用，
    # trap 一旦在异常路径残留，之后每次函数返回都会误执行 rm -rf。
    echo "缺少 native 依赖，正在下载: $repository"
    if ! git clone --depth 1 --recurse-submodules "$repository" "$temporary_directory/source"; then
        echo "警告: 下载 native 依赖失败: $repository" >&2
        rm -rf "$temporary_directory"
        return 1
    fi
    mkdir -p "$destination"
    if ! cp -a "$temporary_directory/source/." "$destination/"; then
        echo "警告: 复制 native 依赖失败: $repository -> $destination" >&2
        rm -rf "$temporary_directory"
        return 1
    fi
    rm -rf "$temporary_directory"
    return 0
}

ensure_native_dependency "https://github.com/google/snappy.git" "app/src/main/jni/snappy" || true

# ============================================================
# librime 与插件源码准备（曾在全新构建机上直接导致 CMake/编译失败）
#   1) librime 必须使用主仓 index 记录的提交（ximeiorg fork），
#      不能拉上游 rime/librime 的 master：上游 core 的 Dictionary/Candidate
#      API 与 librime-correction、librime-t9 不匹配，会报
#      “no member named 'LookupAll' / only virtual member functions can be marked 'override'”。
#   2) librime/plugins 只跟踪 CMakeLists 和适配代码，真正插件源码在主仓
#      兄弟目录（librime-octagram/predict/lua 等）。这些目录是子模块，
#      没初始化时是空目录，plugins 下也不是软链，CMake 的 add_subdirectory
#      会直接报 “does not contain a CMakeLists.txt file”。
# ============================================================
librime_git_url() {
    local url origin_url credential
    url="$(git config -f .gitmodules --get submodule.librime.url 2>/dev/null || true)"
    # .gitmodules 里是 git@github.com: 形式，构建机通常没有 SSH key，改成带凭据的 https
    if [[ "$url" == git@github.com:* ]]; then
        url="https://github.com/${url#git@github.com:}"
    fi
    if [[ "$url" == https://github.com/* ]]; then
        origin_url="$(git remote get-url origin 2>/dev/null || true)"
        if [[ "$origin_url" =~ ^https://([^@/]+)@github\.com/ ]]; then
            credential="${BASH_REMATCH[1]}"
            url="https://${credential}@github.com/${url#https://github.com/}"
        fi
    fi
    printf '%s\n' "$url"
}

prepare_librime() {
    local path=app/src/main/jni/librime
    local commit url
    commit="$(git ls-tree HEAD "$path" | awk '{print $3}')"
    [[ -n "$commit" ]] || return 0
    url="$(librime_git_url)"

    if [[ -d "$path/.git" ]]; then
        git -C "$path" fetch --depth 1 "$url" "$commit" >/dev/null 2>&1 || true
        git -C "$path" checkout -f "$commit" >/dev/null 2>&1 || true
    fi

    # 目录为空、不是 git 仓库，或提交仍对不上时，按 index 提交重新拉取
    if [[ "$(git -C "$path" rev-parse HEAD 2>/dev/null || true)" != "$commit" ]]; then
        echo "重新获取 librime 源码: $commit"
        rm -rf "$path"
        git config "submodule.librime.url" "$url"
        git submodule update --init --depth 1 "$path" || true
        git -C "$path" checkout -f "$commit" >/dev/null 2>&1 || true
    fi

    # librime 自身依赖（glog/leveldb/marisa-trie/opencc/yaml-cpp 等）
    git -C "$path" submodule update --init --depth 1 --recursive >/dev/null 2>&1 || true
}

link_librime_plugin() {
    local name="$1"
    local source_name="${2:-$1}"
    local source="$PWD/app/src/main/jni/$source_name"
    local destination="$PWD/app/src/main/jni/librime/plugins/$name"
    [[ -d "$source" ]] || return 0
    if [[ -e "$destination" && ! -L "$destination" ]]; then
        rm -rf "$destination"
    fi
    if [[ ! -L "$destination" ]]; then
        ln -s "../../$source_name" "$destination"
    fi
}

prepare_librime || echo "警告: librime 源码准备失败，继续尝试构建。" >&2

# 插件子模块：octagram / predict / lua（含 lua 运行时依赖）
for plugin_path in librime-octagram librime-predict librime-lua librime-lua-deps; do
    git submodule update --init --depth 1 "app/src/main/jni/$plugin_path" || true
done

# plugins 目录软链（correction/t9 是主仓普通目录，其余是上面的子模块）
link_librime_plugin librime-correction
link_librime_plugin librime-t9
link_librime_plugin librime-octagram
link_librime_plugin librime-predict
link_librime_plugin lua librime-lua

[ ! -f "$PWD/app/src/main/python/aliyun_git.py" ] && wget -q -O "$PWD/app/src/main/python/aliyun_git.py" https://github.com/QGB/git.bat/raw/refs/heads/master/aliyun_git.py || true
# ============================================================

# 3a) APK 内 pip 依赖锁定：同一主版本沿用上次构建的精确版本，主版本跃迁才升级。
#     结果写入 build/pip-app-requirements.txt，供 app/build.gradle.kts 的 pip{} 读取。
#     联网失败/解析失败只告警：有旧清单就沿用，没有就回退无版本依赖，绝不阻断构建。
mkdir -p "$PROJECT_DIR/build"
python3 - "$PROJECT_DIR" <<'PY' || echo "警告: pip 依赖锁定失败，沿用现有清单或回退无版本依赖。" >&2
import json
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_DIRECT = ["pip", "wheel", "setuptools", "paho-mqtt", "ecdsa", "requests"]
INDEXES = ["https://pypi.org/simple", "https://pypi.tuna.tsinghua.edu.cn/simple"]

project = Path(sys.argv[1])
lock_path = project / "pip-app-lock.json"
out_path = project / "build" / "pip-app-requirements.txt"


def major(version):
    return str(version).split(".")[0]


def read_lock():
    try:
        return json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def pip_dry_run(specs, index_url):
    """只解析不安装，从 --report 拿完整依赖图；任何异常都视为失败。"""
    handle, name = tempfile.mkstemp(prefix="xime-pip-app-", suffix=".json")
    report_path = Path(name)
    try:
        command = [
            sys.executable, "-m", "pip", "install",
            "--dry-run", "--ignore-installed", "--disable-pip-version-check",
            "--no-input", "--quiet", "--report", str(report_path),
            "-i", index_url,
        ]
        if "tuna" in index_url:
            command += ["--trusted-host", "pypi.tuna.tsinghua.edu.cn"]
        command += specs
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            return False, {}
        report = json.loads(report_path.read_text(encoding="utf-8"))
        packages = {}
        for item in report.get("install", []):
            metadata = item.get("metadata", {})
            package = str(metadata.get("name", "")).strip().lower().replace("_", "-")
            version = str(metadata.get("version", "")).strip()
            if package and version:
                packages[package] = version
        return bool(packages), packages
    except (OSError, ValueError, subprocess.SubprocessError):
        return False, {}
    finally:
        report_path.unlink(missing_ok=True)


def main():
    lock = read_lock()
    locked = {name: str(info.get("version", ""))
              for name, info in (lock.get("packages") or {}).items()}
    direct = lock.get("direct") or DEFAULT_DIRECT

    latest = {}
    for index in INDEXES:
        ok, packages = pip_dry_run(list(direct), index)
        if ok:
            latest = packages
            break
    if not latest:
        if out_path.is_file():
            print("[pip] 包索引不可用，沿用上一份精确依赖清单（离线构建）。")
        else:
            print("[pip] 包索引不可用且无历史清单，回退无版本依赖。")
        return 0

    targets, added, jumps = {}, [], []
    for name, version in latest.items():
        old = locked.get(name)
        if not old:
            targets[name] = version
            added.append(name)
        elif major(old) == major(version):
            # 小版本/补丁差异一律沿用上次构建的精确版本。
            targets[name] = old
        else:
            targets[name] = version
            jumps.append((name, old, version))

    specs = [f"{name}=={version}" for name, version in sorted(targets.items())]
    installable = False
    for index in INDEXES:
        installable, _ = pip_dry_run(specs, index)
        if installable:
            break
    if not installable:
        # 例如直接依赖的新主版本与传递依赖上界冲突：放开全图重新解析。
        print("[pip] 锁定集合无法同时安装，放开全图重新解析。")
        targets = dict(latest)

    lines = [f"{name}=={version}" for name, version in sorted(targets.items())]
    payload = "\n".join(lines) + "\n"
    if out_path.is_file() and out_path.read_text(encoding="utf-8") == payload:
        print("[pip] 精确依赖未变化: " + ", ".join(lines))
        return 0
    out_path.write_text(payload, encoding="utf-8")
    print("[pip] 精确依赖: " + ", ".join(lines))
    if added:
        print("[pip] 新增: " + ", ".join(sorted(added)))
    for name, old, new in jumps:
        print(f"[pip] 主版本跃迁: {name} {old} -> {new}")
    return 0


sys.exit(main())
PY
# ============================================================


# 2) 清理旧产物，确保输出是这个 secexp 对应的新包
find "$OUT_DIR" -maxdepth 1 -type f -name "${APK_BASENAME}-*.apk" -delete 2>/dev/null || true

# 3) 先清理并编译 debug APK，确保同一输入输出一致。
rm -rf "$PROJECT_DIR/app/build/outputs/apk/debug" "$PROJECT_DIR/out/debug-secexp"
# 启动图标保证：清掉资源合并/打包中间产物。
# 历史问题：资源任务判定 up-to-date，packaged_res 还停留在旧状态，
# 导致 app/src/debug/res 里的图标没进 APK（即使 mergeDebugResources 已执行）。
rm -rf "$PROJECT_DIR/app/build/intermediates/merged_res" \
       "$PROJECT_DIR/app/build/intermediates/packaged_res" \
       "$PROJECT_DIR/app/build/intermediates/incremental/debug/mergeDebugResources" \
       "$PROJECT_DIR/app/build/intermediates/incremental/debug/packageDebugResources"
#mkdir -p "$PROJECT_DIR/out/debug-secexp"
./gradlew --no-daemon assembleDebug \
  --quiet \
  -PappName="$APP_NAME" \
  -PappChineseName="$APP_CHINESE_NAME" \
  -PapplicationId="$APPLICATION_ID" \
  -PversionCode="$VERSION_CODE" \
  -PversionName="$VERSION_NAME" \
  -PbuildAbis="$BUILD_ABIS" \
  -PpythonVersion="$PYTHON_VERSION" \
  -PappIconPath="$APP_ICON_PATH"

# 4) 找到编译出的 APK
APK_PATH="$(find "$PROJECT_DIR/app/build/outputs/apk/debug" -maxdepth 1 -type f -name "${APK_BASENAME}-${VERSION_CODE}-*.apk" -print -quit)"
if [[ -z "$APK_PATH" ]]; then
    APK_PATH="$(find "$PROJECT_DIR/app/build/outputs/apk/debug" -maxdepth 1 -type f -name '*.apk' -print -quit)"
fi
if [[ -z "$APK_PATH" ]]; then
    echo "错误: 未找到 debug APK，编译失败或输出命名和预期不一致。" >&2
    exit 1
fi

# 4b) 回写 pip 锁：以 APK 内实际安装的 dist-info 为准（Chaquopy 自己解析依赖，
#     可能与主机解析不同，锁必须记录真正进包的那一套）。失败只告警，不影响出包。
python3 - "$APK_PATH" "$PROJECT_DIR/pip-app-lock.json" <<'PY' || echo "警告: 回写 APK pip 锁失败（不影响出包）。" >&2
import io
import json
import re
import sys
import zipfile
from pathlib import Path

DEFAULT_DIRECT = ["pip", "wheel", "setuptools", "paho-mqtt", "ecdsa", "requests"]
apk = Path(sys.argv[1])
lock_path = Path(sys.argv[2])

packages = {}
with zipfile.ZipFile(apk) as archive:
    for name in archive.namelist():
        if not (name.startswith("assets/chaquopy/requirements-") and name.endswith(".imy")):
            continue
        with zipfile.ZipFile(io.BytesIO(archive.read(name))) as imy:
            for entry in imy.namelist():
                if not re.match(r"[^/]+\.dist-info/METADATA$", entry):
                    continue
                text = imy.read(entry).decode("utf-8", "replace")
                meta_name = ""
                meta_version = ""
                for line in text.splitlines():
                    if line.startswith("Name: "):
                        meta_name = line[6:].strip()
                    elif line.startswith("Version: "):
                        meta_version = line[9:].strip()
                if meta_name and meta_version:
                    key = meta_name.lower().replace("_", "-")
                    packages[key] = {"version": meta_version}

if not packages:
    print("[pip] APK 内未找到 dist-info，跳过锁回写。")
    sys.exit(0)

try:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
except (OSError, ValueError):
    lock = {}

previous = lock.get("packages") or {}
for name in sorted(set(previous) | set(packages)):
    before = (previous.get(name) or {}).get("version", "-")
    after = (packages.get(name) or {}).get("version", "-")
    if before != after:
        print(f"[pip] 锁刷新: {name} {before} -> {after}")

lock.setdefault("schema", 1)
lock.setdefault("policy", "same-major-reuse")
lock.setdefault(
    "note",
    "APK 内 pip 依赖锁：同主版本沿用已锁精确版本，仅主版本跃迁时升级；由 debug_build_secexp.sh 生成/刷新。",
)
lock.setdefault("python", "")
lock.setdefault("direct", DEFAULT_DIRECT)
lock["packages"] = {name: packages[name] for name in sorted(packages)}
payload = json.dumps(lock, indent=2, ensure_ascii=False) + "\n"
if lock_path.is_file() and lock_path.read_text(encoding="utf-8") == payload:
    print("[pip] APK pip 锁未变化。")
else:
    lock_path.write_text(payload, encoding="utf-8")
    print(f"[pip] 已更新 APK pip 锁: {lock_path}")
PY

# 5) 先把 ZIP 元数据归一化，消除时间戳和文件顺序造成的差异，再签名
#    Chaquopy 的 imy 内嵌 .pyc，头部带源码 mtime（等于构建时刻），不归一
#    app.imy/requirements-*.imy 永远对不上；改完 imy 必须同步 build.json 的 sha1。
REPACKED_APK="$PROJECT_DIR/app/build/outputs/apk/debug/repacked-${VERSION_CODE}-${BUILD_ABIS}.apk"
python3 - "$APK_PATH" "$REPACKED_APK" <<'PY'
import hashlib
import io
import json
import struct
import sys
import zipfile
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])


def normalize_pyc(raw):
    """pyc 头归一：magic(4) + flags(4) + mtime(4) + source_size(4)。

    flags bit0=1 表示 hash-based pyc（尾部是源哈希），改写会破坏校验，保持原样；
    timestamp-based 只把 mtime 置 0，source_size 保留。
    """
    if len(raw) < 16:
        return raw
    flags = struct.unpack("<I", raw[4:8])[0]
    if flags & 1:
        return raw
    return raw[:8] + b"\x00\x00\x00\x00" + raw[12:]


def repack_imy(raw):
    """imy 是标准 zip：条目排序 + 固定时间戳，内部 pyc 的 mtime 归零。"""
    with zipfile.ZipFile(io.BytesIO(raw)) as zin:
        members = sorted(zin.infolist(), key=lambda info: info.filename)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zout:
            for info in members:
                data = zin.read(info.filename)
                if info.filename.endswith(".pyc"):
                    data = normalize_pyc(data)
                new_info = zipfile.ZipInfo(info.filename, (1980, 1, 1, 0, 0, 0))
                new_info.compress_type = info.compress_type
                new_info.create_system = 3
                new_info.external_attr = info.external_attr
                new_info.internal_attr = info.internal_attr
                new_info.extra = b""
                new_info.comment = b""
                zout.writestr(new_info, data)
        return buffer.getvalue()


with zipfile.ZipFile(src, 'r') as zin:
    members = sorted(zin.infolist(), key=lambda i: i.filename)
    contents = {info.filename: zin.read(info.filename) for info in members}

imy_names = [name for name in contents
             if name.startswith("assets/chaquopy/") and name.endswith(".imy")]
for name in imy_names:
    try:
        contents[name] = repack_imy(contents[name])
    except Exception as error:
        print(f"警告: 归一化 {name} 失败，保留原样: {error}", file=sys.stderr)

# build.json 记录每个 imy 的 sha1，imy 字节变了必须同步，否则 Chaquopy 运行期校验失败。
build_json_name = "assets/chaquopy/build.json"
if imy_names and build_json_name in contents:
    try:
        metadata = json.loads(contents[build_json_name].decode("utf-8"))
        assets_map = metadata.setdefault("assets", {})
        for name in imy_names:
            assets_map[Path(name).name] = hashlib.sha1(contents[name]).hexdigest()
        contents[build_json_name] = json.dumps(metadata, indent=4).encode("utf-8")
    except Exception as error:
        print(f"警告: 同步 build.json 失败，保留原样: {error}", file=sys.stderr)

with zipfile.ZipFile(dst, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
    for info in members:
        data = contents[info.filename]
        new_info = zipfile.ZipInfo(info.filename)
        new_info.compress_type = info.compress_type
        new_info.date_time = (1980, 1, 1, 0, 0, 0)
        new_info.extra = b''
        new_info.comment = b''
        new_info.create_system = 3
        new_info.external_attr = info.external_attr
        new_info.internal_attr = info.internal_attr
        zout.writestr(new_info, data)
PY
rm -f "$APK_PATH"
APK_PATH="$REPACKED_APK"

# 5b) zipalign：上面的 Python 重打包只保留压缩方式、不保证对齐，
#     Android 11+（targetSdk>=30）要求 resources.arsc 等 STORED 条目 4 字节对齐，
#     否则安装会失败（如 -124 Failed parse during installPackageLI）。
#     zipalign 会改字节，必须在 apk_sign.py 签名之前执行；输入输出固定，仍可复现。
ZIPALIGN_BIN="$(find "$ANDROID_HOME_DEFAULT/build-tools" -maxdepth 2 -type f -name zipalign -print 2>/dev/null | sort -V | tail -n 1)"
if [[ -z "$ZIPALIGN_BIN" || ! -x "$ZIPALIGN_BIN" ]]; then
    echo "错误: 未找到可执行的 zipalign，SDK 路径: $ANDROID_HOME_DEFAULT" >&2
    exit 1
fi
ALIGNED_APK="$PROJECT_DIR/app/build/outputs/apk/debug/aligned-${VERSION_CODE}-${BUILD_ABIS}.apk"
"$ZIPALIGN_BIN" -f -p 4 "$APK_PATH" "$ALIGNED_APK"
rm -f "$APK_PATH"
APK_PATH="$ALIGNED_APK"
"$ZIPALIGN_BIN" -c -p 4 "$APK_PATH"

# 6) 使用 apk_sign.py 通过 secexp 确定性生成签名密钥并签名。
#    构建时间由签名脚本写入签名块内的自定义 pair（不在 v2/v3 摘要范围内），
#    改时间不改变签名值，整包只在签名块的 48 字节时间字段上不同。
#    BUILD_TIME_VALUE 可固定时间（可重复性测试），缺省取当前构建机本地时间。
SIGN_TIME_ARGS=()
if [[ -n "${BUILD_TIME_VALUE:-}" ]]; then
  SIGN_TIME_ARGS+=(--build-time "$BUILD_TIME_VALUE")
fi
python3 "$PROJECT_DIR/apk_sign.py" "$APK_PATH" \
  --mode secexp \
  --secexp "$SECEXP" \
  --sdk "$ANDROID_HOME_DEFAULT" \
  "${SIGN_TIME_ARGS[@]}"

SIGNED_APK="${APK_PATH%.apk}-signed.apk"
if [[ ! -f "$SIGNED_APK" ]]; then
    echo "错误: 签名后 APK 未生成：$SIGNED_APK" >&2
    exit 1
fi

# 7) 再复制一份到导出目录，方便直接取用，并保留到全局缓存目录用于真实文件对比
OUT_APK="$OUT_DIR/${APK_BASENAME}-${VERSION_CODE}-${BUILD_ABIS}.apk"
cp -f "$SIGNED_APK" "$OUT_APK"
rm -f "$SIGNED_APK" "$APK_PATH"

echo
echo "SECEXP=$SECEXP"
echo "APK=$OUT_APK"