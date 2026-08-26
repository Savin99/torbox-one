import Foundation

/// Карточка тайтла из мини-API: постер, обложка, описание, состав.
nonisolated struct TitleMeta: Codable, Sendable, Equatable {
    let matched: Bool
    var source: String = ""
    var sourceId: Int = 0
    var title: String = ""
    var originalTitle: String = ""
    var year: String = ""
    var mediaType: String = ""
    var rating: String = ""
    var overview: String = ""
    var tagline: String = ""
    var posterUrl: String = ""
    var backdropUrl: String = ""
    var logoUrl: String = ""
    var genres: [String] = []
    var countries: [String] = []
    var seasonsCount: Int = 0
    var seasonHint: Int?
    var durationMinutes: Int = 0
    var cast: [PersonMeta] = []

    static let unmatched = TitleMeta(matched: false)

    var isSeries: Bool {
        mediaType.uppercased().contains("SERIAL") || mediaType.uppercased().contains("TV")
    }

    var ratingValue: Double? {
        let cleaned = rating.replacingOccurrences(of: ",", with: ".")
        guard !cleaned.isEmpty, cleaned != "null" else { return nil }
        return Double(cleaned)
    }

    /// Постер каталога в полном размере (в списках хватает превью).
    var posterPreviewUrl: String {
        posterUrl.replacingOccurrences(of: "/kp/", with: "/kp_small/")
    }

    /// Строка под заголовком: «2011 · 8 сезонов · драма, фэнтези».
    var metaLine: String {
        var parts: [String] = []
        if !year.isEmpty, year != "null" { parts.append(year) }
        if seasonsCount > 0 {
            parts.append("\(seasonsCount) \(Format.plural(seasonsCount, "сезон", "сезона", "сезонов"))")
        } else if durationMinutes > 0 {
            parts.append(Format.duration(minutes: durationMinutes))
        }
        if !genres.isEmpty { parts.append(genres.prefix(3).joined(separator: ", ")) }
        return parts.joined(separator: " · ")
    }
}

nonisolated struct PersonMeta: Codable, Sendable, Equatable, Identifiable {
    let name: String
    var role: String = ""
    var photoUrl: String = ""
    var profession: String = ""

    var id: String { name + role }

    var isDirector: Bool { profession == "director" }

    /// Подпись под фото: роль актёра или «Режиссёр».
    var subtitle: String {
        if isDirector { return "Режиссёр" }
        return role
    }
}

nonisolated struct EpisodeMeta: Codable, Sendable, Equatable, Identifiable {
    let season: Int
    let episode: Int
    var title: String = ""
    var overview: String = ""
    var airDate: String = ""
    var stillUrl: String = ""

    var id: String { "\(season)x\(episode)" }
}
