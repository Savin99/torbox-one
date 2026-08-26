import Foundation
import Observation

/// Загрузки в память телефона: фоновая URLSession, реестр в JSON,
/// файлы в Documents/Фильмы (видны в приложении «Файлы»).
@Observable
final class DownloadManager: NSObject {
    struct Item: Codable, Identifiable, Equatable {
        enum State: Codable, Equatable {
            case downloading
            case paused
            case done
            case failed(String)
        }

        var key: String
        var torrentID: Int
        var fileID: Int
        var torrentName: String
        var fileName: String
        var totalBytes: Int64
        var relativePath: String
        var state: State

        var id: String { key }
    }

    private(set) var items: [String: Item] = [:]
    /// Живой прогресс 0…1 по ключу; не персистится.
    private(set) var progress: [String: Double] = [:]

    private var tasks: [String: URLSessionDownloadTask] = [:]
    private var resumeDatas: [String: Data] = [:]
    private var refreshers: [String: () async throws -> URL] = [:]
    private var didRetry: Set<String> = []
    private var lastProgressUpdate: [String: Date] = [:]

    private static let registryURL: URL = {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appending(path: "downloads.json")
    }()

    private static let documentsURL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]

    @ObservationIgnored private lazy var session: URLSession = {
        let config = URLSessionConfiguration.background(withIdentifier: "com.ilyaqa.torboxone.downloads")
        config.isDiscretionary = false
        config.sessionSendsLaunchEvents = false
        // Колбэки — на main: весь менеджер живёт на MainActor
        return URLSession(configuration: config, delegate: self, delegateQueue: .main)
    }()

    override init() {
        super.init()
        if let data = try? Data(contentsOf: Self.registryURL),
           let decoded = try? JSONDecoder().decode([String: Item].self, from: data) {
            items = decoded
            // После перезапуска активных задач нет — недокачанное переводим в паузу
            for (key, item) in items where item.state == .downloading {
                items[key]?.state = .paused
            }
            // Файл могли удалить через «Файлы» — не показываем мёртвые записи
            for (key, item) in items where item.state == .done && !FileManager.default.fileExists(
                atPath: Self.documentsURL.appending(path: item.relativePath).path
            ) {
                items.removeValue(forKey: key)
            }
        }
    }

    // MARK: - Публичный API

    func item(key: String) -> Item? {
        items[key]
    }

    /// Локальный файл, если полностью скачан.
    func localURL(key: String) -> URL? {
        guard let item = items[key], item.state == .done else { return nil }
        let url = Self.documentsURL.appending(path: item.relativePath)
        return FileManager.default.fileExists(atPath: url.path) ? url : nil
    }

    var doneItems: [Item] {
        items.values.filter { $0.state == .done }.sorted { $0.torrentName < $1.torrentName }
    }

    var activeItems: [Item] {
        items.values.filter { $0.state != .done }.sorted { $0.torrentName < $1.torrentName }
    }

    var totalBytesUsed: Int64 {
        doneItems.reduce(0) { $0 + $1.totalBytes }
    }

    static var freeBytes: Int64 {
        let values = try? documentsURL.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        return Int64(values?.volumeAvailableCapacityForImportantUsage ?? 0)
    }

    /// Запускает (или продолжает) загрузку файла. refresh — способ получить свежую CDN-ссылку.
    /// Возвращает false, если места на телефоне не хватает — вызывающий покажет тост.
    @discardableResult
    func ensureDownloading(
        torrent: TBTorrent, file: TBFile,
        url: URL, refresh: @escaping () async throws -> URL
    ) -> Bool {
        let key = PlaybackStore.key(torrent: torrent, fileID: file.id)
        refreshers[key] = refresh
        if let existing = items[key], existing.state == .done || existing.state == .downloading {
            return true
        }
        let size = file.size ?? 0
        guard Self.freeBytes > size + 500_000_000 else {
            items[key] = makeItem(key: key, torrent: torrent, file: file, state: .failed("Мало места на телефоне"))
            saveRegistry()
            return false
        }
        if items[key] == nil {
            items[key] = makeItem(key: key, torrent: torrent, file: file, state: .downloading)
        } else {
            items[key]?.state = .downloading
        }
        saveRegistry()
        startTask(key: key, url: url)
        return true
    }

    func pause(key: String) {
        guard let task = tasks[key] else { return }
        tasks.removeValue(forKey: key)
        task.cancel { [weak self] data in
            // delegateQueue = main, но замыкание cancel приходит на произвольной очереди
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.resumeDatas[key] = data
                    self.items[key]?.state = .paused
                    self.saveRegistry()
                }
            }
        }
    }

    /// Продолжение: resumeData, если есть, иначе свежая ссылка.
    func resume(key: String, client: TorBoxClient?) {
        guard let item = items[key], item.state != .done else { return }
        items[key]?.state = .downloading
        if let data = resumeDatas.removeValue(forKey: key) {
            let task = session.downloadTask(withResumeData: data)
            task.taskDescription = key
            tasks[key] = task
            task.resume()
            return
        }
        restartWithFreshLink(key: key, client: client)
    }

    func cancel(key: String) {
        tasks[key]?.cancel()
        tasks.removeValue(forKey: key)
        resumeDatas.removeValue(forKey: key)
        items.removeValue(forKey: key)
        progress.removeValue(forKey: key)
        saveRegistry()
    }

    /// Удаляет скачанный файл с телефона (в TorBox ничего не трогает).
    func deleteLocal(key: String) {
        if let item = items[key] {
            let url = Self.documentsURL.appending(path: item.relativePath)
            try? FileManager.default.removeItem(at: url)
            // Пустую папку торрента подчищаем
            let dir = url.deletingLastPathComponent()
            if let rest = try? FileManager.default.contentsOfDirectory(atPath: dir.path), rest.isEmpty {
                try? FileManager.default.removeItem(at: dir)
            }
        }
        items.removeValue(forKey: key)
        progress.removeValue(forKey: key)
        saveRegistry()
    }

    // MARK: - Внутреннее

    private func makeItem(key: String, torrent: TBTorrent, file: TBFile, state: Item.State) -> Item {
        Item(
            key: key,
            torrentID: torrent.id,
            fileID: file.id,
            torrentName: torrent.name ?? "Без названия",
            fileName: file.displayName,
            totalBytes: file.size ?? 0,
            relativePath: "Фильмы/\(Self.sanitize(torrent.name ?? "torrent-\(torrent.id)"))/\(Self.sanitize(file.displayName))",
            state: state
        )
    }

    private static func sanitize(_ name: String) -> String {
        name.replacingOccurrences(of: "/", with: "⁄").prefix(120).trimmingCharacters(in: .whitespaces)
    }

    private func startTask(key: String, url: URL) {
        let task = session.downloadTask(with: url)
        task.taskDescription = key
        tasks[key] = task
        task.resume()
    }

    private func restartWithFreshLink(key: String, client: TorBoxClient?) {
        if let refresh = refreshers[key] {
            Task {
                do {
                    let fresh = try await refresh()
                    startTask(key: key, url: fresh)
                } catch {
                    items[key]?.state = .failed(error.localizedDescription)
                    saveRegistry()
                }
            }
            return
        }
        guard let client, let item = items[key] else {
            items[key]?.state = .failed("Нет способа обновить ссылку")
            saveRegistry()
            return
        }
        Task {
            do {
                let fresh = try await client.downloadLink(torrentID: item.torrentID, fileID: item.fileID)
                startTask(key: key, url: fresh)
            } catch {
                items[key]?.state = .failed(error.localizedDescription)
                saveRegistry()
            }
        }
    }

    private func saveRegistry() {
        if let data = try? JSONEncoder().encode(items) {
            try? data.write(to: Self.registryURL, options: .atomic)
        }
    }
}

