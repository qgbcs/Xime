import com.android.build.gradle.internal.api.BaseVariantOutputImpl
import org.gradle.api.tasks.Sync
import java.security.MessageDigest
import java.util.Base64
import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    id("com.chaquo.python")
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.ksp)
}

chaquopy {
    defaultConfig {
        version = "3.13"
        // 远端构建容器的 buildPython 必须与目标主次版本一致，显式指向 3.13
        buildPython = listOf("/usr/bin/python3.13")
        pip {
            install("pip")
            install("wheel")
            install("setuptools")
            install("paho-mqtt")
            install("ecdsa")
            install("requests")
        }
    }
}

apply(from = "build-logic/tasks-native.gradle.kts")
apply(from = "build-logic/tasks-plugin-dev.gradle.kts")

// 获取 Git 提交哈希
fun getGitHash(): String {
    return try {
        val process = ProcessBuilder("git", "rev-parse", "--short", "HEAD")
            .directory(rootDir)
            .start()
        process.inputStream.bufferedReader().readText().trim()
    } catch (e: Exception) {
        "unknown"
    }
}

fun getSourceFingerprint(): String {
    val sourceRoots = listOf(
        file("src/main/java"),
        file("src/main/jni/librime_jni"),
        file("src/main/python"),
        file("src/main/assets"),
        file("src/main/res"),
        file("src/main/AndroidManifest.xml"),
    )
    val sourceFiles = sourceRoots
        .flatMap { root ->
            if (root.isFile) listOf(root) else root.walkTopDown().filter { it.isFile }.toList()
        }
        .sortedBy { it.relativeTo(rootProject.projectDir).invariantSeparatorsPath }
    val digest = MessageDigest.getInstance("SHA-256")
    sourceFiles.forEach { source ->
        digest.update(source.relativeTo(rootProject.projectDir).invariantSeparatorsPath.toByteArray(Charsets.UTF_8))
        digest.update(0.toByte())
        source.inputStream().use { input ->
            val buffer = ByteArray(8192)
            while (true) {
                val read = input.read(buffer)
                if (read < 0) break
                digest.update(buffer, 0, read)
            }
        }
    }
    return digest.digest().joinToString("") { it.toInt().and(0xff).toString(16).padStart(2, '0') }
}

val supportedAbis = listOf("armeabi-v7a", "arm64-v8a", "x86", "x86_64")
val buildAbis = providers.gradleProperty("buildAbis")
    .orNull
    ?.split(",")
    ?.map { it.trim() }
    ?.filter { it.isNotEmpty() }
    ?: supportedAbis
require(buildAbis.isNotEmpty() && buildAbis.all { it in supportedAbis }) {
    "buildAbis must contain only: ${supportedAbis.joinToString(", ")}"
}

val configuredAppName = providers.gradleProperty("appName").orElse("输入法").get()
val configuredChineseAppName = providers.gradleProperty("appChineseName").orElse("默认").get()
val configuredAppIconPath = providers.gradleProperty("appIconPath")
    .orElse(providers.environmentVariable("APP_ICON_PATH"))
    .orElse(layout.projectDirectory.file("src/main/res/drawable/ic_launcher_foreground.xml").asFile.absolutePath)
