import SwiftUI

/// Ряд карточек «Продолжить просмотр». Отдельный тип вью, а не свойство:
/// иначе `@Environment` не проставляется и обращение падает.
struct ContinueWatchingCards: View {
    @Environment(AppState.self) private var appState
    let vm: DownloadsViewModel
    let onPlay: (PlayerQueue) -> Void

    @State private var busyKey: String?

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 12) {
                ForEach(resumable) { entry in
                    card(entry)
                }
            }
            .padding(.horizontal, Theme.gutter)
            .padding(.vertical, 2)
        }
    }

    /// Только то, что реально можно включить: либо раздача на месте, либо файл скачан.
    private var resumable: [PlaybackStore.Entry] {
        appState.playback.continueWatching.filter { entry in
            vm.torrents.contains { $0.id == entry.torrentID }
                || appState.downloads.localURL(key: entry.key) != nil
        }
    }

    private func card(_ entry: PlaybackStore.Entry) -> some View {
        Button {
            play(entry)
        } label: {
            VStack(alignment: .leading, spacing: 0) {
                ZStack(alignment: .bottomLeading) {
                    WatchArtwork(
                        entryKey: entry.key,
                        torrentName: entry.torrentName,
                        fileName: entry.fileName,
                        height: 118
                    )
                    LinearGradient(colors: [.clear, .black.opacity(0.55)], startPoint: .top, endPoint: .bottom)
                        .frame(height: 118)
                    HStack(spacing: 8) {
                        if busyKey == entry.key {
                            ProgressView()
                                .tint(.white)
                                .frame(width: 30, height: 30)
                        } else {
                            Image(systemName: "play.fill")
                                .font(.system(size: 13, weight: .bold))
                                .foregroundStyle(.black)
                                .frame(width: 30, height: 30)
                                .background(Circle().fill(.white))
                        }
                        VStack(alignment: .leading, spacing: 1) {
                            if let label = EpisodeInfo.parse(entry.fileName).label {
                                Text(label)
                                    .font(.caption2.weight(.bold))
                                    .foregroundStyle(.white)
                            }
                            Text("с \(Format.clock(entry.positionSeconds))")
                                .font(.caption2.weight(.medium))
                                .foregroundStyle(.white.opacity(0.85))
                        }
                        Spacer(minLength: 0)
                        if appState.downloads.localURL(key: entry.key) != nil {
                            Image(systemName: "iphone")
                                .font(.caption2.weight(.bold))
                                .foregroundStyle(.white.opacity(0.9))
                        }
                    }
                    .padding(10)
                }
                .frame(height: 118)

                VStack(alignment: .leading, spacing: 6) {
                    Text(displayTitle(entry))
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Theme.textPrimary)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                        .frame(height: 30, alignment: .top)
                    Text(appState.meta.meta(for: entry.torrentName)?.title ?? entry.torrentName)
                        .font(.caption2)
                        .foregroundStyle(Theme.textTertiary)
                        .lineLimit(1)
                    SlimProgressBar(fraction: entry.fraction)
                    Text(Format.remaining(entry.remainingSeconds))
                        .font(.caption2)
                        .foregroundStyle(Theme.textSecondary)
                }
                .padding(10)
            }
            .frame(width: 264)
            .cardSurface(radius: Theme.radiusM, raised: true)
            .clipShape(RoundedRectangle(cornerRadius: Theme.radiusM, style: .continuous))
        }
        .buttonStyle(.plain)
        .task {
            await appState.meta.load(name: entry.torrentName, client: appState.searchClient)
        }
        .contextMenu {
            Button(role: .destructive) {
                withAnimation { appState.playback.remove(key: entry.key) }
            } label: {
                Label("Убрать из списка", systemImage: "xmark.circle")
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Продолжить \(displayTitle(entry)), \(Int(entry.fraction * 100)) процентов")
        .accessibilityAddTraits(.isButton)
    }

    /// Название серии из каталога, иначе — вычищенное имя файла.
    private func displayTitle(_ entry: PlaybackStore.Entry) -> String {
        if let meta = appState.meta.meta(for: entry.torrentName) {
            let info = EpisodeInfo.parse(entry.fileName)
            if let number = info.episode,
               let episode = appState.meta.episode(
                   titleID: meta.sourceId, season: info.season, number: number
               ),
               !episode.title.isEmpty {
                return episode.title
            }
        }
        return MediaName.pretty(entry.fileName, seriesName: entry.torrentName)
    }

    private func play(_ entry: PlaybackStore.Entry) {
        Haptics.press()
        let torrent = vm.torrents.first { $0.id == entry.torrentID }
        // Для сериала собираем очередь целиком: после серии сразу пойдёт следующая
        if let torrent, let file = torrent.files?.first(where: { $0.id == entry.fileID }) {
            onPlay(PlayerQueueFactory.queue(torrent: torrent, startAt: file, appState: appState))
            return
        }
        if let item = PlayerQueueFactory.item(entry: entry, torrent: torrent, appState: appState) {
            onPlay(PlayerQueue(single: item))
            return
        }
        appState.toasts.error(
            "Файл недоступен",
            message: "Раздачи больше нет в TorBox, а локальной копии не осталось.",
            actionTitle: "Убрать",
            action: { appState.playback.remove(key: entry.key) }
        )
    }
}

/// Ряд с заголовком — для экранов без `List`.
struct ContinueWatchingStrip: View {
    @Environment(AppState.self) private var appState
    let vm: DownloadsViewModel
    let onPlay: (PlayerQueue) -> Void

    var body: some View {
        if hasItems {
            VStack(alignment: .leading, spacing: 10) {
                SectionHeader(title: "Продолжить просмотр", subtitle: "с того места, где остановился")
                    .padding(.horizontal, Theme.gutter)
                ContinueWatchingCards(vm: vm, onPlay: onPlay)
            }
        }
    }

    private var hasItems: Bool {
        appState.playback.continueWatching.contains { entry in
            vm.torrents.contains { $0.id == entry.torrentID }
                || appState.downloads.localURL(key: entry.key) != nil
        }
    }
}

/// Секция для `List` в библиотеке.
struct ContinueWatchingCarousel: View {
    @Environment(AppState.self) private var appState
    let vm: DownloadsViewModel
    let onPlay: (PlayerQueue) -> Void

    var body: some View {
        if hasItems {
            Section {
                ContinueWatchingCards(vm: vm, onPlay: onPlay)
                    .plainListRow(insets: EdgeInsets(top: 4, leading: 0, bottom: 4, trailing: 0))
            } header: {
                SectionHeader(title: "Продолжить просмотр", subtitle: "с того места, где остановился")
                    .textCase(nil)
                    .plainListRow(insets: EdgeInsets(top: 12, leading: Theme.gutter, bottom: 4, trailing: Theme.gutter))
            }
        }
    }

    private var hasItems: Bool {
        appState.playback.continueWatching.contains { entry in
            vm.torrents.contains { $0.id == entry.torrentID }
                || appState.downloads.localURL(key: entry.key) != nil
        }
    }
}
