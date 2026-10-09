# 群务台安卓 App

把电脑上跑着的群务台，装成手机上的 App——不用每次开浏览器输地址，点开就是待办和月历。

> 这个 App 是**客户端外壳**：它本身不接收 QQ 消息，也没有云端。
> 它只是把你的手机和**你自己的电脑**连起来（WebView 打开你电脑上的群务台网页）。
> 所以电脑上的群务台必须在运行。

---

## 需要什么

| 条件 | 说明 |
| --- | --- |
| 电脑上群务台正在运行 | 电脑浏览器打开 `http://127.0.0.1:8766` 能看到待办台即可 |
| 手机与电脑在同一网络 | 同一 Wi-Fi 最省事；用流量或不同网络，需要一个公网地址（推荐 Tailscale，见 [使用说明](使用说明.md)） |
| 手机系统 | Android 6.0（API 23）及以上 |

App 只申请 **网络访问** 一个权限，不读通讯录、不读相册、不定位、无广告、无统计上报。

---

## 安装

1. 到 [Releases](https://github.com/ExpertKT/notice-hub/releases/latest) 下载 **`notice-hub-android.apk`**。
2. 在手机上点开这个文件。系统会提示「禁止安装未知应用」——按提示允许当前使用的浏览器或文件管理器安装未知应用，然后继续安装。
   - 小米/红米：设置 → 应用设置 → 授权管理 → 安装未知应用
   - 华为/荣耀：设置 → 安全 → 更多安全设置 → 外部来源应用下载
   - OPPO / vivo / 一加：设置里搜「未知来源」或「外部来源应用」
3. 桌面出现「群务台」图标，点开即可。

> 下载页面对匿名访客是公开的，不需要 GitHub 账号；如果浏览器提示文件不安全，是因为 APK 属于「未知来源应用」的常规提醒，选择保留即可。

---

## 首次打开：填两个值

App 第一次启动会显示一个设置页：

| 填什么 | 从哪里拿 |
| --- | --- |
| **服务器地址** | 同一 Wi-Fi：`http://电脑IP:8766`（电脑端「设置 → 订阅地址」里也会列出可用的局域网地址）<br />不同网络：Tailscale 的 `https://你的机器.xxx.ts.net` |
| **Token** | 电脑端网页地址栏里 `?token=...` 的那一串；或「设置」页订阅卡片里复制 |

地址写错时 App 会提示「地址应类似 http://电脑IP:8766 或 https://域名」。

保存后地址与 token 存在手机本地，下次启动直接进网页，不再需要输入。

---

## 改地址 / 换服务器 / 连不上

- **连不上**：先确认电脑上的群务台在运行（托盘图标存在、电脑浏览器能打开待办台）；再确认手机与电脑在同一 Wi-Fi，或你填的是公网（Tailscale）地址。
- **改地址**：App 里**长按返回键**即可回到设置页重新填写；网页加载失败时也会自动回到设置页，不会让你卡死在白屏里。
- 换了电脑、换了路由器 IP、或 Tailscale 域名变了，都按上面的方式重新填一次即可。

---

## 行为说明

- 复用电脑上现有的网页与接口，不修改服务器，也不需要额外的后端。
- 网页内的链接：同主机继续在 App 里打开；外部链接交给系统浏览器。
- 返回键优先返回网页历史，退到头再按一下退出 App。
- 支持竖屏/横屏旋转。
- 只接受 `http://` 或 `https://` 地址，避免把任意输入交给 WebView。

---

## 更新与卸载

- **更新**：下载新版 APK 覆盖安装即可（从 v2026.10.18 起所有版本用同一个签名密钥）。
  - ⚠️ 如果你装过 v2026.10.17 里那个 debug 开发包，需要先卸载再装正式包——签名不同，Android 不允许覆盖。
- **卸载**：长按图标 → 卸载。手机上的数据只有那个服务器地址和 token，电脑上的消息、待办、日志都不受影响。

---

## 开发：自己构建

<details>
<summary>工具链、命令与签名说明</summary>

需要 JDK 17、Android SDK（platform 35 + build-tools 35.0.0）、Gradle 8.9（工程已带 wrapper）。

```powershell
$env:JAVA_HOME='C:\Program Files\Eclipse Adoptium\jdk-17.0.20.101-hotspot'
$env:ANDROID_HOME='F:\android-sdk'
cd android
.\gradlew.bat assembleRelease --no-daemon   # 正式包（需要 keystore.properties）
.\gradlew.bat assembleDebug --no-daemon     # 开发包（用 debug 签名，不用于发布）
```

- **签名**：正式包使用 `android/keystore.properties` 指向的密钥库（**不入版本库**，也已在 `.gitignore` 里排除）。
  换密钥会导致老用户无法覆盖安装，请固定复用同一个密钥库。
- **打包产物**：`android/app/build/outputs/apk/release/app-release.apk`，随每个版本重命名为 `notice-hub-android.apk` 上传到 Releases。
- **图标**：由 `assets/icon-512.png` 缩放生成 `res/mipmap-*`。
- **包信息**：包名 `com.noticehub.app`，`minSdk 23`，`targetSdk 35`，`MainActivity` 为唯一入口。

</details>
