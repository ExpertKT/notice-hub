# 群务台安卓 App

## 方案

采用原生 WebView 封装（方案 b），工程位于 `android/`，不改后端、不新增接口。它比 PWA 更符合“安卓 App”：可安装 APK、首次设置服务器地址/token，之后自动打开现有网页；也避免给单文件 `webapp.py` 增加静态资源路由。Flutter/React Native/Capacitor 未选：本机没有对应 Android 构建链，且 WebView 已覆盖需求。

## 工具链实测

2026-10-09 在 PowerShell 执行：

- `java -version`：失败，`java` 无法识别（未安装/未加入 PATH）。
- `$env:ANDROID_HOME`、`$env:ANDROID_SDK_ROOT`：均为空；`adb`、`sdkmanager`、`gradle` 均未找到。
- `node --version`：`v24.20.0`；`npm --version` / `npx --version`：`11.19.0`。
- `F:\qq-notice-hub\.venv\Scripts\python.exe -m pip show buildozer kivy briefcase`：`Package(s) not found`。

因此本机未构建出 APK。当前工程可在安装 JDK 17、Android SDK（platform 35/build-tools）和 Gradle 8.9+ 后构建。

## 构建与安装

在 `F:\qq-notice-hub\android` 执行：

```powershell
gradle :app:assembleDebug
adb install -r .\app\build\outputs\apk\debug\app-debug.apk
```

或用 Android Studio 打开 `android/`，执行 `app > assembleDebug`。安装后首次输入 `http://电脑IP:8766`（或 Tailscale HTTPS 地址）和 token；保存后自动打开待办/月历。地址/token 用 SharedPreferences 持久保存。再次启动直接打开已保存地址。

## 行为

- 复用现有网页和 API，不修改服务器。
- 连接失败提示：请确认电脑上的群务台正在运行，且手机与电脑在同一 Wi-Fi 或已开 Tailscale。
- 返回键优先返回网页历史；不同域名链接交给系统浏览器。
- `resizeableActivity` 与未锁定方向支持竖屏/横屏。
- 仅允许 HTTP/HTTPS 地址，避免把任意输入交给 WebView。

## 用户只需做

1. 在电脑安装 JDK 17、Android SDK platform 35/build-tools 35，并准备 Gradle 8.9+ 或 Android Studio。
2. 在 `F:\qq-notice-hub\android` 构建并安装上面的 APK。
3. 手机与电脑同一 Wi-Fi，或使用 Tailscale；打开 App 填服务器地址和 token。

## APK 构建实测

已在本机安装 Temurin JDK 17、Android command-line tools、platform-tools、Android 35 platform、Build Tools 35.0.0，以及 Gradle 8.9，并生成 Gradle wrapper。构建命令：

```powershell
$env:JAVA_HOME='C:\Program Files\Eclipse Adoptium\jdk-17.0.20.101-hotspot'
$env:ANDROID_HOME='F:\android-sdk'
cd F:\qq-notice-hub\android
.\gradlew.bat assembleDebug --no-daemon
```

结果：`BUILD SUCCESSFUL`。APK：`F:\qq-notice-hub\android\app\build\outputs\apk\debug\app-debug.apk`；大小 14740 bytes；SHA256 `C93B06EEE23A2050149104704ABA2E53668DC8CD4AA53DFC2545350D3B8C794F`。`aapt2 dump badging` 验证：包名 `com.noticehub.app`、`minSdkVersion:'23'`、`targetSdkVersion:'35'`、`launchable-activity: com.noticehub.app.MainActivity`；ZIP 内含 `AndroidManifest.xml`、`classes.dex`。安装：手机开启允许安装未知来源，传 APK 到手机，点击安装；打开后填写地址和 token。

未改 `webapp.py`、`tests/` 或装机目录。
