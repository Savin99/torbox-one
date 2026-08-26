import Foundation
import Observation

/// Память просмотра: на каком файле (серии) и времени остановился. Переживает
/// перезапуск приложения и долгие паузы — JSON в Application Support.
@Observable
final class PlaybackStore {
    struct Entry: Codable, Identifiable, Equatable {
        var key: String
        var torrentID: Int
        var fileID: Int
        var torrentName: String
        var fileName: String
        var positionSeconds: Double
        var durationSeconds: Double
        var updatedAt: Date
        /// Отмечено вручную: длительность может быть неизвестна, если серию не открывали.
        var watchedManually: Bool? = nil

        var id: String { key }

        var fraction: Double {
            durationSeconds > 0 ? min(positionSeconds / durationSeconds, 1) : 0
        }

        /// ≥95% или отмечено вручную
        var isWatched: Bool {
            if watchedManually == true { return true }
            return durationSeconds > 0 && positionSeconds >= durationSeconds * 0.95
        }

        /// Есть смысл продолжать: смотрел больше 30 с и не досмотрел
        var isResumable: Bool {
            positionSeconds > 30 && !isWatched
        }

        /// Хоть что-то из серии посмотрели.
        var isStarted: Bool {
            positionSeconds > 30 || isWatched
        }

        /// Сколько осталось до конца — для подписи в карусели.
        var remainingSeconds: Double {
            max(0, durationSeconds - positionSeconds)
        }
    }

    private(set) var entries: [String: Entry] = [:]

    private static let fileURL: URL = {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appending(path: "playback.json")
    }()

    init() {
        if let data = try? Data(contentsOf: Self.fileURL),
           let decoded = try? JSONDecoder().decode([String: Entry].self, from: data) {
            entries = decoded
        }
    }

    /// Стабильный ключ: hash торрента живёт дольше, чем его id в аккаунте.
    static func key(torrent: TBTorrent, fileID: Int) -> String {
        let base = torrent.hash?.lowercased()
        return "\(base ?? "id-\(torrent.id)"):\(fileID)"
    }

    func entry(torrent: TBTorrent, fileID: Int) -> Entry? {
        entries[Self.key(torrent: torrent, fileID: fileID)]
    }

    /// Позиция для автопродолжения (nil — начать с начала).
    func resumePosition(torrent: TBTorrent, fileID: Int) -> Double? {
        guard let entry = entry(torrent: torrent, fileID: fileID), entry.isResumable else { return nil }
        return entry.positionSeconds
    }

    func update(
        torrent: TBTorrent, file: TBFile,
        positionSeconds: Double, durationSeconds: Double
    ) {
        let key = Self.key(torrent: torrent, fileID: file.id)
        entries[key] = Entry(
            key: key,
            torrentID: torrent.id,
            fileID: file.id,
            torrentName: torrent.name ?? "Без названия",
            fileName: file.displayName,
            positionSeconds: positionSeconds,
            durationSeconds: durationSeconds,
            updatedAt: Date()
        )
        save()
    }

    /// Обновление целой записи — для локального воспроизведения без объекта торрента.
    func upsert(_ entry: Entry) {
        entries[entry.key] = entry
        save()
    }

    /// Отметить серию просмотренной, не открывая её.
    func markWatched(torrent: TBTorrent, file: TBFile) {
        let key = Self.key(torrent: torrent, fileID: file.id)
        var entry = entries[key] ?? Entry(
            key: key,
            torrentID: torrent.id,
            fileID: file.id,
            torrentName: torrent.name ?? "Без названия",
            fileName: file.displayName,
            positionSeconds: 0,
            durationSeconds: 0,
            updatedAt: Date()
        )
        entry.watchedManually = true
        entry.updatedAt = Date()
        entries[key] = entry
        save()
    }

    /// Забыть прогресс: серия снова считается непросмотренной.
    func resetProgress(torrent: TBTorrent, fileID: Int) {
        entries.removeValue(forKey: Self.key(torrent: torrent, fileID: fileID))
        save()
    }

    /// Всё ли видео раздачи просмотрено — для отметки в библиотеке.
    func progress(torrent: TBTorrent, files: [TBFile]) -> (watched: Int, total: Int, resumable: Entry?) {
        guard !files.isEmpty else { return (0, 0, nil) }
        var watched = 0
        var resumable: Entry?
        for file in files {
            guard let entry = entry(torrent: torrent, fileID: file.id) else { continue }
            if entry.isWatched { watched += 1 }
            if entry.isResumable, resumable == nil || entry.updatedAt > resumable!.updatedAt {
                resumable = entry
            }
        }
        return (watched, files.count, resumable)
    }

    func remove(key: String) {
        entries.removeValue(forKey: key)
        save()
    }

    /// «Продолжить просмотр»: последние незаконченные, свежие сверху.
    var continueWatching: [Entry] {
        entries.values
            .filter(\.isResumable)
            .sorted { $0.updatedAt > $1.updatedAt }
            .prefix(10)
            .map { $0 }
    }

    private func save() {
        if let data = try? JSONEncoder().encode(entries) {
            try? data.write(to: Self.fileURL, options: .atomic)
        }
    }
}
