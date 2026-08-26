import Foundation
import Observation

@Observable
final class DownloadsViewModel {
    /// Как упорядочить библиотеку.
    enum Sort: String, CaseIterable, Identifiable {
        case added
        case name
        case size

        var id: String { rawValue }

        var title: String {
            switch self {
            case .added: "По дате добавления"
            case .name: "По названию"
            case .size: "По размеру"
            }
        }

        var icon: String {
            switch self {
            case .added: "calendar"
            case .name: "textformat.abc"
            case .size: "externaldrive"
            }
        }
    }

    var sort: Sort = .added

    private(set) var torrents: [TBTorrent] = []
    private(set) var notificationsList: [TBNotification] = []
    private(set) var hasLoaded = false
    private(set) var lastRefresh: Date?
    /// Ошибка последнего обновления — показываем плашкой, а не молча.
    private(set) var loadError: String?
    /// Раздачи, по которым сейчас идёт команда (пауза, реанонс, удаление).
    private(set) var busyIDs: Set<Int> = []

    /// Фильтр по названию: у активных пользователей список на сотни раздач.
    var filter = ""

    private static let seenNotificationKey = "lastSeenNotificationID"

    var anyActive: Bool {
        torrents.contains { TorrentStatus(torrent: $0).isActive }
    }

    var filtered: [TBTorrent] {
        let query = filter.trimmingCharacters(in: .whitespaces)
        let base = query.isEmpty
            ? torrents
            : torrents.filter { ($0.name ?? "").localizedCaseInsensitiveContains(query) }
        switch sort {
        case .added:
            return base
        case .name:
            return base.sorted {
                ($0.name ?? "").localizedStandardCompare($1.name ?? "") == .orderedAscending
            }
        case .size:
            return base.sorted { ($0.size ?? 0) > ($1.size ?? 0) }
        }
    }

    var activeTorrents: [TBTorrent] {
        filtered.filter { !TorrentStatus(torrent: $0).isFinished }
    }

    var readyTorrents: [TBTorrent] {
        filtered.filter { TorrentStatus(torrent: $0).isFinished }
    }

    /// Суммарная скорость всех активных раздач — для сводки сверху.
    var totalSpeed: Int64 {
        torrents.reduce(0) { sum, torrent in
            let status = TorrentStatus(torrent: torrent)
            return status.isActive ? sum + (torrent.downloadSpeed ?? 0) : sum
        }
    }

    var totalSize: Int64 {
        torrents.reduce(0) { $0 + ($1.size ?? 0) }
    }

    var activeCount: Int {
        torrents.filter { TorrentStatus(torrent: $0).isActive }.count
    }

    /// Сколько уведомлений пришло с прошлого визита — бейдж на колокольчике.
    var unreadNotifications: Int {
        let seen = UserDefaults.standard.integer(forKey: Self.seenNotificationKey)
        return notificationsList.filter { $0.id > seen }.count
    }

    func markNotificationsSeen() {
        if let newest = notificationsList.map(\.id).max() {
            UserDefaults.standard.set(newest, forKey: Self.seenNotificationKey)
        }
    }

    func torrent(id: Int) -> TBTorrent? {
        torrents.first { $0.id == id }
    }

    // MARK: - Загрузка

    func refresh(client: TorBoxClient, bypassCache: Bool) async {
        do {
            var list = try await client.myList(bypassCache: bypassCache)
            list.sort { lhs, rhs in
                let lDone = TorrentStatus(torrent: lhs).isFinished
                let rDone = TorrentStatus(torrent: rhs).isFinished
                if lDone != rDone {
                    return !lDone
                }
                return (lhs.createdAt ?? "") > (rhs.createdAt ?? "")
            }
            torrents = list
            loadError = nil
            lastRefresh = Date()
        } catch {
            loadError = error.localizedDescription
        }
        hasLoaded = true
    }

    /// Кто из экранов опрашивает TorBox: побеждает последний открытый,
    /// чтобы две вкладки не удваивали запросы.
    @ObservationIgnored private var pollToken = 0

    /// Живёт внутри .task экрана — SwiftUI сам отменяет при уходе в фон.
    func pollLoop(client: TorBoxClient) async {
        pollToken += 1
        let myToken = pollToken
        while !Task.isCancelled, myToken == pollToken {
            let active = anyActive
            await refresh(client: client, bypassCache: active)
            do {
                try await Task.sleep(for: .seconds(anyActive ? 5 : 30))
            } catch {
                break
            }
        }
    }

    @discardableResult
    func control(client: TorBoxClient, id: Int, operation: TorrentOperation) async -> Bool {
        busyIDs.insert(id)
        defer { busyIDs.remove(id) }
        do {
            try await client.control(id: id, operation: operation)
            await refresh(client: client, bypassCache: true)
            return true
        } catch {
            loadError = error.localizedDescription
            return false
        }
    }

    /// Добавление магнета вручную — из буфера обмена или внешней ссылки.
    func addMagnet(_ magnet: String, client: TorBoxClient) async throws {
        let trimmed = magnet.trimmingCharacters(in: .whitespacesAndNewlines)
        try await client.addMagnet(trimmed, name: Self.magnetName(trimmed))
        await refresh(client: client, bypassCache: true)
    }

    /// Имя из dn-параметра магнета, иначе — короткий хеш.
    static func magnetName(_ magnet: String) -> String {
        guard let components = URLComponents(string: magnet) else { return "magnet" }
        if let name = components.queryItems?.first(where: { $0.name == "dn" })?.value, !name.isEmpty {
            // В dn пробелы кодируют плюсом — иначе имя читается как «Cosmos+Laundromat»
            let spaced = name.replacingOccurrences(of: "+", with: " ")
            return spaced.removingPercentEncoding ?? spaced
        }
        let hash = components.queryItems?.first { $0.name == "xt" }?.value ?? ""
        return hash.replacingOccurrences(of: "urn:btih:", with: "").prefix(12).description
    }

    func loadNotifications(client: TorBoxClient) async {
        do {
            notificationsList = try await client.notifications()
        } catch {
            loadError = error.localizedDescription
        }
    }

    func clearNotifications(client: TorBoxClient) async {
        do {
            try await client.clearNotifications()
            notificationsList = []
        } catch {
            loadError = error.localizedDescription
        }
    }
}
