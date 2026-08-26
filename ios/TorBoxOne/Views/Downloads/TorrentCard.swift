import SwiftUI

/// Строка раздачи: постер тайтла, название из каталога и главное о состоянии.
struct TorrentCard: View {
    @Environment(AppState.self) private var appState
    let torrent: TBTorrent
    /// Сколько файлов этой раздачи уже лежит в памяти телефона.
    var localFiles: Int = 0
    var isBusy = false

    var body: some View {
        let status = TorrentStatus(torrent: torrent)
        let fraction = Format.progressFraction(torrent.progress)
        let meta = appState.meta.meta(for: torrent.name)
        return HStack(alignment: .top, spacing: 12) {
            PosterThumb(
                meta: meta,
                fallbackTitle: torrent.name ?? "",
                width: 52,
                progress: status.isFinished ? nil : fraction,
                tint: status.tint
            )
            VStack(alignment: .leading, spacing: 7) {
                Text(meta?.title.isEmpty == false ? meta!.title : (torrent.name ?? "Без названия"))
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)

                if let line = metaLine(meta: meta) {
                    Text(line)
                        .font(.caption)
                        .foregroundStyle(Theme.textTertiary)
                        .lineLimit(1)
                }

                if !status.isFinished {
                    SlimProgressBar(fraction: fraction, tint: status.tint)
                        .padding(.trailing, 4)
                }

                FlowLayout(spacing: 6) {
                    StatusPill(status: status)
                    if status.isActive, let speed = torrent.downloadSpeed, speed > 0 {
                        Chip(text: Format.speed(speed), icon: "speedometer", style: .tinted(Theme.info))
                    }
                    if status.isActive, let eta = torrent.eta, eta > 0 {
                        Chip(text: Format.eta(eta), icon: "clock", style: .tinted(Theme.instant),
                             accessibility: "Осталось \(Format.eta(eta))")
                    }
                }

                FlowLayout(spacing: 6) {
                    if let size = torrent.size, size > 0 {
                        Chip.size(size)
                    }
                    if let seeds = torrent.seeds, !status.isFinished {
                        Chip.seeders(seeds)
                    }
                    if localFiles > 0 {
                        Chip(
                            text: "\(localFiles)",
                            icon: "iphone",
                            style: .tinted(Theme.info),
                            accessibility: "\(localFiles) \(Format.plural(localFiles, "файл", "файла", "файлов")) в памяти телефона"
                        )
                    }
                    if let files = torrent.files?.count, files > 1 {
                        Chip(
                            text: "\(files)",
                            icon: "film.stack",
                            accessibility: "\(files) \(Format.plural(files, "файл", "файла", "файлов")) в раздаче"
                        )
                    }
                }
            }
            Spacer(minLength: 0)
        }
        .padding(14)
        .cardSurface()
        .opacity(isBusy ? 0.55 : 1)
        .overlay {
            if isBusy {
                ProgressView()
            }
        }
        .task {
            await appState.meta.load(name: torrent.name, client: appState.searchClient)
        }
        .accessibilityElement(children: .combine)
    }

    /// Вторая строка: год и жанры из каталога, иначе исходное имя раздачи.
    private func metaLine(meta: TitleMeta?) -> String? {
        guard let meta else { return nil }
        var parts: [String] = []
        if !meta.year.isEmpty, meta.year != "null" { parts.append(meta.year) }
        if meta.seasonsCount > 0 {
            parts.append("\(meta.seasonsCount) \(Format.plural(meta.seasonsCount, "сезон", "сезона", "сезонов"))")
        }
        if let genre = meta.genres.first { parts.append(genre) }
        let line = parts.joined(separator: " · ")
        return line.isEmpty ? nil : line
    }
}
