import Foundation
import UIKit
import UserNotifications

/// 聊天界面里的一行
struct ChatItem: Identifiable {
    enum Kind {
        case user
        case assistant
        case action     // 调用了什么工具
        case result     // 工具回执
        case system     // 提示 / 警告
        case image      // 电脑截图
    }

    let id = UUID()
    var kind: Kind
    var text: String = ""
    var name: String = ""
    var ok: Bool = true
    var images: [UIImage] = []
    var imageURL: URL?
}

@MainActor
final class Store: ObservableObject {
    @Published var config: ServerConfig?
    @Published var info: ServerInfo?
    @Published var items: [ChatItem] = []
    @Published var busy = false
    @Published var status = "未连接"
    @Published var pendingConfirm: BuddyEvent?
    @Published var lastError: String?

    private var lastSeq = 0
    private var streamTask: Task<Void, Never>?
    private let storageKey = "pcbuddy.config.v1"

    private var client: PCBuddyClient? {
        guard let config else { return nil }
        return PCBuddyClient(config)
    }

    init() {
        load()
        if config != nil {
            Task { await refreshInfo() }
            connect()
        }
    }

    // MARK: - 配对

    private func load() {
        guard let data = UserDefaults.standard.data(forKey: storageKey) else { return }
        config = try? JSONDecoder().decode(ServerConfig.self, from: data)
    }

    private func save() {
        guard let config, let data = try? JSONEncoder().encode(config) else { return }
        UserDefaults.standard.set(data, forKey: storageKey)
    }

    /// 配对并验证。base 可以只写 IP，会自动补 http://
    func pair(base rawBase: String, token rawToken: String) async throws {
        var base = rawBase.trimmingCharacters(in: .whitespacesAndNewlines)
        let token = rawToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !base.isEmpty, !token.isEmpty else { throw ClientError.badAddress }

        if !base.lowercased().hasPrefix("http://") && !base.lowercased().hasPrefix("https://") {
            base = "http://" + base
        }
        // 只给了 IP 就补默认端口
        if let c = URLComponents(string: base), c.port == nil {
            base += ":8765"
        }
        while base.hasSuffix("/") { base.removeLast() }

        let candidate = ServerConfig(base: base, token: token)
        let client = PCBuddyClient(candidate)
        let fetched = try await client.info()   // 不通就在这里抛错，不会存下错误配置

        self.config = candidate
        self.info = fetched
        lastSeq = 0
        items.removeAll()
        save()
        connect()
    }

    func unpair() {
        streamTask?.cancel()
        streamTask = nil
        config = nil
        info = nil
        items.removeAll()
        busy = false
        pendingConfirm = nil
        lastSeq = 0
        status = "未连接"
        UserDefaults.standard.removeObject(forKey: storageKey)
    }

    func refreshInfo() async {
        guard let client else { return }
        info = try? await client.info()
    }

    // MARK: - 实时连接

    func connect() {
        streamTask?.cancel()
        guard config != nil else { return }
        streamTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                do {
                    self.status = "已连接"
                    try await self.consume()
                } catch {
                    if Task.isCancelled { return }
                    self.status = "断线重连中…"
                    self.lastError = error.localizedDescription
                }
                if Task.isCancelled { return }
                try? await Task.sleep(nanoseconds: 1_500_000_000)
            }
        }
    }

    private func consume() async throws {
        guard let client else { throw ClientError.notPaired }
        for try await ev in client.events(since: lastSeq) {
            handle(ev)
        }
    }

    // MARK: - 事件处理

    private func handle(_ ev: BuddyEvent) {
        if let s = ev.seq { lastSeq = max(lastSeq, s) }

        switch ev.type {
        case "task_start":
            busy = true
            items.append(ChatItem(kind: .user,
                                  text: ev.text ?? "（图片）"))

        case "thinking", "step":
            status = ev.text ?? "执行中…"

        case "action":
            items.append(ChatItem(kind: .action,
                                  text: ev.detail ?? "",
                                  name: ev.name ?? ""))

        case "result":
            items.append(ChatItem(kind: .result,
                                  text: ev.brief ?? "",
                                  ok: ev.ok ?? true))

        case "image":
            if let p = ev.url, let full = client?.fileURL(p) {
                items.append(ChatItem(kind: .image, imageURL: full))
            }

        case "warn":
            items.append(ChatItem(kind: .system, text: "⚠️ " + (ev.text ?? "")))

        case "final":
            items.append(ChatItem(kind: .assistant, text: ev.text ?? ""))

        case "task_end":
            busy = false
            status = "已连接"
            if ev.ok == false {
                items.append(ChatItem(kind: .system, text: "✕ " + (ev.text ?? "出错了")))
            }
            notifyIfNeeded(ev)

        case "confirm_request":
            pendingConfirm = ev

        case "confirm_done":
            pendingConfirm = nil
            items.append(ChatItem(kind: .system,
                                  text: (ev.allow ?? false) ? "✅ 已允许这次操作" : "🚫 已拒绝这次操作"))

        default:
            break
        }
    }

    /// App 在后台时，用系统通知告诉你任务做完了 —— 这是原生 App 才有的好处
    private func notifyIfNeeded(_ ev: BuddyEvent) {
        guard UIApplication.shared.applicationState != .active else { return }
        let content = UNMutableNotificationContent()
        content.title = (ev.ok == false) ? "任务结束（有错误）" : "任务完成"
        content.body = String((ev.text ?? "").prefix(120))
        content.sound = .default
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        )
    }

    // MARK: - 发指令

    func send(text: String, images: [UIImage]) {
        guard let client else { return }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty && images.isEmpty { return }

        let payloads = images.compactMap { ImageTool.payload(from: $0) }
        items.append(ChatItem(kind: .user,
                              text: trimmed.isEmpty ? "（图片）" : trimmed,
                              images: images))
        busy = true

        Task {
            do {
                try await client.send(text: trimmed, images: payloads)
            } catch {
                busy = false
                items.append(ChatItem(kind: .system, text: "发送失败：\(error.localizedDescription)"))
            }
        }
    }

    func stop() {
        guard let client else { return }
        Task {
            try? await client.stop()
            busy = false
            pendingConfirm = nil
        }
    }

    func resolveConfirm(_ allow: Bool) {
        guard let client, let ev = pendingConfirm, let cid = ev.id else { return }
        pendingConfirm = nil
        Task { try? await client.confirm(id: cid, allow: allow) }
    }
}

// MARK: - 图片压缩

enum ImageTool {
    /// 压到最长边 1600、JPEG 0.85 —— 和网页版保持一致，别把流量浪费在原图上。
    static func payload(from image: UIImage, maxDimension: CGFloat = 1600) -> ImagePayload? {
        let size = image.size
        guard size.width > 1, size.height > 1 else { return nil }

        let scale = min(1, maxDimension / max(size.width, size.height))
        let target = CGSize(width: (size.width * scale).rounded(),
                            height: (size.height * scale).rounded())

        let renderer = UIGraphicsImageRenderer(size: target)
        let resized = renderer.image { _ in
            image.draw(in: CGRect(origin: .zero, size: target))
        }
        guard let data = resized.jpegData(compressionQuality: 0.85) else { return nil }
        return ImagePayload(name: "phone.jpg", data: data.base64EncodedString())
    }
}
