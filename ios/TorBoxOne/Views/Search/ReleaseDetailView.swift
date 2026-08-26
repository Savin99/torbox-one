import SwiftUI

/// Карточка раздачи целиком: характеристики, описание темы, добавление.
struct ReleaseDetailView: View {
    @Environment(AppState.self) private var appState
    @Environment(\.openURL) private var openURL
    let vm: TrackerResultsViewModel
    let release: ReleaseResult

    @State private var showFullDetails = false

    private var titleMeta: TitleMeta? { appState.meta.meta(for: release.title) }

    var body: some View {
        ZStack {
            AppBackground()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if let titleMeta {
                        BackdropHeader(meta: titleMeta, fallbackTitle: release.title, height: 200)
                            .padding(.bottom, 2)
                    }
                    headerCard
                    if let titleMeta, !titleMeta.overview.isEmpty {
                        catalogCard(titleMeta)
                    }
                    statsGrid
                    detailsCard
                    actions
                }
                .padding(.top, titleMeta == nil ? 12 : 0)
                .padding(.bottom, Theme.tabBarInset)
            }
        }
        .navigationTitle(titleMeta?.title ?? "Раздача")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(titleMeta == nil ? .automatic : .hidden, for: .navigationBar)
        .task {
            if let client = appState.searchClient {
                await vm.loadDetails(client: client, release: release)
            }
        }
        .task(id: release.id) {
            // Постер и описание тайтла — по названию раздачи
            await appState.meta.load(name: release.title, client: appState.searchClient)
        }
    }

    // MARK: - Заголовок

    private var headerCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(release.title)
                .font(.title3.weight(.bold))
                .foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
            FlowLayout(spacing: 6) {
                if vm.isCached(release) { Chip.instant }
                if let resolution = release.resolutionLabel { Chip.quality(resolution) }
                if release.hasVoiceover { Chip.voiceover("озвучка") }
            }
            if !release.category.isEmpty {
                Text(release.category)
                    .font(.caption)
                    .foregroundStyle(Theme.textTertiary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .cardSurface()
    }

    /// Что известно о самом фильме или сериале, а не о конкретной раздаче.
    private func catalogCard(_ meta: TitleMeta) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if !meta.tagline.isEmpty {
                Text(meta.tagline)
                    .font(.callout.weight(.medium))
                    .foregroundStyle(Theme.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text(meta.overview)
                .font(.callout)
                .foregroundStyle(Theme.textSecondary)
                .lineLimit(4)
                .fixedSize(horizontal: false, vertical: true)
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
        .padding(.horizontal, Theme.gutter)
    }

    private var statsGrid: some View {
        LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)], spacing: 12) {
            StatTile(icon: "externaldrive", title: "Размер", value: release.sizeText, tint: Theme.info)
            StatTile(
                icon: "arrow.up.arrow.down",
                title: "Раздают / качают",
                value: "\(release.seeders) / \(release.leechers)",
                tint: release.seeders > 5 ? Theme.accent : Theme.danger
            )
            StatTile(icon: "globe", title: "Источник", value: release.sourceName, tint: Theme.violet)
            StatTile(
                icon: vm.isCached(release) ? "bolt.fill" : "clock",
                title: "Старт",
                value: vm.isCached(release) ? "мгновенно" : "обычная закачка",
                tint: vm.isCached(release) ? Theme.instant : Theme.idle
            )
        }
        .padding(.horizontal, Theme.gutter)
    }

    // MARK: - Описание

    @ViewBuilder
    private var detailsCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("О раздаче")
                .font(.headline)
                .foregroundStyle(Theme.textPrimary)
            if let details = vm.detailsCache[release.id] {
                if details.qualitySummary.isEmpty, details.voiceoverSummary.isEmpty,
                   details.audioSummary.isEmpty, details.detailsText.isEmpty {
                    Text("Трекер не отдал подробностей — ориентируйся на название.")
                        .font(.callout)
                        .foregroundStyle(Theme.textSecondary)
                } else {
                    VStack(spacing: 8) {
                        detailRow("Качество", details.qualitySummary, icon: "film")
                        detailRow("Озвучка", details.voiceoverSummary, icon: "waveform")
                        detailRow("Аудио", details.audioSummary, icon: "speaker.wave.2")
                    }
                    if !details.detailsText.isEmpty {
                        Text(details.detailsText)
                            .font(.callout)
                            .foregroundStyle(Theme.textSecondary)
                            .lineLimit(showFullDetails ? nil : 6)
                            .fixedSize(horizontal: false, vertical: true)
                        Button(showFullDetails ? "Свернуть" : "Показать полностью") {
                            Haptics.tap()
                            withAnimation { showFullDetails.toggle() }
                        }
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.accent)
                    }
                }
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    SkeletonBlock(height: 13)
                    SkeletonBlock(height: 13)
                    SkeletonBlock(width: 220, height: 13)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .cardSurface()
        .padding(.horizontal, Theme.gutter)
    }

    @ViewBuilder
    private func detailRow(_ title: String, _ value: String, icon: String) -> some View {
        if !value.isEmpty {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: icon)
                    .font(.caption)
                    .foregroundStyle(Theme.textTertiary)
                    .frame(width: 16)
                Text(title)
                    .font(.subheadline)
                    .foregroundStyle(Theme.textSecondary)
                Spacer(minLength: 8)
                Text(value)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Theme.textPrimary)
                    .multilineTextAlignment(.trailing)
            }
        }
    }

    // MARK: - Действия

    @ViewBuilder
    private var actions: some View {
        VStack(spacing: 10) {
            switch vm.addState(release) {
            case .adding:
                HStack(spacing: 10) {
                    ProgressView().tint(.white)
                    Text("Добавляю в TorBox…")
                }
                .font(.headline)
                .foregroundStyle(.white)
                .frame(maxWidth: .infinity, minHeight: 52)
                .background(
                    RoundedRectangle(cornerRadius: Theme.radiusM, style: .continuous)
                        .fill(Theme.accent.opacity(0.6))
                )
            case .added:
                VStack(spacing: 10) {
                    Label("Добавлено в TorBox", systemImage: "checkmark.circle.fill")
                        .font(.headline)
                        .foregroundStyle(Theme.accent)
                        .frame(maxWidth: .infinity, minHeight: 52)
                        .background(
                            RoundedRectangle(cornerRadius: Theme.radiusM, style: .continuous)
                                .fill(Theme.accent.opacity(0.14))
                        )
                    Button("Открыть в библиотеке") {
                        Haptics.press()
                        appState.selectedTab = .library
                    }
                    .buttonStyle(AccentButtonStyle())
                }
            case .idle, .failed:
                Button {
                    add()
                } label: {
                    Label(
                        appState.hasTorBoxKey ? "Добавить в TorBox" : "Сначала подключи TorBox",
                        systemImage: appState.hasTorBoxKey ? "plus.circle.fill" : "key"
                    )
                }
                .buttonStyle(AccentButtonStyle(tint: appState.hasTorBoxKey ? Theme.accent : Theme.idle))
                if case .failed(let message) = vm.addState(release) {
                    Label(message, systemImage: "exclamationmark.triangle.fill")
                        .font(.footnote)
                        .foregroundStyle(Theme.danger)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }

            HStack(spacing: 10) {
                if let magnet = release.magnet {
                    Button {
                        UIPasteboard.general.string = magnet
                        appState.toasts.info("Магнет скопирован")
                    } label: {
                        Label("Магнет", systemImage: "link")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(SoftButtonStyle())
                }
                if let page = release.sourcePageURL {
                    Button {
                        Haptics.tap()
                        openURL(page)
                    } label: {
                        Label("На трекере", systemImage: "arrow.up.right.square")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(SoftButtonStyle())
                }
            }
        }
        .padding(.horizontal, Theme.gutter)
    }

    private func add() {
        guard let search = appState.searchClient else { return }
        guard let torBox = appState.torBoxClient else {
            appState.selectedTab = .settings
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
            }
        }
    }
}

/// Плитка характеристики: иконка, подпись, значение.
struct StatTile: View {
    let icon: String
    let title: String
    let value: String
    var tint: Color = Theme.accent

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(tint)
                .frame(width: 34, height: 34)
                .background(Circle().fill(tint.opacity(0.15)))
            VStack(alignment: .leading, spacing: 1) {
                Text(title)
                    .font(.caption2)
                    .foregroundStyle(Theme.textTertiary)
                    .lineLimit(1)
                Text(value)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .cardSurface(radius: Theme.radiusM)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(title): \(value)")
    }
}