extension DownloadManager: @preconcurrency URLSessionDownloadDelegate {
    func urlSession(
        _ session: URLSession, downloadTask: URLSessionDownloadTask,
        didWriteData bytesWritten: Int64, totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64
    ) {
        guard let key = downloadTask.taskDescription else { return }
        let now = Date()
        if let last = lastProgressUpdate[key], now.timeIntervalSince(last) < 0.5 { return }
        lastProgressUpdate[key] = now
        if totalBytesExpectedToWrite > 0 {
            progress[key] = Double(totalBytesWritten) / Double(totalBytesExpectedToWrite)
            if items[key]?.totalBytes == 0 {
                items[key]?.totalBytes = totalBytesExpectedToWrite
            }
        }
    }

    func urlSession(
        _ session: URLSession, downloadTask: URLSessionDownloadTask,
        didFinishDownloadingTo location: URL
    ) {
        guard let key = downloadTask.taskDescription, let item = items[key] else { return }
        let status = (downloadTask.response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200...299).contains(status) else {
            // Тело с ошибкой (протухшая ссылка и т.п.) — обрабатываем в didComplete
            items[key]?.state = .failed("HTTP \(status)")
            return
        }
        var target = Self.documentsURL.appending(path: item.relativePath)
        do {
            try FileManager.default.createDirectory(
                at: target.deletingLastPathComponent(), withIntermediateDirectories: true
            )
            try? FileManager.default.removeItem(at: target)
            try FileManager.default.moveItem(at: location, to: target)
            var values = URLResourceValues()
            values.isExcludedFromBackup = true
            try? target.setResourceValues(values)
            items[key]?.state = .done
            if let size = try? target.resourceValues(forKeys: [.fileSizeKey]).fileSize {
                items[key]?.totalBytes = Int64(size)
            }
            progress[key] = 1
        } catch {
            items[key]?.state = .failed("Не сохранился файл: \(error.localizedDescription)")
        }
        tasks.removeValue(forKey: key)
        saveRegistry()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: (any Error)?) {
        guard let key = task.taskDescription, let error else { return }
        tasks.removeValue(forKey: key)
        let nsError = error as NSError
        if nsError.code == NSURLErrorCancelled {
            return // пауза или отмена — состояние уже выставлено
        }
        if let data = nsError.userInfo[NSURLSessionDownloadTaskResumeData] as? Data {
            resumeDatas[key] = data
        }
        // Один автоперезапуск со свежей ссылкой: CDN-ссылки протухают за часы
        if !didRetry.contains(key) {
            didRetry.insert(key)
            resumeDatas.removeValue(forKey: key)
            restartWithFreshLink(key: key, client: nil)
            return
        }
        items[key]?.state = .failed(error.localizedDescription)
        saveRegistry()
    }
}
