import SwiftUI

/// Карточка раздачи: обложка тайтла, описание, состав, серии и техданные.
/// Метаданные подтягиваются по имени раздачи через мини-API.
struct TorrentDetailView: View {
    @Environment(AppState.self) private var appState
    @Environment(\.openURL) private var openURL
    @Environment(\.dismiss) private var dismiss
    let vm: DownloadsViewModel
    let torrentID: Int

    @State private var busyFileID: Int?
    @State private var shareURL: URL?
    @State private var playerQueue: PlayerQueue?
    @State private var fileFilter = ""
    @State private var confirmDelete = false
    @State private var showFullOverview = false
    /// Выбранный сезон: у сериала на 73 файла список иначе не читается.
    @State private var season: Int?
    @State private var didPickSeason = false

    private var torrent: TBTorrent? { vm.torrent(id: torrentID) }
    private var meta: TitleMeta? { appState.meta.meta(for: torrent?.name) }

    var body: some View {
        ZStack {
            AppBackground()
            if let torrent {
                detail(torrent: torrent)
            } else {
                EmptyState(
                    icon: "questionmark.circle",
                    title: "Раздача не найдена",
                    message: "Похоже, её удалили из TorBox.",
                    actionTitle: "Назад",
                    action: { dismiss() },
                    tint: Theme.idle
                )
            }
        }
        .navigationTitle(meta?.title ?? "Раздача")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(.hidden, for: .navigationBar)
        .toolbar {
            if let torrent {
                ToolbarItem(placement: .topBarTrailing) {
                    menu(torrent: torrent)
                }
            }
        }
        .task(id: torrent?.name) {
            await appState.meta.load(name: torrent?.name, client: appState.searchClient)
        }
        .task(id: meta?.sourceId) {
            guard let meta, meta.isSeries else { return }
            await appState.meta.loadEpisodes(titleID: meta.sourceId, client: appState.searchClient)
        }
        .sheet(item: Binding(
            get: { shareURL.map(ShareItem.init) },
            set: { shareURL = $0?.url }
        )) { item in
            ShareSheet(url: item.url)
        }
        .fullScreenCover(item: $playerQueue) { queue in
            PlayerScreen(queue: queue, thumbnails: appState.thumbnails)
        }
        .confirmationDialog(
            "Удалить раздачу из TorBox? Скачанное на телефоне останется.",
            isPresented: $confirmDelete,
            titleVisibility: .visible
        ) {
            Button("Удалить навсегда", role: .destructive) { deleteTorrent() }
            Button("Отмена", role: .cancel) {}
        }
    }

    // MARK: - Каркас

