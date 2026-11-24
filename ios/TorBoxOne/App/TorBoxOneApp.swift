import SwiftUI

@main
struct TorBoxOneApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var appState: AppState

    init() {
        // Штатный URLCache слишком мал — постеры каталога перекачивались бы при каждом скролле
        URLCache.shared = URLCache(memoryCapacity: 64_000_000, diskCapacity: 256_000_000)
        _appState = State(initialValue: AppState())
    }

    var body: some Scene {
        WindowGroup {
            RootTabView()
                .environment(appState)
                .tint(Theme.accent)
                .onOpenURL { url in
                    if appState.infuse.handle(url, appState: appState) { return }
                    // magnet:-ссылка из браузера или другого приложения
                    guard url.scheme?.lowercased() == "magnet" else { return }
                    appState.pendingMagnet = url.absoluteString
                }
        }
    }
}
