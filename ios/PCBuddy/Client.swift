import Foundation

enum ClientError: LocalizedError {
    case badAddress
    case notPaired
    case http(Int, String)
    case offline

    var errorDescription: String? {
        switch self {
        case .badAddress:
            return "服务器地址不对，检查一下 IP 和端口"
        case .notPaired:
            return "还没配对，先扫码或手动填地址"
        case .http(let code, let body):
            if code == 403 { return "口令不对，用电脑上显示的完整网址里的那串" }
            return "服务器返回 \(code)：\(body.prefix(160))"
        case .offline:
            return "连不上电脑，确认在同一个 WiFi、电脑上服务开着、防火墙已放行"
        }
    }
}

/// 和电脑端服务说话的唯一入口。
final class PCBuddyClient {
    let config: ServerConfig

    init(_ config: ServerConfig) {
        self.config = config
    }

    // MARK: URL 组装

    private func url(_ path: String, query: [URLQueryItem] = []) -> URL? {
        guard var c = URLComponents(string: config.base) else { return nil }
        var basePath = c.path
        if basePath.hasSuffix("/") { basePath.removeLast() }
        c.path = basePath + path
        c.queryItems = [URLQueryItem(name: "t", value: config.token)] + query
        return c.url
    }

    /// 把服务端给的 /file/xxx 变成可以给 AsyncImage 用的完整地址
    func fileURL(_ serverPath: String) -> URL? {
        url(serverPath)
    }

    // MARK: 底层请求

    private func makeRequest(_ path: String,
                             query: [URLQueryItem] = [],
                             method: String = "GET",
                             body: Data? = nil,
                             timeout: TimeInterval = 30) throws -> URLRequest {
        guard let u = url(path, query: query) else { throw ClientError.badAddress }
        var r = URLRequest(url: u)
        r.httpMethod = method
        r.timeoutInterval = timeout
        if let body {
            r.httpBody = body
            r.setValue("application/json; charset=utf-8", forHTTPHeaderField: "Content-Type")
        }
        return r
    }

    private func run<T: Decodable>(_ req: URLRequest, as type: T.Type) async throws -> T {
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            guard let http = resp as? HTTPURLResponse else { throw ClientError.offline }
            guard (200..<300).contains(http.statusCode) else {
                throw ClientError.http(http.statusCode, String(data: data, encoding: .utf8) ?? "")
            }
            return try JSONDecoder().decode(T.self, from: data)
        } catch let e as ClientError {
            throw e
        } catch let e as DecodingError {
            throw e
        } catch {
            throw ClientError.offline
        }
    }

    // MARK: 接口

    func info() async throws -> ServerInfo {
        try await run(makeRequest("/api/info"), as: ServerInfo.self)
    }

    @discardableResult
    func send(text: String, images: [ImagePayload]) async throws -> Ack {
        let payload = SendBody(text: text, images: images)
        let body = try JSONEncoder().encode(payload)
        return try await run(makeRequest("/api/send", method: "POST", body: body), as: Ack.self)
    }

    @discardableResult
    func stop() async throws -> Ack {
        try await run(makeRequest("/api/stop", method: "POST", body: Data("{}".utf8)), as: Ack.self)
    }

    @discardableResult
    func confirm(id: String, allow: Bool) async throws -> Ack {
        let body = try JSONEncoder().encode(ConfirmBody(id: id, allow: allow))
        return try await run(makeRequest("/api/confirm", method: "POST", body: body), as: Ack.self)
    }

    /// 实时事件流。断线由外层负责重连。
    func events(since: Int) -> AsyncThrowingStream<BuddyEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let req = try makeRequest("/api/events",
                                              query: [URLQueryItem(name: "since", value: String(since))],
                                              timeout: 3600)
                    let (bytes, resp) = try await URLSession.shared.bytes(for: req)
                    guard let http = resp as? HTTPURLResponse else { throw ClientError.offline }
                    guard http.statusCode == 200 else {
                        throw ClientError.http(http.statusCode, "事件流建立失败")
                    }
                    for try await line in bytes.lines {
                        if Task.isCancelled { break }
                        guard line.hasPrefix("data: ") else { continue }
                        let payload = String(line.dropFirst(6))
                        guard let d = payload.data(using: .utf8) else { continue }
                        if let ev = try? JSONDecoder().decode(BuddyEvent.self, from: d) {
                            continuation.yield(ev)
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}
