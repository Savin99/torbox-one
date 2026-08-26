import Foundation

/// Модели мини-API поиска; ключи snake_case конвертируются декодером клиента.
nonisolated struct CatalogItem: Decodable, Sendable, Identifiable, Hashable {
    let catalogId: Int
    let name: String
    let originalName: String
    let mediaType: String
    let year: String
    let rating: String
    let countries: [String]
    let genres: [String]
    let posterUrl: String
    let searchQuery: String

    var id: Int { catalogId }
}

nonisolated struct SearchResponse: Decodable, Sendable {
    let status: [String: String]
    let results: [ReleaseResult]
}

nonisolated struct ReleaseResult: Decodable, Sendable, Identifiable, Hashable {
    let id: String
    let title: String
    let sizeText: String
    let sizeBytes: Int64
    let seeders: Int
    let leechers: Int
    let sourceName: String
    let category: String
    let magnetUrl: String
    let infoHash: String
    let qualitySummary: String
    let voiceoverSummary: String
    let sourceUrl: String

    var magnet: String? { magnetUrl.isEmpty ? nil : magnetUrl }
}

nonisolated struct ReleaseDetails: Decodable, Sendable {
    let detailsText: String
    let qualitySummary: String
    let voiceoverSummary: String
    let audioSummary: String
}

extension ReleaseResult {
    /// Ранг качества для сортировки: 2160p выше 1080p и так далее.
    var qualityRank: Int {
        let text = (qualitySummary + " " + title).lowercased()
        if text.contains("2160") || text.contains("4k") || text.contains("uhd") { return 4 }
        if text.contains("1080") { return 3 }
        if text.contains("720") { return 2 }
        if text.contains("480") || text.contains("dvdrip") { return 1 }
        return 0
    }

    /// Короткая метка разрешения для чипа.
    var resolutionLabel: String? {
        switch qualityRank {
        case 4: "4K"
        case 3: "1080p"
        case 2: "720p"
        case 1: "SD"
        default: nil
        }
    }

    var hasVoiceover: Bool { !voiceoverSummary.isEmpty }

    var sourcePageURL: URL? {
        sourceUrl.isEmpty ? nil : URL(string: sourceUrl)
    }
}
