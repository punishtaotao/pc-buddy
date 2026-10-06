# 电脑管家 iOS App（原生 SwiftUI）

这是手机端的**原生 App**，不是网页。功能和网页版一致，另外多了几样网页做不到的：

- **扫二维码配对**（调摄像头直接读电脑上那个二维码，不用手打 IP 和口令）
- **系统通知**：App 切到后台时，任务做完了会给你推一条通知
- **原生拍照 / 相册**、原生弹窗、原生手感
- 已配好的 App 图标和启动屏

---

## ⚠️ 先读这一段

**我（写这段代码的 AI）跑在 Windows 上，Xcode 是 macOS 独占的，所以我没能编译验证过这套代码。**

代码是按 SwiftUI / iOS 16 的标准写的，也做了逐文件的自查（括号闭合、API 可用版本、
资源清单合法性），但**第一次编译仍然可能需要修几个小错误**——这是没法避免的。

如果编译报错，把 Xcode 里的红色报错原文（连文件名和行号）发我，我直接改。
不要自己硬猜，Swift 的报错有时会指着错误的地方。

---

## 前置条件

| 项目 | 要求 |
|---|---|
| 编译环境 | macOS + Xcode 15 或更高 |
| 系统版本 | iOS 16.0 以上 |
| 苹果账号 | 免费 Apple ID 就够自用（签名 7 天有效，需重签） |
| 上架 App Store | 需要苹果开发者账号，99 美元/年 |

---

## 路线 A：你有 Mac

### A-1 用 XcodeGen（推荐，一条命令）

```bash
# 装一次就行
brew install xcodegen

cd pc-buddy/ios
xcodegen generate
open PCBuddy.xcodeproj
```

Xcode 里选中 PCBuddy target → Signing & Capabilities → 把你的 Apple ID 填进 **Team**，
Bundle Identifier 改成一个独一无二的（比如 `com.你的名字.pcbuddy`）。

然后连上 iPhone，点运行。

### A-2 不用 XcodeGen，手动建工程

1. Xcode → File → New → Project → iOS → **App**
2. 参数：Product Name `PCBuddy`，Interface **SwiftUI**，Language **Swift**，
   **取消勾选** Core Data / Tests
3. 把 `ios/PCBuddy/` 里的 **7 个 .swift 文件**拖进工程
   （`PCBuddyApp / Models / Client / Store / PairingView / ChatView / QRScanner / CameraPicker`）
4. 把 `Assets.xcassets` 也拖进去（覆盖自带的）
5. 选中 target → Info 标签页 → 右键 Add Row，加上这几个键：

   | Key | 值 |
   |---|---|
   | `NSAppTransportSecurity` | 字典，下面加两个布尔项 |
   | ↳ `NSAllowsArbitraryLoads` | `YES` |
   | ↳ `NSAllowsLocalNetworking` | `YES` |
   | `NSLocalNetworkUsageDescription` | 需要访问本地网络，才能连上你电脑上的「电脑管家」服务。 |
   | `NSCameraUsageDescription` | 用于扫描电脑上显示的配对二维码，以及拍照发送给电脑。 |
   | `NSPhotoLibraryUsageDescription` | 用于从相册选择照片发送给电脑。 |

6. 如果启动后是黑边、不满屏，去 General 里把 **Launch Screen** 补上（随便选一个）
7. Signing & Capabilities 里填 Team、改 Bundle ID

> 那两个 ATS 键**必须加**。电脑端是局域网明文 HTTP，
> 不加的话所有请求会被 iOS 直接掐断，表现就是一直"连不上电脑"。
> `NSLocalNetworkUsageDescription` 也**必须加**，否则 iOS 14+ 会静默拒绝访问局域网。

---

## 路线 B：你没有 Mac

用 GitHub 的 macOS 机器白嫖编译。仓库里已经放了 workflow：
[`.github/workflows/ios-build.yml`](<D:/DSH/pc-buddy/.github/workflows/ios-build.yml>)

1. 把整个 `pc-buddy` 目录传到一个 GitHub 仓库（公开仓库的 macOS runner 免费）
2. 仓库页 → **Actions** → 左边选 **Build iOS app** → **Run workflow**
3. 等 3~5 分钟，跑完在 **Artifacts** 里下载 `PCBuddy-unsigned-ipa`

拿到的是**未签名**的 ipa，不能直接拖进 iPhone，要用签名工具再过一道。

### 签名安装（不需要 Mac，也不需要越狱）

两个常用工具，都支持用你自己的 Apple ID 免费签名：

- **Sideloadly**（Windows 上有客户端）：<https://sideloadly.io/>
  Windows 上装好 iTunes 和 iCloud，USB 连上 iPhone，把 ipa 拖进去，
  填 Apple ID，点 Start。之后手机上要去
  设置 → 通用 → VPN与设备管理 → 信任这个开发者。
- **AltStore**：手机上装，配合电脑端的 AltServer，可以自动续签（免费签名 7 天过期，
  AltStore 会在同一个 WiFi 下自动帮你重签，省事）

免费 Apple ID 的限制：**7 天有效期**、最多 3 个 App、不能推送通知。
想省事就花 99 美元买个开发者账号，签名一年有效、也能用 TestFlight 分发。

---

## 用起来

1. 电脑上双击 `start.bat`，黑窗口里会出现二维码
2. iPhone 打开 App → 「扫二维码自动填」→ 对准屏幕
3. 地址和口令会自动填好，点「连接」
4. **第一次会弹「是否允许访问本地网络」——必须点允许**，否则永远连不上
5. 然后就能发文字、拍照、看执行过程了

---

## 和网页版的区别

| | 网页版（加到主屏幕） | 原生 App |
|---|---|---|
| 安装难度 | 扫码 + 添加到主屏幕，10 秒 | 要编译签名，第一次比较折腾 |
| 图标 / 全屏 | 有 | 有 |
| 后台通知 | ❌ 没有 | ✅ 任务做完推通知 |
| 扫二维码配对 | 手打 IP 和口令 | ✅ App 内直接扫 |
| 上架 App Store | 不可能 | 可以（需开发者账号） |
| 更新 | 改电脑上文件即可，手机刷新就行 | 要重新编译安装 |

**说实话**：如果你只是想手机遥控电脑，网页版更省事，功能一模一样。
原生 App 的价值在于通知、扫码配对，以及"这是个正经 App"的手感。

---

## 代码结构

```
ios/
├── project.yml                 XcodeGen 工程描述（含 Info.plist 全部键）
├── README-iOS.md               本文件
└── PCBuddy/
    ├── PCBuddyApp.swift        入口 + 通知授权
    ├── Models.swift            数据模型（对齐服务端 JSON）
    ├── Client.swift            网络层：普通请求 + SSE 事件流
    ├── Store.swift             状态中枢：配对、事件处理、发指令
    ├── PairingView.swift       配对页（扫码 / 手填）
    ├── ChatView.swift          聊天页 + 单行渲染 + 设置页
    ├── QRScanner.swift         摄像头扫码（AVFoundation）
    ├── CameraPicker.swift      拍照（UIImagePickerController）
    └── Assets.xcassets/        App 图标 + 主题色
```

**关于 SSE**：用的是 `URLSession.bytes(for:)` + `AsyncLineSequence` 逐行读，
配合 `AsyncThrowingStream`。断线后 `Store` 会自动重连，并从 `lastSeq` 续传，
所以切到后台再回来，执行记录不会丢。
