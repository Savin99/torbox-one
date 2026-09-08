import SwiftUI

/// Куда ведёт «Главная»: результаты поиска или карточка раздачи.
enum HomeRoute: Hashable {
    case results(String)
    case torrent(Int)
}

/// Главный экран: сначала то, что хочется продолжить, потом библиотека
/// и каталог. Поиск — тут же, в строке сверху.
struct HomeTab: View {
    @Environment(AppState.self) private var appState
    @State private var vm = SearchViewModel()
    @State private var searchText = ""
    @State private var path: [HomeRoute] = []

    private var library: DownloadsViewModel { appState.library }

    var body: some View {
        NavigationStack(path: $path) {
            ZStack {
                AppBackground()
                content
            }
            .navigationTitle("Главная")
            .navigationDestination(for: HomeRoute.self) { route in
                switch route {
                case .results(let query):
                    TrackerResultsView(query: query)
                case .torrent(let id):
                    TorrentDetailView(vm: library, torrentID: id)
                }
            }
        }
    }

    // MARK: - Содержимое

    private var content: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 22) {
                if searchText.trimmingCharacters(in: .whitespaces).isEmpty {
                    idleContent
                } else {
                    typingContent
                }
            }
            .padding(.top, 8)
            .padding(.bottom, Theme.tabBarInset)
        }
        .scrollDismissesKeyboard(.immediately)
        .refreshable {
            if let client = appState.torBoxClient {
                await library.refresh(client: client, bypassCache: true)
            }
            if let client = appState.searchClient {
                await vm.loadPopular(client: client, force: true)
            }
        }
        .searchable(
            text: $searchText,
            placement: .navigationBarDrawer(displayMode: .always),
            prompt: "Фильм или сериал"
        )
        .textInputAutocapitalization(.sentences)
        .autocorrectionDisabled()
        .submitLabel(.search)
        .onSubmit(of: .search) {
            run(searchText)
        }
        .task(id: appState.torBoxKey) {
            guard let client = appState.torBoxClient else { return }
            await library.pollLoop(client: client)
        }
        .task(id: appState.searchAPIURL) {
            guard let client = appState.searchClient else { return }
            await vm.loadPopular(client: client)
        }
        .task(id: searchText) {
            guard let client = appState.searchClient else { return }
            // Дебаунс 350 мс: каждый новый символ перезапускает задачу
            try? await Task.sleep(for: .milliseconds(350))
            guard !Task.isCancelled else { return }
            await vm.catalogSearch(client: client, query: searchText)
        }
    }

    // MARK: Спокойное состояние

    @ViewBuilder
    private var idleContent: some View {
        if !appState.hasTorBoxKey {
            setupCard(
                icon: "key.horizontal",
                title: "Подключи TorBox",
                message: "Подключи свой аккаунт, чтобы открыть библиотеку и смотреть в Infuse.",
                tint: Theme.instant
            )
        }

        InfuseLibraryCard().padding(.horizontal, Theme.gutter)

        recentlyAdded

        if appState.searchClient == nil {
            EmptyState(
                icon: "antenna.radiowaves.left.and.right.slash",
                title: "Сервер поиска не подключён",
                message: "Каталог и поиск по трекерам живут на мини-API. Укажи его адрес — это одна строка в настройках.",
                actionTitle: "Открыть настройки",
                action: { appState.selectedTab = .settings },
                tint: Theme.violet
            )
        } else {
            recentSearches
            popular
        }
    }

    /// Приглашение донастроить приложение — прямо на главной, а не в пустых экранах.
    private func setupCard(icon: String, title: String, message: String, tint: Color) -> some View {
        Button {
            Haptics.tap()
            appState.selectedTab = .settings
        } label: {
            HStack(spacing: 12) {
                Image(systemName: icon)
                    .font(.headline)
                    .foregroundStyle(tint)
                    .frame(width: 40, height: 40)
                    .background(Circle().fill(tint.opacity(0.15)))
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.textPrimary)
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.bold))
                    .foregroundStyle(Theme.textTertiary)
            }
            .padding(14)
            .cardSurface(radius: Theme.radiusM)
        }
        .buttonStyle(.plain)
        .padding(.horizontal, Theme.gutter)
    }

    /// Свежие готовые раздачи постерами: быстрый путь к тому, что только скачалось.
    @ViewBuilder
    private var recentlyAdded: some View {
        let items = Array(library.readyTorrents.prefix(10))
        if !items.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                SectionHeader(
                    title: "Недавно добавленное",
                    subtitle: "готово к просмотру",
                    actionTitle: "Вся библиотека",
                    action: { appState.selectedTab = .library }
                )
                .padding(.horizontal, Theme.gutter)
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(alignment: .top, spacing: 12) {
                        ForEach(items) { torrent in
                            posterTile(torrent)
                        }
                    }
                    .padding(.horizontal, Theme.gutter)
                    .padding(.vertical, 2)
                }
            }
        }
    }

    private func posterTile(_ torrent: TBTorrent) -> some View {
        let meta = appState.meta.meta(for: torrent.name)
        return Button {
            Haptics.tap()
            path.append(.torrent(torrent.id))
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                PosterImage(
                    url: meta?.posterUrl ?? "",
                    fallbackTitle: meta?.title ?? (torrent.name ?? "")
                )
                .frame(width: 108)
                .overlay(alignment: .topTrailing) {
                    if let rating = meta?.ratingValue {
                        RatingBadge(value: rating)
                            .padding(4)
                    }
                }
                Text(meta?.title.isEmpty == false ? meta!.title : (torrent.name ?? ""))
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                    .frame(width: 108, alignment: .leading)
            }
        }
        .buttonStyle(.plain)
        .task {
            await appState.meta.load(name: torrent.name, client: appState.searchClient)
        }
        .contextMenu {
            if !DownloadSelection.videoFiles(of: torrent).isEmpty {
                Button {
                    Haptics.press()
                    path.append(.torrent(torrent.id))
                } label: {
                    Label("Выбрать для скачивания", systemImage: "arrow.down.to.line")
                }
            }
            Button {
                path.append(.torrent(torrent.id))
            } label: {
                Label("Открыть раздачу", systemImage: "info.circle")
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(meta?.title ?? torrent.name ?? "Раздача")
        .accessibilityAddTraits(.isButton)
    }

    @ViewBuilder
    private var recentSearches: some View {
        if !appState.recentSearches.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                SectionHeader(title: "Недавние запросы", actionTitle: "Очистить") {
                    withAnimation { appState.clearRecentSearches() }
                }
                .padding(.horizontal, Theme.gutter)
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(appState.recentSearches, id: \.self) { query in
                            Button {
                                run(query)
                            } label: {
                                HStack(spacing: 6) {
                                    Image(systemName: "clock.arrow.circlepath")
                                        .font(.caption)
                                    Text(query)
                                        .font(.subheadline.weight(.medium))
                                        .lineLimit(1)
                                }
                                .padding(.horizontal, 14)
                                .frame(height: 38)
                                .background(Capsule().fill(Theme.hairline.opacity(0.08)))
                                .foregroundStyle(Theme.textPrimary)
                            }
                            .buttonStyle(.plain)
                            .contextMenu {
                                Button("Убрать из недавних", systemImage: "xmark") {
                                    withAnimation { appState.forgetSearch(query) }
                                }
                            }
                        }
                    }
                    .padding(.horizontal, Theme.gutter)
                }
            }
        }
    }

    @ViewBuilder
    private var popular: some View {
        switch vm.popularPhase {
        case .idle, .loading:
            VStack(alignment: .leading, spacing: 12) {
                SectionHeader(title: "Сейчас популярно")
                    .padding(.horizontal, Theme.gutter)
                PosterGridSkeleton()
            }
        case .loaded:
            if vm.catalogUnavailable {
                EmptyState(
                    icon: "film.stack",
                    title: "Каталог выключен",
                    message: "На сервере поиска не задан ключ Кинопоиска. Поиск по трекерам работает: введи название в строке сверху.",
                    tint: Theme.violet
                )
            } else {
                VStack(alignment: .leading, spacing: 12) {
                    SectionHeader(title: "Сейчас популярно", subtitle: "нажми постер — соберу раздачи по трекерам")
                        .padding(.horizontal, Theme.gutter)
                    CatalogGrid(items: vm.popular) { item in
                        run(item.searchQuery)
                    }
                }
            }
        case .failed(let message):
            EmptyState(
                icon: "wifi.exclamationmark",
                title: "Каталог не отвечает",
                message: message,
                actionTitle: "Повторить",
                action: {
                    guard let client = appState.searchClient else { return }
                    Task { await vm.loadPopular(client: client, force: true) }
                },
                tint: Theme.danger
            )
        }
    }

    // MARK: Пользователь набирает запрос

    @ViewBuilder
    private var typingContent: some View {
        let query = searchText.trimmingCharacters(in: .whitespaces)

        Button {
            run(query)
        } label: {
            HStack(spacing: 12) {
                Image(systemName: "magnifyingglass")
                    .font(.headline)
                    .foregroundStyle(Theme.accent)
                    .frame(width: 40, height: 40)
                    .background(Circle().fill(Theme.accent.opacity(0.15)))
                VStack(alignment: .leading, spacing: 2) {
                    Text("Искать «\(query)» по трекерам")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.textPrimary)
                        .lineLimit(2)
                    Text("Раздачи со всех настроенных источников")
                        .font(.caption)
                        .foregroundStyle(Theme.textSecondary)
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.bold))
                    .foregroundStyle(Theme.textTertiary)
            }
            .padding(14)
            .cardSurface(radius: Theme.radiusM)
        }
        .buttonStyle(.plain)
        .padding(.horizontal, Theme.gutter)

        // Уже скачанное по этому запросу — быстрее, чем идти на трекеры
        let matches = library.torrents.filter {
            ($0.name ?? "").localizedCaseInsensitiveContains(query)
                || (appState.meta.meta(for: $0.name)?.title ?? "").localizedCaseInsensitiveContains(query)
        }
        if !matches.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                SectionHeader(title: "Уже в твоём TorBox")
                    .padding(.horizontal, Theme.gutter)
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(alignment: .top, spacing: 12) {
                        ForEach(matches.prefix(8)) { torrent in
                            posterTile(torrent)
                        }
                    }
                    .padding(.horizontal, Theme.gutter)
                }
            }
        }

        if appState.searchClient != nil {
            if vm.isMatching && vm.matches.isEmpty {
                PosterGridSkeleton(count: 4)
            } else if !vm.matches.isEmpty {
                VStack(alignment: .leading, spacing: 12) {
                    SectionHeader(title: "Найдено в каталоге", subtitle: "точное название и год — лучший результат")
                        .padding(.horizontal, Theme.gutter)
                    CatalogGrid(items: vm.matches) { item in
                        run(item.searchQuery)
                    }
                }
            } else if query.count >= 2 {
                Text("В каталоге ничего не нашлось — можно искать по трекерам напрямую кнопкой сверху.")
                    .font(.footnote)
                    .foregroundStyle(Theme.textTertiary)
                    .padding(.horizontal, Theme.gutter)
            }
        }
    }

    // MARK: - Действия

    private func run(_ query: String) {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count >= 2 else { return }
        Haptics.press()
        appState.rememberSearch(trimmed)
        path.append(.results(trimmed))
    }
}
