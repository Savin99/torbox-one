import Foundation
import Observation

/// Кэш метаданных: постеры, обложки, состав и названия серий.
/// Диск нужен, чтобы постеры были на месте сразу после запуска и без сети.
@Observable
final class MetaStore {
    private(set) var titles: [String: TitleMeta] = [:]
    private(set) var episodes: [Int: [EpisodeMeta]] = [:]

    /// Ключи, по которым запрос уже летит — чтобы не дублировать.
    @ObservationIgnored private var inFlightTitles: Set<String> = []
    @ObservationIgnored private var inFlightEpisodes: Set<Int> = []

    private static let fileURL: URL = {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appending(path: "meta.json")
    }()

    private struct Snapshot: Codable {
        var titles: [String: TitleMeta]
        var episodes: [Int: [EpisodeMeta]]
    }

    init() {
        if let data = try? Data(contentsOf: Self.fileURL),
           let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data) {
            titles = snapshot.titles
            episodes = snapshot.episodes
        }
    }

    // MARK: - Чтение

    static func key(_ name: String?) -> String {
        (name ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }

    /// Карточка тайтла, если она уже загружена и что-то нашлось.
    func meta(for name: String?) -> TitleMeta? {
        guard let meta = titles[Self.key(name)], meta.matched else { return nil }
        return meta
    }

    /// Известно ли, что по этому имени в каталоге ничего нет.
    func isUnmatched(_ name: String?) -> Bool {
        titles[Self.key(name)]?.matched == false
    }

    func episode(titleID: Int, season: Int?, number: Int) -> EpisodeMeta? {
        guard let list = episodes[titleID] else { return nil }
        if let season {
            return list.first { $0.season == season && $0.episode == number }
        }
        // Без сезона имеет смысл искать только если серия одна на весь список
        let matches = list.filter { $0.episode == number }
        return matches.count == 1 ? matches[0] : nil
    }

    // MARK: - Загрузка

    func load(name: String?, client: SearchAPIClient?) async {
        let key = Self.key(name)
        guard let client, let name, key.count >= 2 else { return }
        guard titles[key] == nil, !inFlightTitles.contains(key) else { return }
        inFlightTitles.insert(key)
        defer { inFlightTitles.remove(key) }
        do {
            let meta = try await client.titleMeta(name: name)
            titles[key] = meta
            save()
        } catch {
            // Отсутствие метаданных — не ошибка для пользователя: интерфейс живёт без них
        }
    }

    func loadEpisodes(titleID: Int, client: SearchAPIClient?) async {
        guard let client, titleID > 0 else { return }
        guard episodes[titleID] == nil, !inFlightEpisodes.contains(titleID) else { return }
        inFlightEpisodes.insert(titleID)
        defer { inFlightEpisodes.remove(titleID) }
        do {
            let list = try await client.episodes(titleID: titleID, season: nil)
            episodes[titleID] = list
            save()
        } catch {
            // Молча: названия серий — украшение, без них список файлов работает
        }
    }

    /// Полный сброс кэша — из настроек, если метаданные подобрались неверно.
    func clear() {
        titles = [:]
        episodes = [:]
        try? FileManager.default.removeItem(at: Self.fileURL)
    }

    /// Забыть одну карточку, чтобы перезапросить её заново.
    func forget(name: String?) {
        let key = Self.key(name)
        if let meta = titles.removeValue(forKey: key), meta.sourceId > 0 {
            episodes.removeValue(forKey: meta.sourceId)
        }
        save()
    }

    var storedTitles: Int { titles.values.filter(\.matched).count }

    private func save() {
        let snapshot = Snapshot(titles: titles, episodes: episodes)
        if let data = try? JSONEncoder().encode(snapshot) {
            try? data.write(to: Self.fileURL, options: .atomic)
        }
    }
}
