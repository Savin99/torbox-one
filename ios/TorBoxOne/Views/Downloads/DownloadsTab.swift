import SwiftUI

struct DownloadsTab: View {
    @Environment(AppState.self) private var appState
    private var vm: DownloadsViewModel { appState.library }
    @State private var pendingDelete: TBTorrent?
    /// Программный переход в раздачу из постерной сетки.
    @State private var openTorrentID: Int?
    @State private var magnetSheet = false
    /// Вид библиотеки: строки или постеры. Запоминаем между запусками.
    @AppStorage("downloadsShowsPosters") private var showsPosters = false

    var body: some View {
        NavigationStack {
            ZStack {
                AppBackground()
                if let client = appState.torBoxClient {
                    content(client: client)
                } else {
                    EmptyState(
                        icon: "key.horizontal",
                        title: "TorBox не подключён",
                        message: "Подключи свой аккаунт TorBox для библиотеки и просмотра в Infuse. Ключ вводится один раз.",
                        actionTitle: "Подключить",
                        action: { appState.selectedTab = .settings },
                        tint: Theme.instant
                    )
                }
            }
            .navigationTitle("Библиотека")
            .navigationDestination(for: Int.self) { torrentID in
                TorrentDetailView(vm: vm, torrentID: torrentID)
            }
            .navigationDestination(isPresented: Binding(
                get: { openTorrentID != nil },
                set: { if !$0 { openTorrentID = nil } }
            )) {
                if let openTorrentID {
                    TorrentDetailView(vm: vm, torrentID: openTorrentID)
                }
            }
            .navigationDestination(for: NotificationsRoute.self) { _ in
                NotificationsView(vm: vm)
            }
        }
        .sheet(isPresented: $magnetSheet) {
            AddMagnetSheet(vm: vm)
        }
        .task(id: appState.pendingMagnet) {
            // task(id:) срабатывает и при появлении экрана: магнет мог прийти,
            // пока вкладка «Библиотека» ещё не была создана
            if appState.pendingMagnet != nil {
                magnetSheet = true
            }
        }
    }

    // MARK: - Содержимое

