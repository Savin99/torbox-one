import SwiftUI

/// Экран раздач по запросу: сортировка, фильтры, добавление одним нажатием.
struct TrackerResultsView: View {
    @Environment(AppState.self) private var appState
    @Environment(\.dismiss) private var dismiss
    @Environment(\.dynamicTypeSize) private var typeSize
    let query: String
    /// Готовая модель — только для отладочного демо-экрана.
    private let preloaded: TrackerResultsViewModel?

    @State private var vm: TrackerResultsViewModel
    /// Выбранная раздача: переход по значению, а не вложенной ссылкой.
    @State private var selectedRelease: ReleaseResult?

    init(query: String, preloaded: TrackerResultsViewModel? = nil) {
        self.query = query
        self.preloaded = preloaded
        _vm = State(initialValue: preloaded ?? TrackerResultsViewModel())
    }

    var body: some View {
        ZStack {
            AppBackground()
            content
        }
        .navigationTitle(query)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if vm.phase == .ready, !vm.all.isEmpty {
                ToolbarItem(placement: .topBarTrailing) { filterMenu }
                ToolbarItem(placement: .topBarTrailing) { sortMenu }
            }
        }
        .navigationDestination(item: $selectedRelease) { release in
            ReleaseDetailView(vm: vm, release: release)
        }
        .task {
            guard preloaded == nil else { return }
            guard let client = appState.searchClient else { return }
            await vm.load(client: client, torBox: appState.torBoxClient, query: query)
        }
    }

    // MARK: - Состояния экрана

    @ViewBuilder
    private var content: some View {
        switch vm.phase {
        case .loading:
            loadingView
        case .cancelled:
            EmptyState(
                icon: "xmark.circle",
                title: "Поиск отменён",
                message: "Можно повторить — или уточнить запрос и попробовать снова.",
                actionTitle: "Повторить",
                action: { retry() },
                tint: Theme.idle
            )
        case .failed(let message):
            EmptyState(
                icon: "wifi.exclamationmark",
                title: "Поиск не удался",
                message: message,
                actionTitle: "Повторить",
                action: { retry() },
                tint: Theme.danger
            )
        case .ready:
            if vm.all.isEmpty {
                ScrollView {
                    VStack(spacing: 16) {
                        if vm.allProvidersFailed {
                            EmptyState(
                                icon: "exclamationmark.icloud",
                                title: "Источники не ответили",
                                message: "Ни один трекер не отдал результаты. Проверь сервер поиска и его доступ к трекерам.",
                                actionTitle: "Повторить",
                                action: { retry() },
                                tint: Theme.danger
                            )
                        } else {
                            EmptyState(
                                icon: "questionmark.folder",
                                title: "Ничего не нашлось",
                                message: "Попробуй короче: только название без года и качества. Английское название часто находит больше.",
                                actionTitle: "Изменить запрос",
                                action: { dismiss() },
                                tint: Theme.instant
                            )
                        }
                        if !vm.providerStatus.isEmpty {
                            VStack(alignment: .leading, spacing: 8) {
                                Text("Что ответили источники")
                                    .font(.caption.weight(.semibold))
                                    .foregroundStyle(Theme.textSecondary)
                                ForEach(vm.providerStatus.sorted(by: { $0.key < $1.key }), id: \.key) { name, status in
                                    HStack {
                                        providerChip(name: name, status: status)
                                        Spacer(minLength: 0)
                                    }
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(14)
                            .cardSurface(radius: Theme.radiusM)
                            .padding(.horizontal, Theme.gutter)
                        }
                    }
                    .padding(.bottom, Theme.tabBarInset)
                }
            } else {
                resultsList
            }
        }
    }

    private var loadingView: some View {
        VStack(spacing: 18) {
            VStack(spacing: 10) {
                ProgressView()
                    .controlSize(.large)
                    .tint(Theme.accent)
                Text("Ищу «\(query)» по трекерам")
                    .font(.headline)
                    .foregroundStyle(Theme.textPrimary)
                    .multilineTextAlignment(.center)
                Text("Трекеры отвечают медленно — иногда до минуты.")
                    .font(.footnote)
                    .foregroundStyle(Theme.textSecondary)
                Button("Отменить") {
                    Haptics.tap()
                    dismiss()
                }
                .buttonStyle(SoftButtonStyle(tint: Theme.textSecondary))
                .padding(.top, 2)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 24)
            .padding(.horizontal, 16)
            .cardSurface()
            .padding(.horizontal, Theme.gutter)

            RowsSkeleton(count: 4)
            Spacer(minLength: 0)
        }
        .padding(.top, 12)
    }

    private var resultsList: some View {
        ScrollView {
            LazyVStack(spacing: 12) {
                summaryHeader
                ForEach(vm.visible) { release in
                    ZStack(alignment: .topTrailing) {
                        Button {
                            Haptics.tap()
                            selectedRelease = release
                        } label: {
                            ReleaseCard(release: release, isCached: vm.isCached(release))
                        }
                        .buttonStyle(.plain)
                        ReleaseAddButton(
                            isCached: vm.isCached(release),
                            state: vm.addState(release),
                            onAdd: { add(release) }
                        )
                        .padding(10)
                    }
                }
                if vm.visible.isEmpty {
                    EmptyState(
                        icon: "line.3.horizontal.decrease.circle",
                        title: "Фильтры всё скрыли",
                        message: "Ни одна раздача не подходит под выбранные условия.",
                        actionTitle: "Сбросить фильтры",
                        action: { resetFilters() },
                        tint: Theme.instant
                    )
                }
            }
            .padding(.horizontal, Theme.gutter)
            .padding(.top, 8)
            .padding(.bottom, Theme.tabBarInset)
        }
        .refreshable { retry() }
    }

    /// Сводка: сколько нашли, что ответили источники, что скрыто фильтрами.
    private var summaryHeader: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text("\(vm.visible.count) \(Format.plural(vm.visible.count, "раздача", "раздачи", "раздач"))")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.textPrimary)
                if vm.hiddenByFilters > 0 {
                    Text("· скрыто \(vm.hiddenByFilters)")
                        .font(.subheadline)
                        .foregroundStyle(Theme.textTertiary)
                }
                Spacer(minLength: 0)
                // На крупных шрифтах подпись сортировки только мешает: она есть в тулбаре
                if typeSize < .accessibility1 {
                    Label(vm.sort.title, systemImage: vm.sort.icon)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(Theme.textSecondary)
                }
            }
            if !vm.providerStatus.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        ForEach(vm.providerStatus.sorted(by: { $0.key < $1.key }), id: \.key) { name, status in
                            providerChip(name: name, status: status)
                        }
                    }
                }
            }
        }
        .padding(.bottom, 2)
    }

    private func providerChip(name: String, status: String) -> some View {
        let failed = status.lowercased().contains("ошиб")
        let empty = status == "0"
        return Chip(
            text: "\(name): \(status)",
            icon: failed ? "exclamationmark.triangle.fill" : empty ? "circle.dashed" : "checkmark.circle.fill",
            style: .tinted(failed ? Theme.danger : empty ? Theme.idle : Theme.accent),
            accessibility: failed
                ? "Источник \(name) ответил ошибкой"
                : "Источник \(name): \(status) результатов"
        )
    }

    // MARK: - Меню

    private var sortMenu: some View {
        Menu {
            Picker("Сортировка", selection: Binding(
                get: { vm.sort },
                set: { vm.sort = $0; Haptics.tap() }
            )) {
                ForEach(TrackerResultsViewModel.Sort.allCases) { option in
                    Label(option.title, systemImage: option.icon).tag(option)
                }
            }
        } label: {
            Image(systemName: "arrow.up.arrow.down")
                .hitTarget(36)
        }
        .accessibilityLabel("Сортировка: \(vm.sort.title)")
    }

    private var filterMenu: some View {
        Menu {
            Toggle(isOn: Binding(get: { vm.onlyCached }, set: { vm.onlyCached = $0; Haptics.tap() })) {
                Label("Только мгновенные", systemImage: "bolt.fill")
            }
            Toggle(isOn: Binding(get: { vm.onlyHD }, set: { vm.onlyHD = $0; Haptics.tap() })) {
                Label("1080p и выше", systemImage: "4k.tv")
            }
            Toggle(isOn: Binding(get: { vm.onlyVoiceover }, set: { vm.onlyVoiceover = $0; Haptics.tap() })) {
                Label("С озвучкой", systemImage: "waveform")
            }
            if vm.activeFilterCount > 0 {
                Divider()
                Button("Сбросить", systemImage: "arrow.counterclockwise") { resetFilters() }
            }
        } label: {
            Image(systemName: vm.activeFilterCount > 0
                ? "line.3.horizontal.decrease.circle.fill"
                : "line.3.horizontal.decrease.circle")
                .hitTarget(36)
        }
        .accessibilityLabel(vm.activeFilterCount > 0 ? "Фильтры: включено \(vm.activeFilterCount)" : "Фильтры")
    }

    // MARK: - Действия

    private func resetFilters() {
        withAnimation {
            vm.onlyCached = false
            vm.onlyHD = false
            vm.onlyVoiceover = false
        }
    }

    private func retry() {
        guard let client = appState.searchClient else { return }
        Task {
            await vm.load(client: client, torBox: appState.torBoxClient, query: query)
        }
    }

    private func add(_ release: ReleaseResult) {
        guard let search = appState.searchClient else { return }
        guard let torBox = appState.torBoxClient else {
            appState.toasts.error(
                "Нет ключа TorBox",
                message: "Подключи TorBox — и раздачи будут добавляться в один тап.",
                actionTitle: "Настройки",
                action: { appState.selectedTab = .settings }
            )
            return
        }
        Haptics.press()
        Task {
            let ok = await vm.add(release: release, client: search, torBox: torBox)
            if ok {
                appState.toasts.success(
                    "Добавлено в TorBox",
                    message: release.title,
                    actionTitle: "Открыть",
                    action: { appState.selectedTab = .library }
                )
            } else if case .failed(let message) = vm.addState(release) {
                appState.toasts.error("Не удалось добавить", message: message)
            }
        }
    }
}
