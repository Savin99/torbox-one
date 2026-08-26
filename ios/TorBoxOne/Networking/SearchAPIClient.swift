import Foundation

/// Клиент мини-API поиска (FastAPI на Mac/VPS).
struct SearchAPIClient {
    let baseURL: URL
    let token: String

    func health() async throws {
        _ = try await get("health")
    }

    func popular() async throws -> [CatalogItem] {
        try decode([CatalogItem].self, from: try await get("catalog/popular"))
    }

    func catalogSearch(_ query: String) async throws -> [CatalogItem] {
        try decode([CatalogItem].self, from: try await get("catalog/search", query: [.init(name: "q", value: query)]))
    }

    /// Трекеры отвечают медленно — таймаут с запасом.
    func trackerSearch(_ query: String) async throws -> SearchResponse {
        try decode(
            SearchResponse.self,
            from: try await get("search", query: [.init(name: "q", value: query)], timeout: 90)
        )
    }

    /// Карточка тайтла по имени раздачи: постер, обложка, состав.
    func titleMeta(name: String) async throws -> TitleMeta {
        try decode(
            TitleMeta.self,
            from: try await get("meta/match", query: [.init(name: "name", value: name)], timeout: 45)
        )
    }

    /// Серии сезона: русские названия, описания и кадры.
    func episodes(titleID: Int, season: Int?) async throws -> [EpisodeMeta] {
        var query: [URLQueryItem] = []
        if let season {
            query.append(.init(name: "season", value: String(season)))
        }
        return try decode(
            [EpisodeMeta].self,
            from: try await get("meta/\(titleID)/episodes", query: query, timeout: 45)
        )
    }

    func releaseDetails(id: String) async throws -> ReleaseDetails {
        try decode(ReleaseDetails.self, from: try await get("release/\(id)/details", timeout: 60))
    }

    func torrentBytes(id: String) async throws -> Data {
        try await get("release/\(id)/torrent", timeout: 60)
    }

    // MARK: - Транспорт

    private func get(_ path: String, query: [URLQueryItem] = [], timeout: TimeInterval = 30) async throws -> Data {
        var comps = URLComponents(url: baseURL.appending(path: path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty {
            comps.queryItems = query
        }
        var req = URLRequest(url: comps.url!)
        req.timeoutInterval = timeout
        if !token.isEmpty {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        let (data, response) = try await URLSession.shared.data(for: req)
        guard let http = response as? HTTPURLResponse else {
            throw AppError.badResponse
        }
        guard (200...299).contains(http.statusCode) else {
            throw AppError.searchAPI(status: http.statusCode)
        }
        return data
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(type, from: data)
    }
}
