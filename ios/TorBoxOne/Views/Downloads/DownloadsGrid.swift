import SwiftUI

/// Библиотека постерами: как в Infuse — плитки с обложками вместо строк.
struct DownloadsGrid: View {
    @Environment(AppState.self) private var appState
    let torrents: [TBTorrent]
    let onOpen: (TBTorrent) -> Void
    let onDelete: (TBTorrent) -> Void
    let onDownload: (TBTorrent) -> Void

    @Environment(\.dynamicTypeSize) private var typeSize

    private var columns: [GridItem] {
        let count = typeSize >= .accessibility1 ? 2 : 3
        return Array(repeating: GridItem(.flexible(), spacing: 12), count: count)
    }

    var body: some View {
        LazyVGrid(columns: columns, spacing: 16) {
            ForEach(torrents) { torrent in
                tile(torrent)
            }
        }
        .padding(.horizontal, Theme.gutter)
    }

    private func tile(_ torrent: TBTorrent) -> some View {
        let meta = appState.meta.meta(for: torrent.name)
        let status = TorrentStatus(torrent: torrent)
        let fraction = Format.progressFraction(torrent.progress)
        return Button {
            Haptics.tap()
            onOpen(torrent)
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                ZStack(alignment: .bottom) {
                    PosterImage(
                        url: meta?.posterUrl ?? "",
                        fallbackTitle: meta?.title ?? (torrent.name ?? "")
                    )
                    if !status.isFinished {
                        VStack(spacing: 4) {
                            Spacer(minLength: 0)
                            SlimProgressBar(fraction: fraction, tint: status.tint, height: 3)
                            Text("\(Int(fraction * 100))%")
                                .font(.caption2.weight(.bold))
                                .foregroundStyle(.white)
                        }
                        .padding(6)
                        .background(
                            LinearGradient(colors: [.clear, .black.opacity(0.75)], startPoint: .top, endPoint: .bottom)
                        )
                    }
                }
                .overlay(alignment: .topTrailing) {
                    if let rating = meta?.ratingValue {
                        RatingBadge(value: rating)
                            .padding(5)
                    }
                }
                Text(meta?.title.isEmpty == false ? meta!.title : (torrent.name ?? "Без названия"))
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                Text(subtitle(meta: meta, torrent: torrent))
                    .font(.caption2)
                    .foregroundStyle(Theme.textTertiary)
                    .lineLimit(1)
            }
        }
        .buttonStyle(.plain)
        .task {
            await appState.meta.load(name: torrent.name, client: appState.searchClient)
        }
        .contextMenu {
            if status.isFinished, !DownloadSelection.videoFiles(of: torrent).isEmpty {
                Button {
                    onDownload(torrent)
                } label: {
                    Label("Выбрать для скачивания", systemImage: "arrow.down.to.line")
                }
            }
            Button {
                onOpen(torrent)
            } label: {
                Label("Открыть раздачу", systemImage: "info.circle")
            }
            Button(role: .destructive) {
                onDelete(torrent)
            } label: {
                Label("Удалить из TorBox", systemImage: "trash")
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(
            [meta?.title ?? torrent.name ?? "", status.label].filter { !$0.isEmpty }.joined(separator: ", ")
        )
        .accessibilityAddTraits(.isButton)
    }

    private func subtitle(meta: TitleMeta?, torrent: TBTorrent) -> String {
        if let meta {
            if meta.seasonsCount > 0 {
                return "\(meta.seasonsCount) \(Format.plural(meta.seasonsCount, "сезон", "сезона", "сезонов"))"
            }
            if !meta.year.isEmpty, meta.year != "null" { return meta.year }
        }
        if let size = torrent.size, size > 0 { return Format.bytes(size) }
        return ""
    }
}
