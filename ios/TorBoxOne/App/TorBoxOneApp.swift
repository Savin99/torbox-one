import SwiftUI
import VLCKit

@main
struct TorBoxOneApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var appState: AppState

    init() {
        // Штатный URLCache слишком мал — постеры каталога перекачивались бы при каждом скролле
        URLCache.shared = URLCache(memoryCapacity: 64_000_000, diskCapacity: 256_000_000)
        // События VLCKit 4 по умолчанию приходят на потоке libvlc; legacy-конфигурация
        // доставляет их async на main — иначе делегат плеера нарушит MainActor-изоляцию
        VLCLibrary.sharedEventsConfiguration = VLCEventsLegacyConfiguration()
        _appState = State(initialValue: AppState())
    }

    var body: some Scene {
        WindowGroup {
            RootTabView()
                .environment(appState)
                .tint(Theme.accent)
                .onOpenURL { url in
                    // magnet:-ссылка из браузера или другого приложения
                    guard url.scheme?.lowercased() == "magnet" else { return }
                    appState.pendingMagnet = url.absoluteString
                }
        }
    }
}