    private func detail(torrent: TBTorrent) -> some View {
        let status = TorrentStatus(torrent: torrent)
        let videos = PlayerQueueFactory.videoFiles(of: torrent)
        return ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                BackdropHeader(
                    meta: meta,
                    fallbackTitle: torrent.name ?? "Без названия",
                    height: 300
                )

                VStack(alignment: .leading, spacing: 18) {
                    if status.isFinished, !videos.isEmpty {
                        watchActions(torrent: torrent, videos: videos)
                    } else {
                        progressCard(torrent: torrent, status: status)
                    }
                    overviewCard(torrent: torrent, status: status)
                    statsGrid(torrent: torrent, status: status)
                    if let meta, !meta.cast.isEmpty {
                        CastRow(people: meta.cast)
                    }
                    filesSection(torrent: torrent, status: status)
                    techCard(torrent: torrent, videos: videos)
                }
                .padding(.horizontal, Theme.gutter)
            }
            .padding(.bottom, Theme.tabBarInset)
        }
        .ignoresSafeArea(edges: .top)
    }

    // MARK: - Действия

    @ViewBuilder
    private func watchActions(torrent: TBTorrent, videos: [TBFile]) -> some View {
        let suggested = PlayerQueueFactory.suggestedFile(torrent: torrent, appState: appState)
        VStack(spacing: 10) {
            if let file = suggested {
                let entry = appState.playback.entry(torrent: torrent, fileID: file.id)
                Button {
                    watch(torrent: torrent, file: file)
                } label: {
                    HStack(spacing: 10) {
                        Image(systemName: "play.fill")
                        VStack(alignment: .leading, spacing: 1) {
                            Text(entry?.isResumable == true ? "Продолжить" : "Смотреть")
                                .font(.headline)
                            Text(shortLabel(torrent: torrent, file: file, entry: entry))
                                .font(.caption)
                                .foregroundStyle(.white.opacity(0.85))
                                .lineLimit(1)
                        }
                        Spacer(minLength: 0)
                        if videos.count > 1 {
                            Text("\(videos.count) \(Format.plural(videos.count, "серия", "серии", "серий"))")
                                .font(.caption.weight(.semibold))
                                .padding(.horizontal, 8)
                                .padding(.vertical, 4)
                                .background(Capsule().fill(.white.opacity(0.22)))
                        }
                    }
                }
                .buttonStyle(AccentButtonStyle())

                HStack(spacing: 10) {
                    if Infuse.isInstalled {
                        iconAction(icon: "play.circle", label: "Infuse") {
                            playInInfuse(torrent: torrent, file: file)
                        }
                    }
                    iconAction(icon: "square.and.arrow.up", label: "Ссылка") {
                        share(torrent: torrent, file: file)
                    }
                    iconAction(icon: "arrow.down.to.line", label: "В телефон") {
                        startDownload(torrent: torrent, file: file)
                    }
                    iconAction(icon: "trash", label: "Удалить", tint: Theme.danger) {
                        Haptics.warning()
                        confirmDelete = true
                    }
                }
            }
        }
    }

    private func iconAction(
        icon: String,
        label: String,
        tint: Color = Theme.textPrimary,
        action: @escaping () -> Void
    ) -> some View {
        Button {
            action()
        } label: {
            VStack(spacing: 5) {
                Image(systemName: icon)
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(tint)
                    .frame(width: 44, height: 44)
                    .background(Circle().fill(Theme.hairline.opacity(0.08)))
                Text(label)
                    .font(.caption2)
                    .foregroundStyle(Theme.textSecondary)
            }
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }

    private func progressCard(torrent: TBTorrent, status: TorrentStatus) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                StatusPill(status: status)
                if status.isActive, let speed = torrent.downloadSpeed, speed > 0 {
                    Chip(text: Format.speed(speed), icon: "speedometer", style: .tinted(Theme.info))
                }
                Spacer(minLength: 0)
            }
            SlimProgressBar(fraction: Format.progressFraction(torrent.progress), tint: status.tint, height: 6)
            HStack {
                Text("\(Int(Format.progressFraction(torrent.progress) * 100))% готово")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(status.tint)
                    .contentTransition(.numericText())
                Spacer()
                if let eta = torrent.eta, eta > 0, status.isActive {
                    Text("осталось \(Format.eta(eta))")
                        .font(.caption)
                        .foregroundStyle(Theme.textSecondary)
                }
            }
            Text("Смотреть можно, когда TorBox закончит закачку.")
                .font(.caption)
                .foregroundStyle(Theme.textTertiary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .cardSurface()
    }

    // MARK: - Описание

    @ViewBuilder
    private func overviewCard(torrent: TBTorrent, status: TorrentStatus) -> some View {
        if let meta {
            VStack(alignment: .leading, spacing: 10) {
                if !meta.tagline.isEmpty {
                    Text(meta.tagline)
                        .font(.callout.weight(.medium))
                        .foregroundStyle(Theme.textPrimary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !meta.overview.isEmpty {
                    Text(meta.overview)
                        .font(.callout)
                        .foregroundStyle(Theme.textSecondary)
                        .lineLimit(showFullOverview ? nil : 4)
                        .fixedSize(horizontal: false, vertical: true)
                    Button(showFullOverview ? "Свернуть" : "Ещё") {
                        Haptics.tap()
                        withAnimation { showFullOverview.toggle() }
                    }
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.accent)
                }
                HStack(spacing: 10) {
                    RatingBadgeRow(meta: meta)
                    if !meta.countries.isEmpty {
                        Text(meta.countries.prefix(2).joined(separator: ", "))
                            .font(.caption)
                            .foregroundStyle(Theme.textTertiary)
                    }
                    Spacer(minLength: 0)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .cardSurface()
        } else if appState.meta.isUnmatched(torrent.name) {
            HStack(spacing: 10) {
                Image(systemName: "questionmark.folder")
                    .foregroundStyle(Theme.textTertiary)
                Text("Каталог не узнал эту раздачу — постера и описания не будет.")
                    .font(.footnote)
                    .foregroundStyle(Theme.textSecondary)
                Spacer(minLength: 0)
            }
            .padding(14)
            .cardSurface(radius: Theme.radiusM)
        } else if appState.searchClient != nil {
            HStack(spacing: 10) {
                ProgressView()
                Text("Ищу описание и постер…")
                    .font(.footnote)
                    .foregroundStyle(Theme.textSecondary)
                Spacer(minLength: 0)
            }
            .padding(14)
            .cardSurface(radius: Theme.radiusM)
        }
    }

    private func shortLabel(torrent: TBTorrent, file: TBFile, entry: PlaybackStore.Entry?) -> String {
        var parts: [String] = []
        if let label = file.episode.label { parts.append(label) }
        parts.append(episodeTitle(torrent: torrent, file: file))
        if let entry, entry.isResumable {
            parts.append("с \(Format.clock(entry.positionSeconds))")
        }
        return parts.joined(separator: " · ")
    }

    /// Название серии из каталога, иначе — вычищенное имя файла.
    private func episodeTitle(torrent: TBTorrent, file: TBFile) -> String {
        if let meta, let number = file.episode.episode,
           let episode = appState.meta.episode(
               titleID: meta.sourceId, season: file.episode.season, number: number
           ),
           !episode.title.isEmpty {
            return episode.title
        }
        return file.prettyName(seriesName: torrent.name)
    }

    private func statsGrid(torrent: TBTorrent, status: TorrentStatus) -> some View {
        LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)], spacing: 12) {
            if let size = torrent.size, size > 0 {
                StatTile(icon: "externaldrive", title: "Размер", value: Format.bytes(size), tint: Theme.info)
            }
            if status.isActive, let speed = torrent.downloadSpeed, speed > 0 {
                StatTile(icon: "speedometer", title: "Скорость", value: Format.speed(speed), tint: Theme.accent)
            }
            if let seeds = torrent.seeds, !status.isFinished {
                StatTile(
                    icon: "arrow.up.arrow.down",
                    title: "Раздают / качают",
                    value: "\(seeds) / \(torrent.peers ?? 0)",
                    tint: seeds > 0 ? Theme.accent : Theme.danger
                )
            }
            if let files = torrent.files?.count {
                StatTile(icon: "film.stack", title: "Файлов", value: "\(files)", tint: Theme.violet)
            }
            if let created = Format.parseAPIDate(torrent.createdAt) {
                StatTile(icon: "calendar", title: "Добавлено", value: Format.relative(created), tint: Theme.idle)
            }
        }
    }

    // MARK: - Файлы и серии

    @ViewBuilder
    private func filesSection(torrent: TBTorrent, status: TorrentStatus) -> some View {
        let files = (torrent.files ?? []).sorted(by: TBFile.orderedBefore)
        let seasons = availableSeasons(files)
        let shown = filteredFiles(seasonFiles(files))
        VStack(alignment: .leading, spacing: 12) {
            SectionHeader(
                title: meta?.isSeries == true ? "Серии" : "Файлы",
                subtitle: subtitleForFiles(files: files, shown: shown, seasons: seasons)
            )
            if seasons.count > 1 {
                seasonPicker(seasons: seasons, files: files, torrent: torrent)
            }
            if files.count > 8 {
                fileSearchField
            }

            if files.isEmpty {
                Text(status.isFinished
                     ? "TorBox не отдал список файлов для этой раздачи."
                     : "Файлы появятся, как только TorBox получит метаданные.")
                    .font(.callout)
                    .foregroundStyle(Theme.textSecondary)
                    .padding(14)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .cardSurface(radius: Theme.radiusM)
            } else {
                ForEach(groupedFiles(shown), id: \.title) { group in
                    if let title = group.title, season == nil {
                        Text(title)
                            .font(.subheadline.weight(.bold))
                            .foregroundStyle(Theme.textSecondary)
                            .padding(.top, 4)
                    }
                    ForEach(group.files) { file in
                        FileRow(
                            torrent: torrent,
                            file: file,
                            episode: episodeMeta(torrent: torrent, file: file),
                            ready: status.isFinished,
                            isBusy: busyFileID == file.id,
                            onWatch: { watch(torrent: torrent, file: file) },
                            onInfuse: { playInInfuse(torrent: torrent, file: file) },
                            onShare: { share(torrent: torrent, file: file) },
                            onDownload: { startDownload(torrent: torrent, file: file) }
                        )
                    }
                }
                if shown.isEmpty {
                    Text("Ни один файл не подходит под фильтр.")
                        .font(.footnote)
                        .foregroundStyle(Theme.textTertiary)
                }
            }
        }
    }

    private var fileSearchField: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass")
                .foregroundStyle(Theme.textTertiary)
            TextField("Серия или название", text: $fileFilter)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .font(.subheadline)
            if !fileFilter.isEmpty {
                Button {
                    fileFilter = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(Theme.textTertiary)
                }
                .accessibilityLabel("Очистить фильтр")
            }
        }
        .padding(.horizontal, 12)
        .frame(height: Theme.hit)
        .cardSurface(radius: Theme.radiusM)
    }

    /// Чипы сезонов: «Все» плюс номера. Первым открывается сезон, где остановился.
    private func seasonPicker(seasons: [Int], files: [TBFile], torrent: TBTorrent) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                seasonChip(title: "Все", value: nil, torrent: torrent, files: files)
                ForEach(seasons, id: \.self) { number in
                    seasonChip(title: "Сезон \(number)", value: number, torrent: torrent, files: files)
                }
            }
            .padding(.vertical, 2)
        }
        .onAppear {
            guard !didPickSeason else { return }
            didPickSeason = true
            // Открываем сезон подсказанной серии — обычно это то, что смотрят
            if let file = PlayerQueueFactory.suggestedFile(torrent: torrent, appState: appState),
               let number = file.episode.season, seasons.contains(number) {
                season = number
            } else {
                season = seasons.first
            }
        }
    }

    private func seasonChip(title: String, value: Int?, torrent: TBTorrent, files: [TBFile]) -> some View {
        let isSelected = season == value
        let seasonFiles = value == nil ? files : files.filter { $0.episode.season == value }
        let progress = appState.playback.progress(
            torrent: torrent, files: seasonFiles.filter(\.isVideo)
        )
        return Button {
            Haptics.tap()
            withAnimation { season = value }
        } label: {
            HStack(spacing: 5) {
                Text(title)
                    .font(.subheadline.weight(.semibold))
                if progress.total > 0, progress.watched == progress.total {
                    Image(systemName: "checkmark")
                        .font(.caption2.weight(.bold))
                }
            }
            .padding(.horizontal, 14)
            .frame(height: 36)
            .background(
                Capsule().fill(isSelected ? Theme.accent : Theme.hairline.opacity(0.08))
            )
            .foregroundStyle(isSelected ? .white : Theme.textPrimary)
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(isSelected ? [.isSelected, .isButton] : .isButton)
    }

    private func availableSeasons(_ files: [TBFile]) -> [Int] {
        Array(Set(files.compactMap { $0.episode.season })).sorted()
    }

    private func seasonFiles(_ files: [TBFile]) -> [TBFile] {
        guard let season else { return files }
        return files.filter { $0.episode.season == season }
    }

    private func subtitleForFiles(files: [TBFile], shown: [TBFile], seasons: [Int]) -> String? {
        guard !files.isEmpty else { return nil }
        if season != nil {
            return "\(shown.count) \(Format.plural(shown.count, "серия", "серии", "серий")) в сезоне"
        }
        return "\(files.count) \(Format.plural(files.count, "файл", "файла", "файлов"))"
    }

    private func episodeMeta(torrent: TBTorrent, file: TBFile) -> EpisodeMeta? {
        guard let meta, let number = file.episode.episode else { return nil }
        return appState.meta.episode(titleID: meta.sourceId, season: file.episode.season, number: number)
    }

    /// Фильтр ищет и по названию серии из каталога, а не только по имени файла.
    private func filteredFiles(_ files: [TBFile]) -> [TBFile] {
        let query = fileFilter.trimmingCharacters(in: .whitespaces)
        guard !query.isEmpty else { return files }
        return files.filter { file in
            if file.displayName.localizedCaseInsensitiveContains(query) { return true }
            guard let torrent else { return false }
            return episodeTitle(torrent: torrent, file: file).localizedCaseInsensitiveContains(query)
        }
    }

    private struct FileGroup {
        let title: String?
        let files: [TBFile]
    }

    /// Сериалы разбиваем по сезонам — иначе список на 70 файлов не читается.
    private func groupedFiles(_ files: [TBFile]) -> [FileGroup] {
        let seasons = Set(files.compactMap { $0.episode.season })
        guard seasons.count > 1 else { return [FileGroup(title: nil, files: files)] }
        var groups: [FileGroup] = []
        for season in seasons.sorted() {
            let matching = files.filter { $0.episode.season == season }
            if !matching.isEmpty {
                groups.append(FileGroup(title: "Сезон \(season)", files: matching))
            }
        }
        let rest = files.filter { $0.episode.season == nil }
        if !rest.isEmpty {
            groups.append(FileGroup(title: "Остальное", files: rest))
        }
        return groups
    }

    // MARK: - Техданные

    @ViewBuilder
    private func techCard(torrent: TBTorrent, videos: [TBFile]) -> some View {
        let file = PlayerQueueFactory.suggestedFile(torrent: torrent, appState: appState) ?? videos.first
        if let file {
            let tech = MediaTech.parse(file.displayName)
            VStack(alignment: .leading, spacing: 8) {
                Text("Файл")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(Theme.textTertiary)
                    .textCase(.uppercase)
                Text(file.displayName)
                    .font(.footnote.monospaced())
                    .foregroundStyle(Theme.textSecondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                FlowLayout(spacing: 6) {
                    if let size = file.size, size > 0 {
                        Chip.size(size)
                    }
                    ForEach(tech.chips, id: \.self) { value in
                        Chip(text: value, icon: "waveform.badge.magnifyingglass", style: .tinted(Theme.quality))
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(14)
            .cardSurface(radius: Theme.radiusM)
        }
    }

    // MARK: - Меню раздачи

    private func menu(torrent: TBTorrent) -> some View {
        Menu {
            let status = TorrentStatus(torrent: torrent)
            if status.kind == .paused {
                Button {
                    control(torrent: torrent, operation: .resume, message: "Загрузка продолжена")
                } label: {
                    Label("Продолжить загрузку", systemImage: "play.fill")
                }
            }
            if !status.isFinished {
                Button {
                    control(torrent: torrent, operation: .reannounce, message: "Реанонс отправлен")
                } label: {
                    Label("Реанонс трекерам", systemImage: "antenna.radiowaves.left.and.right")
                }
            } else {
                Button {
                    control(torrent: torrent, operation: .stopSeeding, message: "Раздача остановлена")
                } label: {
                    Label("Остановить раздачу", systemImage: "stop.circle")
                }
            }
            if let files = torrent.files?.filter(\.isVideo), !files.isEmpty {
                Divider()
                Button {
                    markSeasonWatched(torrent: torrent, files: files)
                } label: {
                    Label(
                        season == nil ? "Отметить всё просмотренным" : "Отметить сезон просмотренным",
                        systemImage: "eye.fill"
                    )
                }
                Button {
                    resetSeasonProgress(torrent: torrent, files: files)
                } label: {
                    Label(
                        season == nil ? "Сбросить весь прогресс" : "Сбросить прогресс сезона",
                        systemImage: "arrow.counterclockwise"
                    )
                }
            }
            Divider()
            Button {
                refreshMeta(torrent: torrent)
            } label: {
                Label("Обновить описание и постер", systemImage: "arrow.clockwise.circle")
            }
            Divider()
            Button(role: .destructive) {
                confirmDelete = true
            } label: {
                Label("Удалить из TorBox", systemImage: "trash")
            }
        } label: {
            Image(systemName: "ellipsis")
                .font(.headline)
                .foregroundStyle(Theme.textPrimary)
                .frame(width: 36, height: 36)
                .background(Circle().fill(.ultraThinMaterial))
        }
        .accessibilityLabel("Действия с раздачей")
    }

    // MARK: - Действия

    private func watch(torrent: TBTorrent, file: TBFile) {
        Haptics.press()
        playerQueue = PlayerQueueFactory.queue(torrent: torrent, startAt: file, appState: appState)
    }

    private func markSeasonWatched(torrent: TBTorrent, files: [TBFile]) {
        Haptics.success()
        for file in seasonFiles(files) {
            appState.playback.markWatched(torrent: torrent, file: file)
        }
        appState.toasts.success("Отмечено просмотренным")
    }

    private func resetSeasonProgress(torrent: TBTorrent, files: [TBFile]) {
        Haptics.warning()
        for file in seasonFiles(files) {
            appState.playback.resetProgress(torrent: torrent, fileID: file.id)
        }
        appState.toasts.info("Прогресс сброшен")
    }

    private func refreshMeta(torrent: TBTorrent) {
        appState.meta.forget(name: torrent.name)
        Task {
            await appState.meta.load(name: torrent.name, client: appState.searchClient)
            if let meta = appState.meta.meta(for: torrent.name), meta.isSeries {
                await appState.meta.loadEpisodes(titleID: meta.sourceId, client: appState.searchClient)
            }
            if appState.meta.meta(for: torrent.name) == nil {
                appState.toasts.info("Каталог не узнал эту раздачу")
            }
        }
    }

    private func startDownload(torrent: TBTorrent, file: TBFile) {
        guard let client = appState.torBoxClient else { return }
        let downloads = appState.downloads
        let key = PlaybackStore.key(torrent: torrent, fileID: file.id)
        if downloads.item(key: key)?.state == .done {
            appState.toasts.info("Файл уже на телефоне")
            return
        }
        withLink(torrent: torrent, file: file) { cdn in
            let started = downloads.ensureDownloading(torrent: torrent, file: file, url: cdn) {
                try await client.downloadLink(torrentID: torrent.id, fileID: file.id)
            }
            if started {
                appState.toasts.success("Качаю в телефон", message: file.prettyName(seriesName: torrent.name))
            } else {
                appState.toasts.error(
                    "Не хватает места",
                    message: "Освободи память телефона — файлу нужно \(Format.bytes(file.size ?? 0)).",
                    actionTitle: "Память",
                    action: { appState.selectedTab = .settings }
                )
            }
        }
    }

    private func playInInfuse(torrent: TBTorrent, file: TBFile) {
        withLink(torrent: torrent, file: file) { cdn in
            guard let target = Infuse.playURL(for: cdn) else {
                appState.toasts.error("Не получилось собрать ссылку для Infuse")
                return
            }
            openURL(target)
        }
    }

    private func share(torrent: TBTorrent, file: TBFile) {
        withLink(torrent: torrent, file: file) { cdn in
            shareURL = cdn
        }
    }

    private func withLink(torrent: TBTorrent, file: TBFile, then action: @escaping (URL) -> Void) {
        guard let client = appState.torBoxClient else { return }
        busyFileID = file.id
        Task {
            defer { busyFileID = nil }
            do {
                let cdn = try await client.downloadLink(torrentID: torrent.id, fileID: file.id)
                action(cdn)
            } catch {
                appState.toasts.failure("Ссылка не получена", error)
            }
        }
    }

    private func control(torrent: TBTorrent, operation: TorrentOperation, message: String) {
        guard let client = appState.torBoxClient else { return }
        Haptics.tap()
        Task {
            let ok = await vm.control(client: client, id: torrent.id, operation: operation)
            if ok {
                appState.toasts.success(message)
            } else if let error = vm.loadError {
                appState.toasts.error("Не получилось", message: error)
            }
        }
    }

    private func deleteTorrent() {
        guard let client = appState.torBoxClient, let torrent else { return }
        Task {
            let ok = await vm.control(client: client, id: torrent.id, operation: .delete)
            if ok {
                appState.toasts.success("Удалено из TorBox", message: torrent.name)
                dismiss()
            } else if let error = vm.loadError {
                appState.toasts.error("Не удалось удалить", message: error)
            }
        }
    }
}

/// Строка серии: кадр или номер, название из каталога, состояние просмотра.
private struct FileRow: View {
    @Environment(AppState.self) private var appState
    let torrent: TBTorrent
    let file: TBFile
    let episode: EpisodeMeta?
    let ready: Bool
    let isBusy: Bool
    let onWatch: () -> Void
    let onInfuse: () -> Void
    let onShare: () -> Void
    let onDownload: () -> Void

    var body: some View {
        let key = PlaybackStore.key(torrent: torrent, fileID: file.id)
        let watched = appState.playback.entry(torrent: torrent, fileID: file.id)
        let download = appState.downloads.item(key: key)
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 12) {
                artwork
                VStack(alignment: .leading, spacing: 5) {
                    Text(title)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.textPrimary)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                    if let episode, !episode.overview.isEmpty {
                        Text(episode.overview)
                            .font(.caption)
                            .foregroundStyle(Theme.textSecondary)
                            .lineLimit(2)
                    } else if title != file.displayName {
                        Text(file.displayName)
                            .font(.caption2)
                            .foregroundStyle(Theme.textTertiary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                    if let watched, watched.isResumable {
                        SlimProgressBar(fraction: watched.fraction, height: 3)
                            .padding(.trailing, 8)
                    }
                    FlowLayout(spacing: 6) {
                        if let size = file.size, size > 0 {
                            Chip.size(size)
                        }
                        if let watched {
                            if watched.isWatched {
                                Chip(text: "просмотрено", icon: "checkmark", style: .tinted(Theme.accent))
                            } else if watched.isResumable {
                                Chip(
                                    text: Format.clock(watched.positionSeconds),
                                    icon: "play.fill",
                                    style: .tinted(Theme.accent),
                                    accessibility: "Продолжить с \(Format.clock(watched.positionSeconds))"
                                )
                            }
                        }
                        downloadChip(download: download, key: key)
                    }
                }
                Spacer(minLength: 0)
                if ready {
                    actions
                }
            }
        }
        .padding(12)
        .cardSurface(radius: Theme.radiusM)
    }

    private var title: String {
        if let episode, !episode.title.isEmpty { return episode.title }
        return file.prettyName(seriesName: torrent.name)
    }

    /// Короткая причина вместо безликого «не скачался».
    private static func shortFailure(_ reason: String) -> String {
        let lowered = reason.lowercased()
        if lowered.contains("места") { return "мало места" }
        if lowered.contains("http") { return "ошибка сервера" }
        if lowered.contains("интернет") || lowered.contains("network") || lowered.contains("соединен") {
            return "нет сети"
        }
        return "не скачался"
    }

    /// Кадр серии, если каталог его отдал; иначе — номер серии.
    @ViewBuilder
    private var artwork: some View {
        if let still = episode?.stillUrl, !still.isEmpty, let url = URL(string: still) {
            AsyncImage(url: url) { phase in
                switch phase {
                case .success(let image):
                    image.resizable().aspectRatio(contentMode: .fill)
                default:
                    Theme.hairline.opacity(0.08)
                }
            }
            .frame(width: 104, height: 59)
            .clipShape(RoundedRectangle(cornerRadius: Theme.radiusS, style: .continuous))
            .overlay(alignment: .bottomLeading) {
                if let number = file.episode.episode {
                    Text("\(number)")
                        .font(.caption2.weight(.heavy))
                        .padding(.horizontal, 5)
                        .padding(.vertical, 2)
                        .background(Capsule().fill(.black.opacity(0.6)))
                        .foregroundStyle(.white)
                        .padding(4)
                }
            }
            .accessibilityHidden(true)
        } else if let number = file.episode.episode {
            VStack(spacing: 0) {
                Text("\(number)")
                    .font(.system(size: 17, weight: .heavy, design: .rounded))
                    .foregroundStyle(Theme.textPrimary)
                if let season = file.episode.season {
                    Text("S\(season)")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(Theme.textTertiary)
                }
            }
            .frame(width: 42, height: 42)
            .background(
                RoundedRectangle(cornerRadius: Theme.radiusS, style: .continuous)
                    .fill(Theme.hairline.opacity(0.07))
            )
            .accessibilityHidden(true)
        } else {
            FileArtwork(title: file.displayName, isVideo: file.isVideo, side: 42)
        }
    }

    @ViewBuilder
    private func downloadChip(download: DownloadManager.Item?, key: String) -> some View {
        if let download {
            switch download.state {
            case .done:
                Chip.onDevice()
            case .downloading:
                Chip(
                    text: "\(Int((appState.downloads.progress[key] ?? 0) * 100))%",
                    icon: "arrow.down",
                    style: .tinted(Theme.info),
                    accessibility: "Качается в телефон"
                )
            case .paused:
                Chip(text: "пауза", icon: "pause.fill", style: .tinted(Theme.idle))
            case .failed(let reason):
                Chip(
                    text: Self.shortFailure(reason),
                    icon: "exclamationmark.triangle.fill",
                    style: .tinted(Theme.danger),
                    accessibility: "Не скачался: \(reason)"
                )
            }
        }
    }

    @ViewBuilder
    private var actions: some View {
        if isBusy {
            ProgressView()
                .frame(width: 38, height: 38)
        } else {
            HStack(spacing: 6) {
                if file.isVideo {
                    Button {
                        onWatch()
                    } label: {
                        Image(systemName: "play.fill")
                            .font(.system(size: 14, weight: .bold))
                            .foregroundStyle(.white)
                            .frame(width: 38, height: 38)
                            .background(Circle().fill(Theme.accent))
                            .hitTarget()
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Смотреть \(title)")
                }
                Menu {
                    if file.isVideo {
                        let watched = appState.playback.entry(torrent: torrent, fileID: file.id)
                        if watched?.isWatched == true {
                            Button {
                                appState.playback.resetProgress(torrent: torrent, fileID: file.id)
                                Haptics.tap()
                            } label: {
                                Label("Снять отметку «просмотрено»", systemImage: "eye.slash")
                            }
                        } else {
                            Button {
                                appState.playback.markWatched(torrent: torrent, file: file)
                                Haptics.success()
                            } label: {
                                Label("Отметить просмотренным", systemImage: "eye.fill")
                            }
                        }
                        if watched?.isStarted == true {
                            Button {
                                appState.playback.resetProgress(torrent: torrent, fileID: file.id)
                                Haptics.tap()
                            } label: {
                                Label("Смотреть с начала", systemImage: "arrow.counterclockwise")
                            }
                        }
                        Divider()
                    }
                    if file.isVideo, Infuse.isInstalled {
                        Button {
                            onInfuse()
                        } label: {
                            Label("Смотреть в Infuse", systemImage: "play.circle")
                        }
                    }
                    Button {
                        onShare()
                    } label: {
                        Label("Поделиться ссылкой", systemImage: "square.and.arrow.up")
                    }
                    downloadMenuItems
                } label: {
                    Image(systemName: "ellipsis")
                        .font(.system(size: 15, weight: .bold))
                        .foregroundStyle(Theme.textSecondary)
                        .frame(width: 38, height: 38)
                        .background(Circle().fill(Theme.hairline.opacity(0.07)))
                        .hitTarget()
                }
                .accessibilityLabel("Действия с файлом")
            }
        }
    }

    @ViewBuilder
    private var downloadMenuItems: some View {
        let key = PlaybackStore.key(torrent: torrent, fileID: file.id)
        let downloads = appState.downloads
        switch downloads.item(key: key)?.state {
        case .done:
            Button(role: .destructive) {
                downloads.deleteLocal(key: key)
                Haptics.warning()
            } label: {
                Label("Удалить с телефона", systemImage: "iphone.slash")
            }
        case .downloading:
            Button {
                downloads.pause(key: key)
            } label: {
                Label("Пауза загрузки", systemImage: "pause.circle")
            }
        case .paused, .failed:
            Button {
                downloads.resume(key: key, client: appState.torBoxClient)
            } label: {
                Label("Продолжить загрузку", systemImage: "arrow.clockwise")
            }
        case nil:
            Button {
                onDownload()
            } label: {
                Label("Скачать в телефон", systemImage: "arrow.down.to.line")
            }
        }
    }
}

/// Сборка deep-link для Infuse: вложенный URL кодируем вручную по unreserved-набору,
/// иначе &-параметры CDN-ссылки разорвут query самого infuse://.
enum Infuse {
    static var isInstalled: Bool {
        guard let probe = URL(string: "infuse://") else { return false }
        return UIApplication.shared.canOpenURL(probe)
    }

    static func playURL(for cdn: URL) -> URL? {
        let unreserved = CharacterSet(
            charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
        )
        guard let encoded = cdn.absoluteString.addingPercentEncoding(withAllowedCharacters: unreserved) else {
            return nil
        }
        return URL(string: "infuse://x-callback-url/play?url=\(encoded)")
    }
}

private struct ShareItem: Identifiable {
    let url: URL
    var id: String { url.absoluteString }
}

private struct ShareSheet: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: [url], applicationActivities: nil)
    }

    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}