    @ViewBuilder
    private func content(client: TorBoxClient) -> some View {
        Group {
            if !vm.hasLoaded {
                ScrollView {
                    RowsSkeleton(count: 5).padding(.top, 8)
                }
            } else if vm.torrents.isEmpty {
                if let error = vm.loadError {
                    EmptyState(
                        icon: "wifi.exclamationmark",
                        title: "TorBox не отвечает",
                        message: error,
                        actionTitle: "Повторить",
                        action: {
                            Task { await vm.refresh(client: client, bypassCache: true) }
                        },
                        tint: Theme.danger
                    )
                } else {
                    EmptyState(
                        icon: "tray",
                        title: "Пока пусто",
                        message: "Найди фильм или сериал — и он появится здесь вместе с прогрессом закачки.",
                        actionTitle: "На главную",
                        action: { appState.selectedTab = .home }
                    )
                }
            } else if showsPosters {
                grid(client: client)
            } else {
                list(client: client)
            }
        }
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                NavigationLink(value: NotificationsRoute.list) {
                    Image(systemName: vm.unreadNotifications > 0 ? "bell.badge.fill" : "bell")
                        .hitTarget(36)
                }
                .accessibilityLabel(
                    vm.unreadNotifications > 0
                        ? "Уведомления, новых: \(vm.unreadNotifications)"
                        : "Уведомления"
                )
            }
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Picker("Сортировка", selection: Binding(
                        get: { vm.sort },
                        set: { vm.sort = $0; Haptics.tap() }
                    )) {
                        ForEach(DownloadsViewModel.Sort.allCases) { option in
                            Label(option.title, systemImage: option.icon).tag(option)
                        }
                    }
                } label: {
                    Image(systemName: "arrow.up.arrow.down")
                        .hitTarget(36)
                }
                .accessibilityLabel("Сортировка: \(vm.sort.title)")
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    Haptics.tap()
                    withAnimation { showsPosters.toggle() }
                } label: {
                    Image(systemName: showsPosters ? "list.bullet" : "square.grid.2x2")
                        .hitTarget(36)
                }
                .accessibilityLabel(showsPosters ? "Показать списком" : "Показать постерами")
            }
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Button {
                        magnetSheet = true
                    } label: {
                        Label("Добавить магнет", systemImage: "link.badge.plus")
                    }
                    Button {
                        Task { await vm.refresh(client: client, bypassCache: true) }
                    } label: {
                        Label("Обновить сейчас", systemImage: "arrow.clockwise")
                    }
                    NavigationLink(value: NotificationsRoute.list) {
                        Label("Уведомления TorBox", systemImage: "bell")
                    }
                } label: {
                    Image(systemName: "ellipsis.circle")
                        .hitTarget(36)
                }
                .accessibilityLabel("Ещё")
            }
        }
        .task(id: appState.torBoxKey) {
            await vm.pollLoop(client: client)
        }
        .task {
            // Уведомления нужны сразу: по ним считается бейдж на колокольчике
            await vm.loadNotifications(client: client)
        }
        .confirmationDialog(
            "Удалить «\(pendingDelete?.name ?? "")» из TorBox? Файлы в облаке исчезнут, скачанное на телефоне останется.",
            isPresented: Binding(get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } }),
            titleVisibility: .visible
        ) {
            Button("Удалить навсегда", role: .destructive) {
                if let torrent = pendingDelete {
                    remove(torrent, client: client)
                }
                pendingDelete = nil
            }
            Button("Отмена", role: .cancel) { pendingDelete = nil }
        }
    }

    private func list(client: TorBoxClient) -> some View {
        List {
            if vm.activeCount > 0 {
                Section {
                    SummaryCard(vm: vm)
                        .plainListRow(insets: EdgeInsets(top: 4, leading: Theme.gutter, bottom: 4, trailing: Theme.gutter))
                }
            }

            if let error = vm.loadError {
                Section {
                    InlineWarning(text: error)
                        .plainListRow()
                }
            }

            Section { InfuseLibraryCard().plainListRow() }

            if !vm.activeTorrents.isEmpty {
                Section {
                    ForEach(vm.activeTorrents) { torrent in
                        row(torrent: torrent, client: client)
                    }
                } header: {
                    SectionHeader(
                        title: "Качаются",
                        subtitle: vm.totalSpeed > 0 ? "суммарно \(Format.speed(vm.totalSpeed))" : nil
                    )
                    .textCase(nil)
                    .plainListRow(insets: EdgeInsets(top: 12, leading: Theme.gutter, bottom: 4, trailing: Theme.gutter))
                }
            }

            if !vm.readyTorrents.isEmpty {
                Section {
                    ForEach(vm.readyTorrents) { torrent in
                        row(torrent: torrent, client: client)
                    }
                } header: {
                    SectionHeader(
                        title: "Готово",
                        subtitle: "\(vm.readyTorrents.count) \(Format.plural(vm.readyTorrents.count, "раздача", "раздачи", "раздач"))"
                    )
                    .textCase(nil)
                    .plainListRow(insets: EdgeInsets(top: 12, leading: Theme.gutter, bottom: 4, trailing: Theme.gutter))
                }
            }

            if vm.filtered.isEmpty, !vm.filter.isEmpty {
                Section {
                    EmptyState(
                        icon: "magnifyingglass",
                        title: "Ничего не найдено",
                        message: "В твоих загрузках нет раздач с таким названием.",
                        tint: Theme.idle
                    )
                    .plainListRow()
                }
            }

            Section {
                Color.clear.frame(height: Theme.tabBarInset)
                    .plainListRow(insets: EdgeInsets())
            }
        }
        .themedList()
        .searchable(text: Binding(get: { vm.filter }, set: { vm.filter = $0 }), prompt: "Название раздачи")
        .refreshable {
            await vm.refresh(client: client, bypassCache: true)
        }
    }

    /// Постерная сетка: активные сверху, поиск и обновление работают так же.
    private func grid(client: TorBoxClient) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if vm.activeCount > 0 {
                    SummaryCard(vm: vm)
                        .padding(.horizontal, Theme.gutter)
                }
                if let error = vm.loadError {
                    InlineWarning(text: error)
                        .padding(.horizontal, Theme.gutter)
                }
                InfuseLibraryCard().padding(.horizontal, Theme.gutter)
                DownloadsGrid(
                    torrents: vm.filtered,
                    onOpen: { torrent in openTorrentID = torrent.id },
                    onDelete: { torrent in pendingDelete = torrent },
                    onDownload: { torrent in
                        if !DownloadSelection.videoFiles(of: torrent).isEmpty {
                            openTorrentID = torrent.id
                        }
                    }
                )
                if vm.filtered.isEmpty, !vm.filter.isEmpty {
                    EmptyState(
                        icon: "magnifyingglass",
                        title: "Ничего не найдено",
                        message: "В твоих загрузках нет раздач с таким названием.",
                        tint: Theme.idle
                    )
                }
            }
            .padding(.top, 8)
            .padding(.bottom, Theme.tabBarInset)
        }
        .searchable(text: Binding(get: { vm.filter }, set: { vm.filter = $0 }), prompt: "Название раздачи")
        .refreshable {
            await vm.refresh(client: client, bypassCache: true)
        }
    }

    private func row(torrent: TBTorrent, client: TorBoxClient) -> some View {
        NavigationLink(value: torrent.id) {
            TorrentCard(
                torrent: torrent,
                localFiles: localFileCount(torrent),
                isBusy: vm.busyIDs.contains(torrent.id)
            )
        }
        .buttonStyle(.plain)
        .plainListRow()
        .swipeActions(edge: .leading, allowsFullSwipe: false) {
            leadingActions(client: client, torrent: torrent)
        }
        .swipeActions(edge: .trailing) {
            Button("Удалить", systemImage: "trash", role: .destructive) {
                Haptics.warning()
                pendingDelete = torrent
            }
        }
        .contextMenu {
            if !DownloadSelection.videoFiles(of: torrent).isEmpty,
               TorrentStatus(torrent: torrent).isFinished {
                Button {
                    openTorrentID = torrent.id
                } label: {
                    Label("Выбрать для скачивания", systemImage: "arrow.down.to.line")
                }
            }
            Button(role: .destructive) {
                pendingDelete = torrent
            } label: {
                Label("Удалить из TorBox", systemImage: "trash")
            }
        }
    }

    @ViewBuilder
    private func leadingActions(client: TorBoxClient, torrent: TBTorrent) -> some View {
        let status = TorrentStatus(torrent: torrent)
        if status.isFinished,
           !DownloadSelection.videoFiles(of: torrent).isEmpty {
            Button("Скачать", systemImage: "arrow.down.to.line") {
                Haptics.press()
                openTorrentID = torrent.id
            }
            .tint(Theme.accent)
        }
        if status.kind == .paused {
            Button("Продолжить", systemImage: "play.fill") {
                run(client: client, id: torrent.id, operation: .resume, message: "Загрузка продолжена")
            }
            .tint(Theme.accent)
        }
        if !status.isFinished {
            Button("Анонс", systemImage: "antenna.radiowaves.left.and.right") {
                run(client: client, id: torrent.id, operation: .reannounce, message: "Реанонс отправлен")
            }
            .tint(Theme.info)
        } else {
            Button("Стоп-сид", systemImage: "stop.circle") {
                run(client: client, id: torrent.id, operation: .stopSeeding, message: "Раздача остановлена")
            }
            .tint(Theme.instant)
        }
    }

    // MARK: - Действия

    private func localFileCount(_ torrent: TBTorrent) -> Int {
        appState.downloads.items.values.filter { $0.torrentID == torrent.id && $0.state == .done }.count
    }

    private func run(client: TorBoxClient, id: Int, operation: TorrentOperation, message: String) {
        Haptics.tap()
        Task {
            let ok = await vm.control(client: client, id: id, operation: operation)
            if ok {
                appState.toasts.success(message)
            } else if let error = vm.loadError {
                appState.toasts.error("Не получилось", message: error)
            }
        }
    }

    private func remove(_ torrent: TBTorrent, client: TorBoxClient) {
        Task {
            let ok = await vm.control(client: client, id: torrent.id, operation: .delete)
            if ok {
                appState.toasts.success("Удалено из TorBox", message: torrent.name)
            } else if let error = vm.loadError {
                appState.toasts.error("Не удалось удалить", message: error)
            }
        }
    }
}

