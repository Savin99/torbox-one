import SwiftUI

/// «Память телефона»: что скачано, сколько занято, что качается сейчас.
struct DownloadedFilesView: View {
    @Environment(AppState.self) private var appState
    @State private var confirmClearAll = false

    var body: some View {
        let downloads = appState.downloads
        ZStack {
            AppBackground()
            ScrollView {
                VStack(spacing: 16) {
                    storageCard

                    if !downloads.activeItems.isEmpty {
                        VStack(alignment: .leading, spacing: 10) {
                            SectionHeader(title: "В процессе")
                            ForEach(downloads.activeItems) { item in
                                activeRow(item)
                            }
                        }
                    }

                    VStack(alignment: .leading, spacing: 10) {
                        SectionHeader(
                            title: "На телефоне",
                            subtitle: downloads.doneItems.isEmpty
                                ? nil
                                : "\(downloads.doneItems.count) \(Format.plural(downloads.doneItems.count, "файл", "файла", "файлов"))",
                            actionTitle: downloads.doneItems.isEmpty ? nil : "Удалить все",
                            action: downloads.doneItems.isEmpty ? nil : { confirmClearAll = true }
                        )
                        if downloads.doneItems.isEmpty {
                            EmptyState(
                                icon: "arrow.down.to.line",
                                title: "Ничего не скачано",
                                message: "Новые видео сохраняются через «Скачать в Infuse». Здесь остаются файлы, скачанные в TorBox One раньше.",
                                tint: Theme.info
                            )
                        } else {
                            ForEach(downloads.doneItems) { item in
                                doneRow(item)
                            }
                        }
                    }

                    Text("Файлы лежат в «Файлы» → TorBox One → Фильмы. Удаление здесь не трогает облако TorBox.")
                        .font(.caption)
                        .foregroundStyle(Theme.textTertiary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.horizontal, Theme.gutter)
                .padding(.top, 12)
                .padding(.bottom, Theme.tabBarInset)
            }
        }
        .navigationTitle("Файлы TorBox One")
        .navigationBarTitleDisplayMode(.inline)
        .confirmationDialog(
            "Удалить все скачанные файлы с телефона?",
            isPresented: $confirmClearAll,
            titleVisibility: .visible
        ) {
            Button("Удалить все", role: .destructive) {
                for item in appState.downloads.doneItems {
                    appState.downloads.deleteLocal(key: item.key)
                }
                appState.toasts.success("Память освобождена")
            }
            Button("Отмена", role: .cancel) {}
        }
    }

    /// Полоса «занято приложением / свободно» — сразу видно, есть ли место.
    private var storageCard: some View {
        let used = appState.downloads.totalBytesUsed
        let free = DownloadManager.freeBytes
        let total = max(used + free, 1)
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(Format.bytes(used))
                        .font(.title2.weight(.bold))
                        .foregroundStyle(Theme.textPrimary)
                    Text("занимают серии TorBox One")
                        .font(.caption)
                        .foregroundStyle(Theme.textSecondary)
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 2) {
                    Text(Format.bytes(free))
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.accent)
                    Text("свободно")
                        .font(.caption)
                        .foregroundStyle(Theme.textSecondary)
                }
            }
            SlimProgressBar(fraction: Double(used) / Double(total), tint: Theme.info, height: 8)
        }
        .padding(16)
        .cardSurface()
    }

    private func doneRow(_ item: DownloadManager.Item) -> some View {
        HStack(spacing: 12) {
            FileArtwork(title: item.fileName, side: 42)
            VStack(alignment: .leading, spacing: 3) {
                Text(MediaName.pretty(item.fileName, seriesName: item.torrentName))
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(2)
                Text(item.torrentName)
                    .font(.caption)
                    .foregroundStyle(Theme.textTertiary)
                    .lineLimit(1)
                Chip.size(item.totalBytes)
            }
            Spacer(minLength: 0)
            if let local = appState.downloads.localURL(key: item.key) {
                ShareLink(item: local) {
                    Image(systemName: "square.and.arrow.up")
                        .frame(width: 44, height: 44)
                }
                .accessibilityLabel("Передать \(item.fileName) в Infuse")
            }
            Button {
                Haptics.warning()
                appState.downloads.deleteLocal(key: item.key)
            } label: {
                Image(systemName: "trash")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(Theme.danger)
                    .frame(width: 38, height: 38)
                    .background(Circle().fill(Theme.danger.opacity(0.12)))
                    .hitTarget()
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Удалить \(item.fileName) с телефона")
        }
        .padding(12)
        .cardSurface(radius: Theme.radiusM)
    }

    private func activeRow(_ item: DownloadManager.Item) -> some View {
        let fraction = appState.downloads.progress[item.key] ?? 0
        return HStack(spacing: 12) {
            ProgressRing(fraction: fraction, tint: tint(for: item.state), diameter: 42, lineWidth: 4)
            VStack(alignment: .leading, spacing: 3) {
                Text(MediaName.pretty(item.fileName, seriesName: item.torrentName))
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(2)
                Text(stateText(item))
                    .font(.caption)
                    .foregroundStyle(Theme.textSecondary)
            }
            Spacer(minLength: 0)
            controls(item)
        }
        .padding(12)
        .cardSurface(radius: Theme.radiusM)
    }

    @ViewBuilder
    private func controls(_ item: DownloadManager.Item) -> some View {
        HStack(spacing: 6) {
            switch item.state {
            case .downloading:
                iconButton("pause.fill", tint: Theme.instant, label: "Пауза загрузки") {
                    appState.downloads.pause(key: item.key)
                }
            case .paused, .failed:
                iconButton("arrow.clockwise", tint: Theme.accent, label: "Продолжить загрузку") {
                    appState.downloads.resume(key: item.key, client: appState.torBoxClient)
                }
            case .done:
                EmptyView()
            }
            iconButton("xmark", tint: Theme.danger, label: "Отменить загрузку") {
                appState.downloads.cancel(key: item.key)
            }
        }
    }

    private func iconButton(_ system: String, tint: Color, label: String, action: @escaping () -> Void) -> some View {
        Button {
            Haptics.tap()
            action()
        } label: {
            Image(systemName: system)
                .font(.system(size: 13, weight: .bold))
                .foregroundStyle(tint)
                .frame(width: 34, height: 34)
                .background(Circle().fill(tint.opacity(0.12)))
                .hitTarget()
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }

    private func stateText(_ item: DownloadManager.Item) -> String {
        switch item.state {
        case .downloading:
            let fraction = appState.downloads.progress[item.key] ?? 0
            return "качается · \(Int(fraction * 100))% из \(Format.bytes(item.totalBytes))"
        case .paused:
            return "на паузе · \(Format.bytes(item.totalBytes))"
        case .failed(let message):
            return "ошибка: \(message)"
        case .done:
            return Format.bytes(item.totalBytes)
        }
    }

    private func tint(for state: DownloadManager.Item.State) -> Color {
        switch state {
        case .downloading: Theme.info
        case .paused: Theme.idle
        case .failed: Theme.danger
        case .done: Theme.accent
        }
    }
}