val configuredAppIcon = configuredAppIconPath.map { file(it).canonicalFile }
val configuredAppIconExtension = configuredAppIcon.map { it.extension.lowercase() }
val generatedAppIconResources = layout.buildDirectory.dir("generated/appIcon/res")
val generateAppIconResources = tasks.register<Sync>("generateAppIconResources") {
    val iconFile = configuredAppIcon.get()
    val iconExtension = configuredAppIconExtension.get()
    require(iconFile.isFile && iconFile.canRead() && iconFile.length() > 0) {
        "APP_ICON_PATH is missing, unreadable, or empty: $iconFile"
    }
    require(iconExtension in setOf("png", "webp", "xml")) {
        "APP_ICON_PATH must point to a PNG, WebP, or Android vector XML file: $iconFile"
    }
    // 图标不能放进 drawable（默认密度桶按 mdpi 处理）：高密度设备会按 density/160 放大，
    // 1760px 的 PNG 在 560dpi 下被放大成 6160px（约 145MB），绘制时触发
    // "Canvas: trying to draw too large" 崩溃。nodpi 桶不做密度缩放。
    // Sync 的根目录设为生成目录，顺带清掉旧版本遗留在 drawable/ 下的图标。
    val generatedDir = generatedAppIconResources.get().asFile
    inputs.file(iconFile)
    into(generatedDir)
    from(iconFile) {
        into("drawable-nodpi")
        rename { "ic_launcher_foreground.$iconExtension" }
    }
    doLast {
        if (iconExtension != "png") return@doLast
        // 大图标缩到图标够用的尺寸，避免输入法内存里常驻十几 MB 位图。
        // 用 python3 + Pillow 处理：Gradle 脚本的编译类路径不含 java.awt，且项目构建本就依赖 python3。
        // 没有 python3/Pillow 时保留原图，drawable-nodpi 已能保证不触发密度放大崩溃。
        val target = generatedDir.resolve("drawable-nodpi/ic_launcher_foreground.png")
        val maxSize = 432
        val script = """
            import sys
            try:
                from PIL import Image
            except Exception:
                sys.exit(3)
            path, limit = sys.argv[1], int(sys.argv[2])
            image = Image.open(path).convert("RGBA")
            scale = limit / max(image.size)
            if scale < 1:
                size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
                image.resize(size, Image.LANCZOS).save(path, "PNG")
        """.trimIndent()
        try {
            val process = ProcessBuilder(
                "python3", "-c", script, target.absolutePath, maxSize.toString()
            ).redirectErrorStream(true).start()
            // 先把输出读完，避免管道写满阻塞。
            process.inputStream.bufferedReader().readText()
            process.waitFor()
        } catch (_: java.io.IOException) {
            // 本机没有 python3 时跳过缩放。
        }
    }
}
val configuredApplicationId = providers.gradleProperty("applicationId")
    .orElse("com.kingzcheung.xime")
    .get()
val configuredVersionCode = providers.gradleProperty("versionCode")
    .orElse("20260828")
    .get()
    .toInt()
val configuredVersionName = providers.gradleProperty("versionName")
    .orElse("版本号字符串安装界面最多显示超过会用省略号表示长度17个字符android规范合法的是1024")
    .get()
val escapedAppName = configuredAppName.replace("\\", "\\\\").replace("\"", "\\\"")
val escapedChineseAppName = configuredChineseAppName.replace("\\", "\\\\").replace("\"", "\\\"")

// 获取构建时间已移除：构建时刻会写入 BuildConfig 进而进入 classes.dex，
// 破坏 F-Droid 可复现构建（不同环境构建时间不同导致产物不一致）。

// 加载签名配置
val keystorePropertiesFile = rootProject.file("app/keystore.properties")
val keystoreProperties = Properties()
if (keystorePropertiesFile.exists()) {
    keystoreProperties.load(keystorePropertiesFile.inputStream())
}

