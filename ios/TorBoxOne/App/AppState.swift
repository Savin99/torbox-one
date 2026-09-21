import Foundation
import Observation

/// Куда ведёт таб-бар. Отдельный тип нужен, чтобы тост «Добавлено» умел
/// переключить пользователя на «Библиотеку» одним нажатием.
enum AppTab: Hashable {
    case home
    case library
    case settings
}

/// Глобальное состояние: креды, собранные из них API-клиенты, навигация и тосты.
@Observable
final class AppState {
    let playback = PlaybackStore()
    let infuse = InfuseCoordinator()
    let downloads = DownloadManager()
    let toasts = ToastCenter()
    let meta = MetaStore()
    let thumbnails = ThumbnailStore()
    /// Список раздач и опрос TorBox общие для всех экранов: иначе «Главная»
    /// и «Библиотека» дублировали бы запросы и расходились в данных.
    let library = DownloadsViewModel()

    /// Выбранный таб — во состоянии, чтобы на него можно было увести из тоста.
    var selectedTab: AppTab = .home
    /// Магнет, полученный извне (share sheet или magnet:-ссылка).
    var pendingMagnet: String?

    /// Первый запуск закрыт — приветственный экран больше не показываем.
    var didOnboard: Bool {
        didSet { UserDefaults.standard.set(didOnboard, forKey: Self.onboardKey) }
    }

    /// Последние запросы: возвращаться к ним куда быстрее, чем набирать снова.
    private(set) var recentSearches: [String] = []

    var torBoxKey: String {
        didSet {
            Keychain.set(torBoxKey, account: Self.torBoxKeyAccount)
            if oldValue != torBoxKey { infuse.accountChanged() }
        }
    }
    var searchAPIURL: String {
        didSet { UserDefaults.standard.set(searchAPIURL, forKey: Self.searchURLKey) }
    }
    var searchAPIToken: String {
        didSet { Keychain.set(searchAPIToken, account: Self.searchTokenAccount) }
    }

    private static let torBoxKeyAccount = "torbox-api-key"
    private static let searchTokenAccount = "search-api-token"
    private static let searchURLKey = "searchAPIURL"
    private static let onboardKey = "didOnboard"
    private static let recentKey = "recentSearches"
    private static let recentLimit = 8

    init() {
        torBoxKey = Keychain.get(account: Self.torBoxKeyAccount) ?? ""
        searchAPIURL = UserDefaults.standard.string(forKey: Self.searchURLKey) ?? ""
        searchAPIToken = Keychain.get(account: Self.searchTokenAccount) ?? ""
        didOnboard = UserDefaults.standard.bool(forKey: Self.onboardKey)
        recentSearches = UserDefaults.standard.stringArray(forKey: Self.recentKey) ?? []
    }

    var hasTorBoxKey: Bool { !torBoxKey.isEmpty }
    var hasSearchAPI: Bool { URL(string: searchAPIURL)?.host() != nil }
    /// Search, cloud downloads, and the local player are available.
    var isFullyConfigured: Bool { hasTorBoxKey && hasSearchAPI && infuse.isInstalled }

    var torBoxClient: TorBoxClient? {
        hasTorBoxKey ? TorBoxClient(apiKey: torBoxKey) : nil
    }

    var searchClient: SearchAPIClient? {
        guard hasSearchAPI, let url = URL(string: searchAPIURL) else { return nil }
        return SearchAPIClient(baseURL: url, token: searchAPIToken)
    }

    // MARK: - История поиска

    func rememberSearch(_ query: String) {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count >= 2 else { return }
        var list = recentSearches.filter { $0.caseInsensitiveCompare(trimmed) != .orderedSame }
        list.insert(trimmed, at: 0)
        recentSearches = Array(list.prefix(Self.recentLimit))
        UserDefaults.standard.set(recentSearches, forKey: Self.recentKey)
    }

    func forgetSearch(_ query: String) {
        recentSearches.removeAll { $0 == query }
        UserDefaults.standard.set(recentSearches, forKey: Self.recentKey)
    }

    func clearRecentSearches() {
        recentSearches = []
        UserDefaults.standard.set(recentSearches, forKey: Self.recentKey)
    }
}
