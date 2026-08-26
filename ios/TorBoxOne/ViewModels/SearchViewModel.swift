import Foundation
import Observation

/// Каталог: популярное и подсказки по названию. Поиск по трекерам живёт
/// отдельно (`TrackerResultsViewModel`), чтобы его можно было отменить.
@Observable
final class SearchViewModel {
    enum Phase: Equatable {
        case idle
        case loading
        case loaded
        case failed(String)
    }

    private(set) var popular: [CatalogItem] = []
    private(set) var popularPhase: Phase = .idle
    private(set) var matches: [CatalogItem] = []
    private(set) var isMatching = false
    /// Каталог не настроен на сервере (нет ключа Кинопоиска) — тогда прячем секцию.
    private(set) var catalogUnavailable = false

    func loadPopular(client: SearchAPIClient, force: Bool = false) async {
        if !force, popularPhase == .loaded || popularPhase == .loading { return }
        popularPhase = .loading
        do {
            popular = try await client.popular()
            popularPhase = .loaded
            catalogUnavailable = false
        } catch {
            if case AppError.searchAPI(let status) = error, status == 503 {
                catalogUnavailable = true
                popularPhase = .loaded
                return
            }
            popularPhase = .failed(error.localizedDescription)
        }
    }

    func catalogSearch(client: SearchAPIClient, query: String) async {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count >= 2 else {
            matches = []
            isMatching = false
            return
        }
        isMatching = true
        defer { isMatching = false }
        // Подсказки — вспомогательные: ошибку здесь не показываем, строка поиска работает всегда
        matches = (try? await client.catalogSearch(trimmed)) ?? []
    }
}
