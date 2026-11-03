import SwiftUI

struct RootTabView: View {
    @Environment(AppState.self) private var appState
    @Environment(\.scenePhase) private var scenePhase

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
        .overlay {
            if appState.infuse.isPreparing {
                InfusePreparationOverlay(coordinator: appState.infuse)
            }
        }
        .sheet(item: Binding(get: { appState.infuse.sheet }, set: { appState.infuse.sheet = $0 })) { sheet in
            InfuseIntegrationSheet(sheet: sheet)
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
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { appState.infuse.refreshAvailability() }
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
