import CryptoKit
import Foundation

/// Only Infuse's save/download action is exposed. Playback never receives a remote URL.
nonisolated enum InfuseProtocol {
    static let minimumVersion = "8.4.7"
    static let maximumURLBytes = 60_000

    struct Media: Sendable {
        var url: URL
        var filename: String
        var subtitle: URL? = nil
    }
    enum Failure: LocalizedError {
        case invalidMedia, oversizedSelection
        var errorDescription: String? {
            switch self {
            case .invalidMedia: "TorBox вернул неподходящую ссылку на файл."
            case .oversizedSelection: "Выбрано слишком много файлов для одной передачи. Скачай сезон несколькими группами серий."
            }
        }
    }

    static func isWebURL(_ url: URL) -> Bool {
        ["https", "http"].contains(url.scheme?.lowercased() ?? "")
            && url.host?.isEmpty == false && url.user == nil && url.password == nil
    }

    static func digest(_ value: String) -> String {
        SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    static func downloadURL(media: [Media], sessionID: UUID) throws -> URL {
        guard !media.isEmpty else { throw Failure.invalidMedia }
        var pairs: [(String, String)] = []
        for item in media {
            guard isWebURL(item.url) else { throw Failure.invalidMedia }
            pairs.append(("url", item.url.absoluteString))
            pairs.append(("filename", item.filename))
            if let subtitle = item.subtitle {
                guard isWebURL(subtitle) else { throw Failure.invalidMedia }
                pairs.append(("sub", subtitle.absoluteString))
            }
        }
        pairs += [
            ("download", "1"),
            ("x-success", "torboxone://infuse/success?session=\(sessionID.uuidString)"),
            ("x-error", "torboxone://infuse/error?session=\(sessionID.uuidString)"),
        ]
        let unreserved = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        let query = pairs.map { "\($0.0)=\($0.1.addingPercentEncoding(withAllowedCharacters: unreserved)!)" }.joined(separator: "&")
        guard let url = URL(string: "infuse://x-callback-url/save?\(query)") else { throw Failure.invalidMedia }
        guard url.absoluteString.utf8.count <= maximumURLBytes else { throw Failure.oversizedSelection }
        return url
    }

    enum Callback: Equatable {
        /// Acknowledgement only: not evidence that downloads have finished.
        case accepted
        case failed(indices: [Int])
    }

    static func callback(_ url: URL, session: InfuseSession, account: String, now: Date = Date()) -> Callback? {
        guard url.scheme?.lowercased() == "torboxone", url.host == "infuse",
              url.user == nil, url.password == nil, url.port == nil, url.fragment == nil,
              session.acceptedAt == nil, session.account == account, now >= session.createdAt,
              now.timeIntervalSince(session.createdAt) < 7 * 24 * 3600,
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return nil }
        let query = components.queryItems ?? []
        let ids = query.filter { $0.name == "session" }
        guard ids.count == 1, ids[0].value == session.id.uuidString else { return nil }
        if url.path == "/success" { return .accepted }
        guard url.path == "/error" else { return nil }
        let failed = query.filter { $0.name == "failedUrl" }
        var indices: [Int] = []
        for item in failed {
            guard let value = item.value,
                  let index = session.tracks.firstIndex(where: { $0.urlDigest == digest(value) }) else { return nil }
            if !indices.contains(index) { indices.append(index) }
        }
        return .failed(indices: indices.isEmpty ? Array(session.tracks.indices) : indices)
    }
}

/// Persisted before the handoff to survive app termination. Signed URLs and keys are never stored.
nonisolated struct InfuseSession: Codable, Sendable {
    struct Track: Codable, Sendable {
        var item: InfuseDownloadItem
        var urlDigest: String
    }
    var id: UUID
    var account: String
    var createdAt: Date
    var tracks: [Track]
    var acceptedAt: Date? = nil
}

final class InfuseSessionStore {
    private let fileURL: URL
    private(set) var session: InfuseSession?

    init(fileURL: URL? = nil) {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        self.fileURL = fileURL ?? base.appending(path: "infuse-download-session.json")
        if let data = try? Data(contentsOf: self.fileURL) {
            session = try? JSONDecoder().decode(InfuseSession.self, from: data)
        }
    }

    func save(_ value: InfuseSession) throws {
        try FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        let data = try JSONEncoder().encode(value)
        try data.write(to: fileURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        session = value
    }

    func clear() {
        session = nil
        try? FileManager.default.removeItem(at: fileURL)
    }
}
