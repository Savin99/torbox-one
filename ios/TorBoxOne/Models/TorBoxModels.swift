import Foundation

/// Все ответы TorBox завёрнуты в {success, detail, data}; data может отсутствовать даже при success.
nonisolated struct TBEnvelope<T: Decodable & Sendable>: Decodable, Sendable {
    let success: Bool
    let detail: String?
    let data: T?
}

nonisolated struct TBTorrent: Decodable, Sendable, Identifiable {
    let id: Int
    let name: String?
    let size: Int64?
    let progress: Double?
    let downloadState: String?
    let downloadFinished: Bool?
    let cached: Bool?
    let active: Bool?
    let downloadSpeed: Int64?
    let eta: Int?
    let seeds: Int?
    let peers: Int?
    let files: [TBFile]?
    let hash: String?
    let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case id, name, size, progress, cached, active, eta, seeds, peers, files, hash
        case downloadState = "download_state"
        case downloadFinished = "download_finished"
        case downloadSpeed = "download_speed"
        case createdAt = "created_at"
    }
}

nonisolated struct TBFile: Decodable, Sendable, Identifiable {
    let id: Int
    let name: String?
    let shortName: String?
    let size: Int64?
    let mimetype: String?

    enum CodingKeys: String, CodingKey {
        case id, name, size, mimetype
        case shortName = "short_name"
    }

    var displayName: String {
        shortName ?? name?.components(separatedBy: "/").last ?? "файл \(id)"
    }

    var isVideo: Bool {
        if let mimetype, mimetype.hasPrefix("video/") { return true }
        let ext = (displayName as NSString).pathExtension.lowercased()
        return ["mkv", "mp4", "avi", "m4v", "mov", "ts", "webm"].contains(ext)
    }
}

nonisolated struct TBNotification: Decodable, Sendable, Identifiable {
    let id: Int
    let title: String?
    let message: String?
    let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case id, title, message
        case createdAt = "created_at"
    }
}
