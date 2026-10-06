import Foundation

// MARK: - 服务器信息

/// GET /api/info 的返回
struct ServerInfo: Codable {
    var model: String?
    var hasKey: Bool?
    var key: String?
    var workDir: String?
    var busy: Bool?
    var vision: Bool?
    var lanIp: String?
    var port: Int?

    enum CodingKeys: String, CodingKey {
        case model, key, busy, vision, port
        case hasKey = "has_key"
        case workDir = "work_dir"
        case lanIp = "lan_ip"
    }
}

// MARK: - 配对信息

struct ServerConfig: Codable, Equatable {
    /// 形如 http://192.168.1.100:8765
    var base: String
    /// 访问口令
    var token: String

    var displayBase: String {
        base.replacingOccurrences(of: "http://", with: "")
    }
}

// MARK: - 事件

/// 服务端 SSE 推来的事件。字段全部可选，方便服务端以后加字段不会把解析搞崩。
struct BuddyEvent: Codable {
    var seq: Int?
    var ts: String?
    var type: String
    var text: String?
    var name: String?
    var detail: String?
    var brief: String?
    var ok: Bool?
    var n: Int?
    var url: String?
    var images: [String]?
    var id: String?
    var kind: String?
    var allow: Bool?
    var elapsed: Double?
    var timeout: Int?
    var reason: String?
}

// MARK: - 请求体

struct ImagePayload: Codable {
    var name: String
    /// 纯 base64（不带 data: 前缀），服务端两种都认
    var data: String
}

struct SendBody: Codable {
    var text: String
    var images: [ImagePayload]
}

struct ConfirmBody: Codable {
    var id: String
    var allow: Bool
}

struct Ack: Codable {
    var ok: Bool?
    var error: String?
    var queued: Int?
    var images: Int?
}
