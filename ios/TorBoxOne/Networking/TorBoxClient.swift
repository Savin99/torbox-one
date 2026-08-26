import Foundation

enum TorrentOperation: String {
    case delete
    case resume
    case reannounce
    case stopSeeding = "stop_seeding"
}

/// Прямой клиент TorBox API. Ключ не покидает приложение.
struct TorBoxClient {
    let apiKey: String

    private static let base = "https://api.torbox.app/v1/api"

    private struct Head: Decodable {
        let success: Bool
        let detail: String?
    }

    // MARK: - Публичные методы

    func myList(bypassCache: Bool = false) async throws -> [TBTorrent] {
        let data = try await send(request(path: "torrents/mylist", query: [
            .init(name: "limit", value: "1000"),
            .init(name: "offset", value: "0"),
            .init(name: "bypass_cache", value: bypassCache ? "true" : "false"),
        ]))
        let decoder = JSONDecoder()
        // data бывает списком, одиночным объектом или null — как в боте
        if let env = try? decoder.decode(TBEnvelope<[TBTorrent]>.self, from: data) {
            return env.data ?? []
        }
        if let env = try? decoder.decode(TBEnvelope<TBTorrent>.self, from: data) {
            return env.data.map { [$0] } ?? []
        }
        return []
    }

    func addMagnet(_ magnet: String, name: String) async throws {
        var req = request(path: "torrents/createtorrent")
        req.httpMethod = "POST"
        req.timeoutInterval = 60
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        req.httpBody = Self.formEncode([("magnet", magnet), ("name", String(name.prefix(200)))])
        try await send(req)
    }

    func addTorrentFile(_ fileData: Data, name: String) async throws {
        let boundary = "torboxone-\(UUID().uuidString)"
        var body = Data()
        body.append(text: "--\(boundary)\r\n")
        body.append(text: "Content-Disposition: form-data; name=\"file\"; filename=\"release.torrent\"\r\n")
        body.append(text: "Content-Type: application/x-bittorrent\r\n\r\n")
        body.append(fileData)
        body.append(text: "\r\n--\(boundary)\r\n")
        body.append(text: "Content-Disposition: form-data; name=\"name\"\r\n\r\n")
        body.append(text: String(name.prefix(200)))
        body.append(text: "\r\n--\(boundary)--\r\n")

        var req = request(path: "torrents/createtorrent")
        req.httpMethod = "POST"
        req.timeoutInterval = 60
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await URLSession.shared.upload(for: req, from: body)
        try Self.checkHead(data: data, response: response)
    }

    func control(id: Int, operation: TorrentOperation) async throws {
        var req = request(path: "torrents/controltorrent")
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(ControlBody(torrentId: id, operation: operation.rawValue))
        try await send(req)
    }

    /// CDN-ссылка на файл: redirect=false, чтобы в ответе пришёл прямой URL без API-ключа.
    func downloadLink(torrentID: Int, fileID: Int) async throws -> URL {
        let data = try await send(request(path: "torrents/requestdl", query: [
            .init(name: "token", value: apiKey),
            .init(name: "torrent_id", value: String(torrentID)),
            .init(name: "file_id", value: String(fileID)),
            .init(name: "redirect", value: "false"),
        ]))
        guard let env = try? JSONDecoder().decode(TBEnvelope<String>.self, from: data),
              let link = env.data, let url = URL(string: link) else {
            throw AppError.badResponse
        }
        return url
    }

    func checkCached(hashes: [String]) async throws -> Set<String> {
        let cleaned = hashes.filter { !$0.isEmpty }.map { $0.lowercased() }
        guard !cleaned.isEmpty else { return [] }
        let data = try await send(request(path: "torrents/checkcached", query: [
            .init(name: "hash", value: cleaned.joined(separator: ",")),
            .init(name: "format", value: "object"),
            .init(name: "list_files", value: "false"),
        ]))
        guard let env = try? JSONDecoder().decode(TBEnvelope<[String: TBAnyValue]>.self, from: data) else {
            return []
        }
        return Set((env.data ?? [:]).keys.map { $0.lowercased() })
    }

    func notifications() async throws -> [TBNotification] {
        let data = try await send(request(path: "notifications/mynotifications"))
        let env = try? JSONDecoder().decode(TBEnvelope<[TBNotification]>.self, from: data)
        return env?.data ?? []
    }

    func clearNotifications() async throws {
        var req = request(path: "notifications/clear")
        req.httpMethod = "POST"
        try await send(req)
    }

    // MARK: - Транспорт

    private struct ControlBody: Encodable {
        let torrentId: Int
        let operation: String
        let all = false

        enum CodingKeys: String, CodingKey {
            case operation, all
            case torrentId = "torrent_id"
        }
    }

    private func request(path: String, query: [URLQueryItem] = []) -> URLRequest {
        var comps = URLComponents(string: "\(Self.base)/\(path)")!
        if !query.isEmpty {
            comps.queryItems = query
        }
        var req = URLRequest(url: comps.url!)
        req.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        req.setValue("TorBoxOne/1.0 (iOS)", forHTTPHeaderField: "User-Agent")
        return req
    }

    @discardableResult
    private func send(_ req: URLRequest) async throws -> Data {
        let (data, response) = try await URLSession.shared.data(for: req)
        try Self.checkHead(data: data, response: response)
        return data
    }

    /// Обёртка TorBox важнее HTTP-статуса: бывает 404 с success:true (пустой список).
    private static func checkHead(data: Data, response: URLResponse) throws {
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard let head = try? JSONDecoder().decode(Head.self, from: data) else {
            throw AppError.torBox(detail: "HTTP \(status)")
        }
        guard head.success else {
            throw AppError.torBox(detail: head.detail ?? "HTTP \(status)")
        }
    }

    private static func formEncode(_ pairs: [(String, String)]) -> Data {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        let encoded = pairs.map { key, value in
            "\(key)=\(value.addingPercentEncoding(withAllowedCharacters: allowed) ?? "")"
        }
        return Data(encoded.joined(separator: "&").utf8)
    }
}

/// Значение произвольной формы в ответе checkcached — важно только наличие ключа.
nonisolated struct TBAnyValue: Decodable, Sendable {
    init(from decoder: any Decoder) throws {}
}

private extension Data {
    mutating func append(text: String) {
        append(Data(text.utf8))
    }
}
