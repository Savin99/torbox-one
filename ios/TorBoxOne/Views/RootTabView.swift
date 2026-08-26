import SwiftUI

struct RootTabView: View {
    @Environment(AppState.self) private var appState

    var body: some View {
        @Bindable var state = appState
        TabView(selection: $state.selectedTab) {
            Tab("Главная", systemImage: "play.house", value: AppTab.home) {
                HomeTab()
            }
            Tab("Библиотека", systemImage: "film.stack", value: AppTab.library) {
                DownloadsTab()
            }
            .badge(activeDownloads)
            Tab("Настройки", systemImage: "gearshape", value: AppTab.settings) {
                SettingsTab()
            }
        }
        .tint(Theme.accent)
        .tabBarMinimizeBehavior(.onScrollDown)
        .overlay(alignment: .top) {
            ToastOverlay(toasts: appState.toasts)
        }
        .fullScreenCover(isPresented: Binding(
            get: { !appState.didOnboard },
            set: { if !$0 { state.didOnboard = true } }
        )) {
            OnboardingView()
        }
        .onChange(of: appState.selectedTab) { _, _ in
            Haptics.tap()
        }
        .onChange(of: appState.pendingMagnet) { _, magnet in
            // Ссылка magnet: из другого приложения — ведём в «Библиотеку»,
            // там лист подхватит её сам (вкладка могла быть ещё не создана).
            if magnet != nil {
                appState.selectedTab = .library
            }
        }
    }

    /// Число живых загрузок в память телефона — на бейдже таба.
    private var activeDownloads: Int {
        appState.downloads.items.values.filter { $0.state == .downloading }.count
    }
}
