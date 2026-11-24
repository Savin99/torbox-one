import Foundation
import Observation
import UIKit

enum InfuseSheet: Identifiable {
    case setup
    case failure(String)
    case export(URL)
    var id: String {
        switch self {
        case .setup: "setup"
        case .failure: "failure"
        case .export(let url): url.path
        }
    }
}

/// TorBox One prepares downloads; Infuse owns local files, playback, resume and deletion.
@Observable
final class InfuseCoordinator {
    private(set) var isInstalled = false
    private(set) var isPreparing = false
    private(set) var title = ""
    private(set) var preparedCount = 0
    private(set) var totalCount = 0
    private(set) var lastAcceptedCount = 0
    var sheet: InfuseSheet?
    private(set) var retryItems: [InfuseDownloadItem] = []
    private let sessions: InfuseSessionStore
    private var task: Task<Void, Never>?
    private var requestID = UUID()

    init(sessions: InfuseSessionStore = InfuseSessionStore()) {
        self.sessions = sessions
        refreshAvailability()
    }

    func refreshAvailability() {
        isInstalled = UIApplication.shared.canOpenURL(URL(string: "infuse://")!)
    }

    func accountChanged() {
        cancel()
        sessions.clear()
        retryItems = []
        lastAcceptedCount = 0
        sheet = nil
    }

    func start(_ items: [InfuseDownloadItem], appState: AppState) {
        guard !items.isEmpty else { return }
        cancel()
        retryItems = items
        sheet = nil
        refreshAvailability()
        guard isInstalled else { sheet = .setup; return }
        let id = UUID()
        requestID = id
        isPreparing = true
        title = items[0].torrentName
        preparedCount = 0
        totalCount = items.count
        let account = InfuseProtocol.digest(appState.torBoxKey)
        let client = appState.torBoxClient
        task = Task { [weak self] in
            guard let self else { return }
            defer {
                if requestID == id { isPreparing = false; task = nil }
            }
            do {
                guard let client else { throw AppError.notConfigured }
                var media: [InfuseProtocol.Media] = []
                var tracks: [InfuseSession.Track] = []
                for item in items {
                    try Task.checkCancellation()
                    let url = try await client.downloadLink(torrentID: item.torrentID, fileID: item.fileID)
                    var subtitle: URL?
                    if let subtitleID = item.subtitleFileID {
                        subtitle = try? await client.downloadLink(torrentID: item.torrentID, fileID: subtitleID)
                    }
                    media.append(.init(url: url, filename: item.filename, subtitle: subtitle))
                    tracks.append(.init(item: item, urlDigest: InfuseProtocol.digest(url.absoluteString)))
                    preparedCount = media.count
                }
                try Task.checkCancellation()
                guard requestID == id, account == InfuseProtocol.digest(appState.torBoxKey) else { return }
                let session = InfuseSession(id: UUID(), account: account, createdAt: Date(), tracks: tracks)
                let url = try InfuseProtocol.downloadURL(media: media, sessionID: session.id)
                try sessions.save(session)
                let opened = await UIApplication.shared.open(url, options: [:])
                guard requestID == id else { return }
                if !opened {
                    sessions.clear()
                    sheet = .failure("iOS не смогла открыть Infuse. Проверь, что приложение установлено и обновлено.")
                }
            } catch is CancellationError {
                // No app handoff or partial season transfer when cancelled.
            } catch {
                guard requestID == id, !Task.isCancelled else { return }
                sheet = .failure(error.localizedDescription)
            }
        }
    }

    func retry(appState: AppState) { start(retryItems, appState: appState) }

    func cancel() {
        requestID = UUID()
        task?.cancel()
        task = nil
        isPreparing = false
    }

    @discardableResult
    func handle(_ url: URL, appState: AppState) -> Bool {
        guard url.scheme?.lowercased() == "torboxone" else { return false }
        guard var session = sessions.session,
              let callback = InfuseProtocol.callback(url, session: session,
                  account: InfuseProtocol.digest(appState.torBoxKey)) else { return true }
        switch callback {
        case .accepted:
            session.acceptedAt = Date()
            do { try sessions.save(session) } catch { sessions.clear() }
            lastAcceptedCount = session.tracks.count
            retryItems = []
            appState.toasts.success("Передано в Infuse: \(session.tracks.count)",
                message: "Дождись завершения загрузки в Infuse — затем смотри без интернета.")
        case .failed(let indices):
            sessions.clear()
            retryItems = indices.map { session.tracks[$0].item }
            sheet = .failure("Infuse не принял файлов: \(retryItems.count). Повтор получит свежие ссылки только для них.")
        }
        return true
    }

    /// Deliberately opens Infuse itself, never its remote-play action.
    func openInfuse() {
        refreshAvailability()
        guard isInstalled else { sheet = .setup; return }
        UIApplication.shared.open(URL(string: "infuse://")!, options: [:])
    }
}