/// Отдельный тип маршрута: Int уже занят идентификатором раздачи.
enum NotificationsRoute: Hashable {
    case list
}

/// Сводка сверху: что происходит с аккаунтом прямо сейчас.
struct SummaryCard: View {
    let vm: DownloadsViewModel

    var body: some View {
        HStack(spacing: 14) {
            ZStack {
                Circle()
                    .fill((vm.activeCount > 0 ? Theme.info : Theme.accent).opacity(0.16))
                    .frame(width: 46, height: 46)
                Image(systemName: vm.activeCount > 0 ? "arrow.down.circle.fill" : "checkmark.circle.fill")
                    .font(.title2)
                    .foregroundStyle(vm.activeCount > 0 ? Theme.info : Theme.accent)
            }
            VStack(alignment: .leading, spacing: 3) {
                Text(headline)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.textPrimary)
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(Theme.textSecondary)
            }
            Spacer(minLength: 0)
        }
        .padding(14)
        .cardSurface()
        .accessibilityElement(children: .combine)
    }

    private var headline: String {
        if vm.activeCount > 0 {
            return "\(vm.activeCount) \(Format.plural(vm.activeCount, "раздача качается", "раздачи качаются", "раздач качаются"))"
        }
        return "Всё скачано"
    }

    private var detail: String {
        var parts: [String] = []
        if vm.totalSpeed > 0 { parts.append(Format.speed(vm.totalSpeed)) }
        parts.append("\(vm.torrents.count) \(Format.plural(vm.torrents.count, "раздача", "раздачи", "раздач"))")
        if vm.totalSize > 0 { parts.append(Format.bytes(vm.totalSize)) }
        if let refresh = vm.lastRefresh {
            parts.append("обновлено \(Format.relative(refresh))")
        }
        return parts.joined(separator: " · ")
    }
}

/// Плашка предупреждения внутри списка.
struct InlineWarning: View {
    let text: String

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(Theme.danger)
            Text(text)
                .font(.footnote)
                .foregroundStyle(Theme.textSecondary)
            Spacer(minLength: 0)
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: Theme.radiusM, style: .continuous)
                .fill(Theme.danger.opacity(0.12))
        )
    }
}
