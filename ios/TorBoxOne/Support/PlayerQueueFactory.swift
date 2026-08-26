import Foundation

/// Сборка очереди для плеера. Здесь же решается, играть локальный файл или
/// тянуть свежую CDN-ссылку, и нужно ли попутно качать серию в телефон.
enum PlayerQueueFactory {
    /// Все видеофайлы раздачи по порядку, старт — с выбранного файла.
    static func queue(
        torrent: TBTorrent,
        startAt file: TBFile,
        appState: AppState
    ) -> PlayerQueue {
        let videos = videoFiles(of: torrent)
        let items = videos.map { item(torrent: torrent, file: $0, appState: appState) }
        let index = videos.firstIndex { $0.id == file.id } ?? 0
        guard !items.isEmpty else {
            return PlayerQueue(single: item(torrent: torrent, file: file, appState: appState))
        }
        return PlayerQueue(items: items, index: index)
    }

    /// Видео раздачи в человеческом порядке (сезон, серия, затем имя).
    static func videoFiles(of torrent: TBTorrent) -> [TBFile] {
        (torrent.files ?? []).filter(\.isVideo).sorted(by: TBFile.orderedBefore)
    }

    /// С чего продолжить смотреть раздачу: недосмотренная серия, иначе первая непросмотренная.
    static func suggestedFile(torrent: TBTorrent, appState: AppState) -> TBFile? {
        let videos = videoFiles(of: torrent)
        guard !videos.isEmpty else { return nil }
        let resumable = videos
            .compactMap { file -> (TBFile, Date)? in
                guard let entry = appState.playback.entry(torrent: torrent, fileID: file.id),
                      entry.isResumable else { return nil }
                return (file, entry.updatedAt)
            }
            .max { $0.1 < $1.1 }?
            .0
        if let resumable { return resumable }
        let unwatched = videos.first { file in
            appState.playback.entry(torrent: torrent, fileID: file.id)?.isWatched != true
        }
        return unwatched ?? videos.first
    }

    static func item(torrent: TBTorrent, file: TBFile, appState: AppState) -> PlayerTrackItem {
        let key = PlaybackStore.key(torrent: torrent, fileID: file.id)
        let playback = appState.playback
        let downloads = appState.downloads
        let client = appState.torBoxClient
        let saveWhileWatching = appState.downloadWhileWatching
        let localURL = downloads.localURL(key: key)
        let toasts = appState.toasts
        let openSettings = { appState.selectedTab = .settings }

        let titleMeta = appState.meta.meta(for: torrent.name)
        let episodeInfo = file.episode
        let episodeMeta = titleMeta.flatMap { meta in
            episodeInfo.episode.flatMap { number in
                appState.meta.episode(titleID: meta.sourceId, season: episodeInfo.season, number: number)
            }
        }
        let displayTitle = episodeMeta?.title.isEmpty == false
            ? episodeMeta!.title
            : file.prettyName(seriesName: torrent.name)

        return PlayerTrackItem(
            id: key,
            title: displayTitle,
            subtitle: titleMeta?.title.isEmpty == false ? titleMeta?.title : torrent.name,
            episodeLabel: file.episode.label,
            isLocal: localURL != nil,
            resolveURL: {
                // Скачанный файл играет без сети; иначе просим у TorBox свежую ссылку
                if let localURL { return localURL }
                guard let client else { throw AppError.notConfigured }
                return try await client.downloadLink(torrentID: torrent.id, fileID: file.id)
            },
            startAt: {
                playback.resumePosition(torrent: torrent, fileID: file.id)
            },
            onProgress: { position, duration in
                playback.update(
                    torrent: torrent, file: file,
                    positionSeconds: position, durationSeconds: duration
                )
            },
            onStart: { url in
                // «Как в Telegram»: включил серию — она сразу качается в телефон
                guard localURL == nil, saveWhileWatching, let client else { return }
                let started = downloads.ensureDownloading(torrent: torrent, file: file, url: url) {
                    try await client.downloadLink(torrentID: torrent.id, fileID: file.id)
                }
                if !started {
                    // Молчать нельзя: иначе в списке просто появится «не скачался»
                    toasts.error(
                        "Не хватает места на телефоне",
                        message: "Серия играет, но не сохранится. Освободи память или выключи «Качать при просмотре».",
                        actionTitle: "Настройки",
                        action: { openSettings() }
                    )
                }
            }
        )
    }

    /// Продолжение просмотра из карусели: торрент может быть уже удалён,
    /// тогда играем локальную копию, если она есть.
    static func item(
        entry: PlaybackStore.Entry,
        torrent: TBTorrent?,
        appState: AppState
    ) -> PlayerTrackItem? {
        if let torrent, let file = torrent.files?.first(where: { $0.id == entry.fileID }) {
            return item(torrent: torrent, file: file, appState: appState)
        }
        guard let local = appState.downloads.localURL(key: entry.key) else { return nil }
        let playback = appState.playback
        return PlayerTrackItem.local(
            id: entry.key,
            title: MediaName.pretty(entry.fileName, seriesName: entry.torrentName),
            subtitle: entry.torrentName,
            episodeLabel: EpisodeInfo.parse(entry.fileName).label,
            url: local,
            startAt: { entry.isResumable ? entry.positionSeconds : nil },
            onProgress: { position, duration in
                var updated = entry
                updated.positionSeconds = position
                updated.durationSeconds = duration
                updated.updatedAt = Date()
                playback.upsert(updated)
            }
        )
    }
}
