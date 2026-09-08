import SwiftUI

/// Карточка раздачи: обложка тайтла, описание, состав, серии и техданные.
/// Метаданные подтягиваются по имени раздачи через мини-API.
struct TorrentDetailView: View {
    @Environment(AppState.self) private var appState
    @Environment(\.dismiss) private var dismiss
    let vm: DownloadsViewModel
    let torrentID: Int

    @State private var busyFileID: Int?
    @State private var shareURL: URL?
    @State private var fileFilter = ""
    @State private var selectedFileIDs = Set<Int>()
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
            FileTransferSheet(url: item.url)
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
        let videos = DownloadSelection.videoFiles(of: torrent)
        return ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                BackdropHeader(
                    meta: meta,
                    fallbackTitle: torrent.name ?? "Без названия",
                    height: 300
                )

                VStack(alignment: .leading, spacing: 18) {
                    if status.isFinished, !videos.isEmpty {
                        downloadActions(torrent: torrent, videos: videos)
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

    private func downloadActions(torrent: TBTorrent, videos: [TBFile]) -> some View {
        let batch = DownloadSelection.seasonFiles(videos, season: season)
        let number = season ?? availableSeasons(videos).first
        return VStack(alignment: .leading, spacing: 12) {
            InfuseLibraryCard()
            if !batch.isEmpty {
                Button {
                    download(torrent: torrent, files: batch)
                } label: {
                    HStack {
                        Image(systemName: "arrow.down.to.line")
                        VStack(alignment: .leading, spacing: 3) {
                            Text(videos.count == 1 ? "Скачать фильм в Infuse" : "Скачать сезон \(number.map(String.init) ?? "") в Infuse")
                                .font(.headline)
                            Text(selectionSummary(batch)).font(.caption)
                        }
                        Spacer()
                    }
                }
                .buttonStyle(AccentButtonStyle())
            }
            Text("Можно скачать сезон целиком или отметить нужные серии ниже. Сначала дождись загрузки в Infuse, затем смотри локально.")
                .font(.footnote).foregroundStyle(Theme.textSecondary)
        }
    }

    private func selectionSummary(_ files: [TBFile]) -> String {
        let count = "\(files.count) \(Format.plural(files.count, "файл", "файла", "файлов"))"
        guard files.allSatisfy({ $0.size != nil }) else { return count }
        return count + " · " + Format.bytes(files.reduce(Int64(0)) { $0 + max(0, $1.size ?? 0) })
    }

    private func download(torrent: TBTorrent, files: [TBFile]) {
        guard TorrentStatus(torrent: torrent).isFinished else { return }
        Haptics.press()
        appState.infuse.start(DownloadSelection.items(torrent: torrent, files: files), appState: appState)
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
            if status.isFinished, files.contains(where: \.isVideo) {
                let selected = files.filter { selectedFileIDs.contains($0.id) && $0.isVideo }
                HStack {
                    Text(selected.isEmpty ? "Отметь нужные серии" : selectionSummary(selected))
                        .font(.caption).foregroundStyle(Theme.textSecondary)
                    Spacer()
                    Button("Скачать выбранное") { download(torrent: torrent, files: selected) }
                        .font(.subheadline.weight(.semibold))
                        .disabled(selected.isEmpty)
                }
                .frame(minHeight: Theme.hit)
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
                            isSelected: selectedFileIDs.contains(file.id),
                            onSelect: {
                                if selectedFileIDs.contains(file.id) { selectedFileIDs.remove(file.id) }
                                else { selectedFileIDs.insert(file.id) }
                            },
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
                ForEach(seasons, id: \.self) { number in
                    seasonChip(title: "Сезон \(number)", value: number, torrent: torrent, files: files)
                }
            }
            .padding(.vertical, 2)
        }
        .onAppear {
            guard !didPickSeason else { return }
            didPickSeason = true
            season = seasons.first
        }
    }

    private func seasonChip(title: String, value: Int?, torrent: TBTorrent, files: [TBFile]) -> some View {
        let isSelected = (season ?? availableSeasons(files).first) == value
        return Button {
            Haptics.tap()
            withAnimation { season = value; selectedFileIDs.removeAll() }
        } label: {
            Text(title)
                .font(.subheadline.weight(.semibold))
                .padding(.horizontal, 14)
                .frame(height: 36)
                .background(Capsule().fill(isSelected ? Theme.accent : Theme.hairline.opacity(0.08)))
                .foregroundStyle(isSelected ? .white : Theme.textPrimary)
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(isSelected ? [.isSelected, .isButton] : .isButton)
    }

    private func availableSeasons(_ files: [TBFile]) -> [Int] {
        Array(Set(files.compactMap { $0.episode.season })).sorted()
    }

    private func seasonFiles(_ files: [TBFile]) -> [TBFile] {
        guard let selected = season ?? availableSeasons(files).first else { return files }
        return files.filter { $0.episode.season == selected || $0.episode.season == nil }
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
        let file = videos.first
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
        if file.isVideo {
            download(torrent: torrent, files: [file])
            return
        }
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

/// Selection and download state for TorBox files. Playback history belongs to Infuse.
private struct FileRow: View {
    @Environment(AppState.self) private var appState
    let torrent: TBTorrent
    let file: TBFile
    let episode: EpisodeMeta?
    let ready: Bool
    let isBusy: Bool
    let isSelected: Bool
    let onSelect: () -> Void
    let onShare: () -> Void
    let onDownload: () -> Void

    private var title: String {
        if let episode, !episode.title.isEmpty { return episode.title }
        return file.prettyName(seriesName: torrent.name)
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            if file.isVideo, ready {
                Button(action: onSelect) {
                    Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                        .font(.title3)
                        .foregroundStyle(isSelected ? Theme.accent : Theme.textTertiary)
                        .frame(width: 44, height: 44)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Выбрать \(title)")
                .accessibilityAddTraits(isSelected ? [.isSelected] : [])
            }
            VStack(alignment: .leading, spacing: 6) {
                Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.textPrimary)
                if let label = file.episode.label {
                    Text(label).font(.caption).foregroundStyle(Theme.textSecondary)
                }
                if let episode, !episode.overview.isEmpty {
                    Text(episode.overview).font(.caption).foregroundStyle(Theme.textSecondary).lineLimit(2)
                }
                FlowLayout(spacing: 6) {
                    if let size = file.size { Chip.size(size) }
                    if appState.downloads.localURL(key: PlaybackStore.key(torrent: torrent, fileID: file.id)) != nil {
                        Chip(text: "в TorBox One", icon: "iphone", style: .tinted(Theme.info))
                    }
                }
            }
            Spacer(minLength: 0)
            if isBusy {
                ProgressView().frame(width: 44, height: 44)
            } else if ready {
                VStack(spacing: 4) {
                    Button(action: onDownload) {
                        Image(systemName: "arrow.down.to.line")
                            .foregroundStyle(Theme.accent).frame(width: 44, height: 44)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(file.isVideo ? "Скачать \(title) в Infuse" : "Скачать \(title)")
                    Menu {
                        if let local = appState.downloads.localURL(key: PlaybackStore.key(torrent: torrent, fileID: file.id)) {
                            Button {
                                appState.infuse.sheet = .export(local)
                            } label: {
                                Label("Передать сохранённый файл", systemImage: "square.and.arrow.up")
                            }
                        }
                        Button(action: onShare) { Label("Поделиться ссылкой", systemImage: "link") }
                    } label: {
                        Image(systemName: "ellipsis").frame(width: 44, height: 44)
                    }
                    .accessibilityLabel("Действия с файлом")
                }
            }
        }
        .padding(12)
        .cardSurface(radius: Theme.radiusM)
    }
}

private struct ShareItem: Identifiable {
    let url: URL
    var id: String { url.absoluteString }
}
