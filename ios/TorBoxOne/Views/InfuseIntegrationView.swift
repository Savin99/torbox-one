import SwiftUI

/// Shared integration UI: setup, recoverable launch failures, and legacy file export.
struct InfuseIntegrationSheet: View {
    @Environment(AppState.self) private var appState
    @Environment(\.dismiss) private var dismiss
    let sheet: InfuseSheet

    var body: some View {
        switch sheet {
        case .export(let url):
            FileTransferSheet(url: url)
        case .setup:
            panel(title: "Смотрим в Infuse", message: "TorBox One находит фильмы и передаёт выбранные серии на скачивание. Infuse хранит локальные файлы и помнит место остановки. Установи Infuse \(InfuseProtocol.minimumVersion) или новее.") {
                Link("Установить или обновить Infuse", destination: InfuseLinks.appStore)
                    .buttonStyle(AccentButtonStyle())
                if !appState.infuse.retryItems.isEmpty {
                    Button("Infuse установлен — продолжить") { appState.infuse.retry(appState: appState) }
                        .buttonStyle(AccentButtonStyle())
                }
            }
        case .failure(let message):
            panel(title: "Не удалось передать файлы", message: message) {
                Button("Повторить со свежими ссылками") { appState.infuse.retry(appState: appState) }
                    .buttonStyle(AccentButtonStyle())
                if let key = appState.infuse.retryItems.first?.id,
                   let local = appState.downloads.localURL(key: key) {
                    Button("Передать скачанную копию в Infuse") { appState.infuse.sheet = .export(local) }
                }
            }
        }
    }

    private func panel<Actions: View>(title: String, message: String, @ViewBuilder actions: () -> Actions) -> some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    Image(systemName: "play.rectangle.on.rectangle")
                        .font(.system(size: 42))
                        .foregroundStyle(Theme.accent)
                    Text(title).font(.title.bold())
                    Text(message).foregroundStyle(Theme.textSecondary)
                    actions()
                }
                .padding(24)
            }
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Закрыть") { dismiss() }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }
}

struct InfusePreparationOverlay: View {
    let coordinator: InfuseCoordinator

    var body: some View {
        ZStack {
            Color.black.opacity(0.3).ignoresSafeArea()
            VStack(spacing: 14) {
                ProgressView().controlSize(.large)
                Text("Готовим скачивание в Infuse").font(.headline)
                Text(coordinator.title).font(.subheadline).lineLimit(2).multilineTextAlignment(.center)
                if coordinator.totalCount > 1 {
                    Text("Файлы: \(coordinator.preparedCount) из \(coordinator.totalCount)")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Button("Отмена") { coordinator.cancel() }
            }
            .padding(24)
            .frame(maxWidth: 320)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 24))
            .padding(24)
        }
    }
}

/// iOS grants the receiving application access; a private file:// deep link would not.
struct FileTransferSheet: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: [url], applicationActivities: nil)
    }
    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}

enum InfuseLinks {
    static let appStore = URL(string: "https://apps.apple.com/app/id1136220934")!
}

/// Does not pass a CDN URL to playback. The user opens a completed local download in Infuse.
struct InfuseLibraryCard: View {
    @Environment(AppState.self) private var appState

    var body: some View {
        Button { appState.infuse.openInfuse() } label: {
            HStack(spacing: 12) {
                Image(systemName: "play.circle.fill")
                    .font(.system(size: 30)).foregroundStyle(Theme.accent)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Смотреть скачанное").font(.headline).foregroundStyle(Theme.textPrimary)
                    Text("В Infuse · продолжит с места остановки")
                        .font(.caption).foregroundStyle(Theme.textSecondary)
                }
                Spacer(minLength: 0)
                Image(systemName: "arrow.up.forward.app").foregroundStyle(Theme.textSecondary)
            }
            .padding(16)
            .cardSurface(radius: Theme.radiusM)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Открыть Infuse для просмотра скачанных файлов")
    }
}