android {
    namespace = "com.kingzcheung.xime"
    compileSdk = 36

    defaultConfig {
        applicationId = configuredApplicationId
        minSdk = 27
        targetSdk = 35
        versionCode = configuredVersionCode
        versionName = configuredVersionName

        buildConfigField("String", "APP_NAME", "\"$escapedAppName\"")
        buildConfigField("String", "APP_CHINESE_NAME", "\"$escapedChineseAppName\"")
        resValue("string", "app_name", configuredAppName)


        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        // NDK 配置
        ndk {
            abiFilters += buildAbis
        }

        // 构建信息
        buildConfigField("String", "GIT_HASH", "\"${getGitHash()}\"")
        buildConfigField("String", "SOURCE_FINGERPRINT", "\"${getSourceFingerprint()}\"")
    }

    signingConfigs {
        getByName("debug") {
            val debugKeystore = rootProject.file("../.android/debug.keystore")
            if (debugKeystore.exists()) {
                // 绑定到固定 debug keystore，避免不同容器/不同时间生成不同签名。
                storeFile = debugKeystore
                storePassword = "android"
                keyAlias = "androiddebugkey"
                keyPassword = "android"
            }
        }

        create("release") {
            if (keystorePropertiesFile.exists()) {
                // 优先使用 storeFile（已存在的 keystore 文件）；否则解码 keyBase64。
                // keyBase64 方式用于 CI/F-Droid 从 secrets 注入签名。
                val storeFileProp = keystoreProperties.getProperty("storeFile")?.let { file(it) }
                storeFile = if (storeFileProp != null && storeFileProp.exists()) {
                    storeFileProp
                } else {
                    keystoreProperties.getProperty("keyBase64")?.let { keyBase64 ->
                        val ks = File(layout.buildDirectory.get().asFile, "release-keystore.jks")
                        ks.writeBytes(Base64.getDecoder().decode(keyBase64.replace("\n", "").trim()))
                        ks
                    }
                }
                storePassword = keystoreProperties.getProperty("storePassword")
                keyAlias = keystoreProperties.getProperty("keyAlias")
                keyPassword = keystoreProperties.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        debug {
            // 让 debug 构建稳定复用固定 keystore，避免每次新环境都生成不同证书。
            signingConfig = signingConfigs.getByName("debug")
        }

        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            // 只在本地有 keystore.properties 时才使用签名配置
            // GitHub Actions 使用自己的签名方式
            if (keystorePropertiesFile.exists()) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
        isCoreLibraryDesugaringEnabled = true
    }
    kotlin {
        compilerOptions {
            jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
            freeCompilerArgs.add("-Xunused")
        }
    }
    buildFeatures {
        compose = true
        buildConfig = true
        aidl = true
    }

    // NDK 构建配置
    externalNativeBuild {
        cmake {
            path = file("src/main/jni/CMakeLists.txt")
            version = "3.22.1"
        }
    }

    // 打包配置
    packaging {
        jniLibs {
            useLegacyPackaging = true
        }
    }
    ndkVersion = "29.0.14206865"

    // F-Droid 一致性验证：不在 APK 中写入依赖元数据（Dependency metadata 签名块）。
    dependenciesInfo {
        includeInApk = false
        includeInBundle = false
    }

    // 测试 classpath 包含 main assets，使 T9Decoder() 无参构造可加载 pinyin_lm.bin
    sourceSets {
        getByName("debug") {
            res.srcDir(generatedAppIconResources.get().asFile)
        }
        getByName("release") {
            res.srcDir(generatedAppIconResources.get().asFile)
        }
        getByName("test") {
            resources.srcDirs("src/main/assets")
        }
    }
    lint {
        checkReleaseBuilds = false
        checkGeneratedSources = false
        abortOnError = false
        checkDependencies = true
    }

    // 分架构打包
    splits {
        abi {
            isEnable = buildAbis.size > 1
            reset()
            if (buildAbis.size > 1) {
                include(*buildAbis.toTypedArray())
            }
            isUniversalApk = buildAbis.size > 1
        }
    }
}

tasks.configureEach {
    if ((name.startsWith("generate") || name.startsWith("merge") ||
            name.startsWith("process") || name.startsWith("map")) &&
        (name.contains("Debug") || name.contains("Release"))
    ) {
        dependsOn(generateAppIconResources)
    }
}

android.applicationVariants.all {
    val appName = "Xime"
    outputs.all {
        val abi = filters.find { it.filterType.toString() == "ABI" }?.identifier
            ?: buildAbis.singleOrNull()
            ?: "universal"
        (this as BaseVariantOutputImpl).outputFileName = "$appName-$versionCode-$abi.apk"
    }
}

// Nightly 构建通过 androidComponents API 覆盖 versionCode/versionName
androidComponents {
    onVariants { variant ->
        val vc = project.findProperty("versionCode")?.toString()?.toIntOrNull()
        val vn = project.findProperty("versionName")?.toString()
        if (vc != null && vn != null) {
            variant.outputs.forEach { output ->
                output.versionCode.set(vc)
                output.versionName.set(vn)
            }
        }
    }
}
dependencies {
    implementation(project(":plugin-core"))
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.appcompat)
    implementation(libs.material)

    // Kotlin stdlib - CRITICAL for plugin compatibility
    implementation("org.jetbrains.kotlin:kotlin-stdlib:2.4.10")
    implementation(libs.kotlinx.coroutines.core)
    implementation(libs.androidx.work.runtime.ktx)

    // Jetpack Compose
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.foundation)
    implementation(libs.androidx.activity.compose)

    // Material Icons
    implementation(libs.androidx.compose.material.icons.core)
    implementation(libs.androidx.compose.material.icons.extended)

    // Navigation
    implementation(libs.androidx.navigation.compose)

    // Lifecycle
    implementation(libs.androidx.lifecycle.runtime)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)

    // SavedState
    implementation(libs.androidx.savedstate)

    // Coil (Image Loading)
    implementation(libs.coil)

    // OkHttp for WebSocket and model download
    implementation("com.squareup.okhttp3:okhttp:5.4.0")
    // Apache Commons Compress for tar.bz2 extraction
    implementation("org.apache.commons:commons-compress:1.28.0")

    // Kaml for YAML parsing
    implementation(libs.kaml)

    // Autofill inline suggestions (API 30+)
    implementation(libs.androidx.autofill)

    // exp4j for calculator expression evaluation
    implementation(libs.exp4j)

    // ZXing for QR code generation
    implementation("com.google.zxing:core:3.5.4")

    // Ktor embedded server for wireless import
    implementation("io.ktor:ktor-server-core:3.5.2")
    implementation("io.ktor:ktor-server-cio:3.5.2")
    implementation(libs.kotlinx.serialization.json)

    // Room 3.0 (SQLite)
    implementation(libs.androidx.room3.runtime)
    ksp(libs.androidx.room3.compiler)

    // Sora Code Editor for YAML viewing/editing
    implementation(platform("io.github.rosemoe:editor-bom:0.24.6"))
    implementation("io.github.rosemoe:editor")
    implementation("io.github.rosemoe:language-textmate")
    coreLibraryDesugaring("com.android.tools:desugar_jdk_libs:2.1.5")

    debugImplementation(libs.androidx.compose.ui.tooling)

    testImplementation(libs.junit)
    testImplementation(libs.kotlinx.coroutines.core)
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.11.0")
    testImplementation("org.mockito:mockito-core:5.23.0")
    testImplementation("org.mockito.kotlin:mockito-kotlin:5.2.1")

    androidTestImplementation(libs.androidx.junit)
    androidTestImplementation(libs.androidx.espresso.core)
    androidTestImplementation(platform(libs.androidx.compose.bom))
    androidTestImplementation(libs.androidx.compose.ui.test.junit4)
    androidTestImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.11.0")
    androidTestImplementation("androidx.test:runner:1.6.2")
    androidTestImplementation("androidx.test:rules:1.6.1")
    androidTestImplementation("androidx.concurrent:concurrent-futures:1.2.0")
}

// Align concurrent-futures version: espresso 3.7.0 requires 1.2.0
dependencies {
    constraints {
        implementation("androidx.concurrent:concurrent-futures:1.2.0") {
            because("test dependencies (espresso 3.7.0) require 1.2.0")
        }
    }
}
