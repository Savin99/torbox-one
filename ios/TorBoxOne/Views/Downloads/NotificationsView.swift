import SwiftUI

/// Уведомления TorBox: что и когда закончилось.
struct NotificationsView: View {
    @Environment(AppState.self) private var appState
    let vm: DownloadsViewModel

    @State private var confirmClear = false

    var body: some View {
        ZStack {
            AppBackground()
            if vm.notificationsList.isEmpty {
                EmptyState(
                    icon: "bell.slash",
                    title: "Уведомлений нет",
                    message: "Здесь появятся сообщения TorBox: завершённые загрузки, проблемы с раздачами.",
                    tint: Theme.idle
                )
            } else {
                List {
                    ForEach(vm.notificationsList) { item in
                        row(item)
                            .plainListRow()
                    }
                    Section {
                        Color.clear.frame(height: Theme.tabBarInset)
                            .plainListRow(insets: EdgeInsets())
                    }
                }
                .themedList()
            }
        }
        .navigationTitle("Уведомления")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if !vm.notificationsList.isEmpty {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Очистить") {
                        Haptics.tap()
                        confirmClear = true
                    }
                }
            }
        }
        .task {
            if let client = appState.torBoxClient {
                await vm.loadNotifications(client: client)
                vm.markNotificationsSeen()
            }
        }
        .confirmationDialog("Очистить все уведомления?", isPresented: $confirmClear, titleVisibility: .visible) {
            Button("Очистить", role: .destructive) {
                if let client = appState.torBoxClient {
                    Task {
                        await vm.clearNotifications(client: client)
                        appState.toasts.success("Уведомления очищены")
                    }
                }
            }
            Button("Отмена", role: .cancel) {}
        }
    }

    private func row(_ item: TBNotification) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon(for: item))
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(tint(for: item))
                .frame(width: 34, height: 34)
                .background(Circle().fill(tint(for: item).opacity(0.15)))
            VStack(alignment: .leading, spacing: 3) {
                Text(item.title ?? "Уведомление")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.textPrimary)
                if let message = item.message, !message.isEmpty {
                    Text(message)
                        .font(.footnote)
                        .foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let date = Format.parseAPIDate(item.createdAt) {
                    Text(Format.relative(date))
                        .font(.caption2)
                        .foregroundStyle(Theme.textTertiary)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(14)
        .cardSurface(radius: Theme.radiusM)
        .accessibilityElement(children: .combine)
    }

    private func icon(for item: TBNotification) -> String {
        let text = ((item.title ?? "") + (item.message ?? "")).lowercased()
        if text.contains("fail") || text.contains("error") || text.contains("ошиб") {
            return "exclamationmark.triangle.fill"
        }
        if text.contains("download") || text.contains("complete") || text.contains("готов") {
            return "checkmark.circle.fill"
        }
        return "bell.fill"
    }

    private func tint(for item: TBNotification) -> Color {
        let text = ((item.title ?? "") + (item.message ?? "")).lowercased()
        if text.contains("fail") || text.contains("error") || text.contains("ошиб") {
            return Theme.danger
        }
        if text.contains("download") || text.contains("complete") || text.contains("готов") {
            return Theme.accent
        }
        return Theme.info
    }
}
