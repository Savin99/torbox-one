import SwiftUI

struct SettingsTab: View {
    @Environment(AppState.self) private var appState
    @Environment(\.openURL) private var openURL

    @State private var torBoxKeyInput = ""
    @State private var torBoxStatus: CheckStatus = .idle
    @State private var searchURLInput = ""
    @State private var searchTokenInput = ""
    @State private var searchStatus: CheckStatus = .idle
    @State private var showKey = false
    @State private var confirmForgetKey = false
    @State private var confirmClearMeta = false


    var body: some View {
        NavigationStack {
            ZStack {
                AppBackground()
                ScrollView {
                    VStack(spacing: 16) {
                        connectionCard
                        torBoxCard
                        searchCard
                        watchCard
                        metadataCard
                        infuseCard
                        aboutCard
                        #if DEBUG
                        debugCard
                        #endif
                    }
                    .padding(.horizontal, Theme.gutter)
                    .padding(.top, 8)
                    .padding(.bottom, Theme.tabBarInset)
                }
                .scrollDismissesKeyboard(.interactively)
            }
            .navigationTitle("Настройки")
            .confirmationDialog(
                "Очистить кэш метаданных? Постеры и описания загрузятся заново.",
                isPresented: $confirmClearMeta,
                titleVisibility: .visible
            ) {
                Button("Очистить", role: .destructive) {
                    appState.meta.clear()
                    appState.thumbnails.clear()
                    appState.toasts.info("Кэш метаданных очищен")
                }
                Button("Отмена", role: .cancel) {}
            }
            .onAppear {
                torBoxKeyInput = appState.torBoxKey
                searchURLInput = appState.searchAPIURL
                searchTokenInput = appState.searchAPIToken
            }
            .confirmationDialog(
                "Удалить ключ TorBox из Keychain?",
                isPresented: $confirmForgetKey,
                titleVisibility: .visible
            ) {
                Button("Удалить ключ", role: .destructive) {
                    appState.torBoxKey = ""
                    torBoxKeyInput = ""
                    torBoxStatus = .idle
                    appState.toasts.info("Ключ удалён")
                }
                Button("Отмена", role: .cancel) {}
            }
        }
    }

    // MARK: - Состояние подключений

    private var connectionCard: some View {
        VStack(spacing: 12) {
            connectionRow(
                icon: "cloud.fill",
                title: "TorBox",
                ok: appState.hasTorBoxKey,
                okText: "подключён",
                failText: "нет ключа"
            )
            Divider().overlay(Theme.hairline.opacity(0.08))
            connectionRow(
                icon: "magnifyingglass",
                title: "Сервер поиска",
                ok: appState.hasSearchAPI,
                okText: URL(string: appState.searchAPIURL)?.host() ?? "подключён",
                failText: "не указан"
            )
        }
        .padding(16)
        .cardSurface()
    }

    private func connectionRow(icon: String, title: String, ok: Bool, okText: String, failText: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(ok ? Theme.accent : Theme.idle)
                .frame(width: 34, height: 34)
                .background(Circle().fill((ok ? Theme.accent : Theme.idle).opacity(0.15)))
            VStack(alignment: .leading, spacing: 1) {
                Text(title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.textPrimary)
                Text(ok ? okText : failText)
                    .font(.caption)
                    .foregroundStyle(ok ? Theme.accent : Theme.textSecondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
            Image(systemName: ok ? "checkmark.circle.fill" : "exclamationmark.circle")
                .foregroundStyle(ok ? Theme.accent : Theme.instant)
        }
        .accessibilityElement(children: .combine)
    }

    // MARK: - TorBox

    private var torBoxCard: some View {
        SettingsCard(
            title: "TorBox",
            icon: "cloud.fill",
            tint: Theme.accent,
            footer: "Ключ из TorBox → Settings → API Keys. Хранится в Keychain и уходит только на api.torbox.app."
        ) {
            HStack(spacing: 8) {
                if showKey {
                    TextField("API-ключ", text: $torBoxKeyInput)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(.callout.monospaced())
                } else {
                    SecureField("API-ключ", text: $torBoxKeyInput)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(.callout)
                }
                Button {
                    showKey.toggle()
                    Haptics.tap()
                } label: {
                    Image(systemName: showKey ? "eye.slash" : "eye")
                        .foregroundStyle(Theme.textTertiary)
                }
                .accessibilityLabel(showKey ? "Скрыть ключ" : "Показать ключ")
                PasteButton(payloadType: String.self) { strings in
                    guard let pasted = strings.first else { return }
                    torBoxKeyInput = pasted.trimmingCharacters(in: .whitespacesAndNewlines)
                    Haptics.tap()
                }
                .labelStyle(.iconOnly)
                .buttonBorderShape(.circle)
                .tint(Theme.accent)
                .accessibilityLabel("Вставить ключ из буфера")
            }
            .frame(minHeight: Theme.hit)

            StatusLine(status: torBoxStatus)

            HStack(spacing: 10) {
                Button {
                    Task { await checkTorBoxKey() }
                } label: {
                    if torBoxStatus.isChecking {
                        ProgressView().tint(.white)
                    } else {
                        Text("Проверить и сохранить")
                    }
                }
                .buttonStyle(AccentButtonStyle())
                .disabled(torBoxKeyInput.trimmingCharacters(in: .whitespaces).isEmpty || torBoxStatus.isChecking)
            }

            if appState.hasTorBoxKey {
                Button("Удалить ключ") {
                    Haptics.warning()
                    confirmForgetKey = true
                }
                .font(.subheadline.weight(.medium))
                .foregroundStyle(Theme.danger)
                .frame(minHeight: 36)
            }
        }
    }

    private func checkTorBoxKey() async {
        torBoxStatus = .checking
        let result = await SetupChecks.torBox(key: torBoxKeyInput)
        torBoxStatus = result
        if result.isOK {
            appState.torBoxKey = torBoxKeyInput.trimmingCharacters(in: .whitespacesAndNewlines)
            appState.toasts.success("TorBox подключён")
        } else {
            Haptics.error()
        }
    }

    // MARK: - Сервер поиска

    private var searchCard: some View {
        SettingsCard(
            title: "Сервер поиска",
            icon: "magnifyingglass",
            tint: Theme.violet,
            footer: "Мини-API с каталогом Кинопоиска и поиском по трекерам. Дома — адрес компьютера, в дороге — VPS."
        ) {
            TextField("http://192.168.1.10:8787", text: $searchURLInput)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
                .font(.callout)
                .frame(minHeight: Theme.hit)
            Divider().overlay(Theme.hairline.opacity(0.08))
            SecureField("Токен (если включён)", text: $searchTokenInput)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .font(.callout)
                .frame(minHeight: Theme.hit)

            StatusLine(status: searchStatus)

            Button {
                Task { await checkSearchAPI() }
            } label: {
                if searchStatus.isChecking {
                    ProgressView().tint(.white)
                } else {
                    Text("Проверить и сохранить")
                }
            }
            .buttonStyle(AccentButtonStyle())
            .disabled(URL(string: searchURLInput)?.host() == nil || searchStatus.isChecking)
        }
    }

    private func checkSearchAPI() async {
        searchStatus = .checking
        let result = await SetupChecks.searchAPI(url: searchURLInput, token: searchTokenInput)
        searchStatus = result
        if result.isOK {
            appState.searchAPIURL = searchURLInput.trimmingCharacters(in: .whitespacesAndNewlines)
            appState.searchAPIToken = searchTokenInput
            appState.toasts.success("Сервер поиска подключён")
        } else {
            Haptics.error()
        }
    }

    // MARK: - Просмотр

    private var watchCard: some View {
        SettingsCard(
            title: "Локальный просмотр",
            icon: "iphone",
            tint: Theme.info,
            footer: "TorBox One передаёт выбранные файлы на скачивание. Просмотр и продолжение с места остановки — в Infuse."
        ) {
            InfuseLibraryCard()
            Text("1. Добавь раздачу и дождись её готовности в TorBox.\n2. Открой раздачу и скачай фильм, сезон или выбранные серии в Infuse.\n3. Дождись окончания загрузки в Infuse и смотри без интернета.\n4. Удали просмотренный сезон в Infuse, затем скачай следующий здесь.")
                .font(.footnote).foregroundStyle(Theme.textSecondary)
            if !appState.downloads.items.isEmpty {
                Divider()
                NavigationLink {
                    DownloadedFilesView()
                } label: {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Файлы TorBox One")
                            Text("\(Format.bytes(appState.downloads.totalBytesUsed)) · можно передать в Infuse")
                                .font(.caption).foregroundStyle(Theme.textSecondary)
                        }
                        Spacer()
                        Image(systemName: "chevron.right")
                    }
                    .frame(minHeight: Theme.hit)
                }
                .buttonStyle(.plain)
            }
        }
    }

    // MARK: - Метаданные

    private var metadataCard: some View {
        SettingsCard(
            title: "Метаданные",
            icon: "photo.on.rectangle.angled",
            tint: Theme.quality,
            footer: "Постеры, обложки, описания и русские названия серий приходят от сервера поиска (Кинопоиск). Кадры серий появятся, если на сервере задан TMDB_API_KEY."
        ) {
            HStack {
                Text("В кэше")
                    .font(.subheadline)
                    .foregroundStyle(Theme.textSecondary)
                Spacer()
                Text("\(appState.meta.storedTitles) \(Format.plural(appState.meta.storedTitles, "тайтл", "тайтла", "тайтлов"))")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Theme.textPrimary)
            }
            .frame(minHeight: 36)

            Divider().overlay(Theme.hairline.opacity(0.08))

            HStack {
                Text("Сохранённые кадры")
                    .font(.subheadline)
                    .foregroundStyle(Theme.textSecondary)
                Spacer()
                Text(Format.bytes(appState.thumbnails.totalBytes))
                    .font(.subheadline.weight(.medium).monospacedDigit())
                    .foregroundStyle(Theme.textPrimary)
            }
            .frame(minHeight: 36)

            Divider().overlay(Theme.hairline.opacity(0.08))

            Button {
                Haptics.warning()
                confirmClearMeta = true
            } label: {
                HStack {
                    Label("Очистить кэш метаданных", systemImage: "trash")
                        .font(.subheadline)
                        .foregroundStyle(Theme.danger)
                    Spacer()
                }
                .frame(minHeight: Theme.hit)
            }
            .buttonStyle(.plain)
        }
    }

    // MARK: - Infuse

    private var infuseCard: some View {
        SettingsCard(
            title: "Infuse",
            icon: "play.circle",
            tint: Theme.instant,
            footer: "Чтобы сразу освободить память после удаления сезона, очисти «Недавно удалённые» в приложении «Файлы»."
        ) {
            Label(appState.infuse.isInstalled ? "Infuse установлен" : "Установи Infuse для просмотра",
                  systemImage: appState.infuse.isInstalled ? "checkmark.circle.fill" : "arrow.down.app")
                .foregroundStyle(appState.infuse.isInstalled ? Theme.accent : Theme.textSecondary)
            Link("Infuse \(InfuseProtocol.minimumVersion) или новее · App Store", destination: InfuseLinks.appStore)
                .font(.footnote)
            Text("В Infuse включи «Продолжать воспроизведение» для памяти позиции и «Непрерывное воспроизведение» для серий подряд. Язык озвучки и субтитров также выбирается там.")
                .font(.footnote).foregroundStyle(Theme.textSecondary)
            Link("Как удалить скачанные серии", destination: URL(string: "https://support.firecore.com/hc/en-us/articles/215090957-Creating-Folders-and-Deleting-Videos")!)
                .font(.footnote)
        }
    }

    // MARK: - О приложении

    private var aboutCard: some View {
        SettingsCard(title: "Приложение", icon: "info.circle", tint: Theme.idle, footer: nil) {
            HStack {
                Text("Версия")
                    .font(.subheadline)
                    .foregroundStyle(Theme.textSecondary)
                Spacer()
                Text(versionText)
                    .font(.subheadline.weight(.medium).monospacedDigit())
                    .foregroundStyle(Theme.textPrimary)
            }
            .frame(minHeight: 36)

            Divider().overlay(Theme.hairline.opacity(0.08))

            Button {
                Haptics.tap()
                appState.didOnboard = false
            } label: {
                HStack {
                    Label("Показать приветствие снова", systemImage: "sparkles")
                        .font(.subheadline)
                        .foregroundStyle(Theme.textPrimary)
                    Spacer()
                }
                .frame(minHeight: Theme.hit)
            }
            .buttonStyle(.plain)

            if !appState.recentSearches.isEmpty {
                Divider().overlay(Theme.hairline.opacity(0.08))
                Button {
                    appState.clearRecentSearches()
                    appState.toasts.info("История поиска очищена")
                } label: {
                    HStack {
                        Label("Очистить историю поиска", systemImage: "clock.arrow.circlepath")
                            .font(.subheadline)
                            .foregroundStyle(Theme.textPrimary)
                        Spacer()
                    }
                    .frame(minHeight: Theme.hit)
                }
                .buttonStyle(.plain)
            }
        }
    }

    private var versionText: String {
        let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "—"
        let build = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "—"
        return "\(version) (\(build))"
    }

    #if DEBUG
    private var debugCard: some View {
        SettingsCard(title: "Отладка", icon: "ladybug", tint: Theme.danger, footer: nil) {
            NavigationLink {
                TrackerResultsView(query: "Демо-результаты", preloaded: .demo)
            } label: {
                HStack {
                    Text("Экран результатов на демо-данных")
                        .font(.subheadline)
                        .foregroundStyle(Theme.textPrimary)
                    Spacer()
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(Theme.textTertiary)
                }
                .frame(minHeight: Theme.hit)
            }
            .buttonStyle(.plain)
        }
    }
    #endif
}

/// Карточка настроек: заголовок с иконкой, содержимое, пояснение снизу.
struct SettingsCard<Content: View>: View {
    let title: String
    let icon: String
    var tint: Color = Theme.accent
    var footer: String?
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .font(.system(size: 13, weight: .bold))
                    .foregroundStyle(tint)
                Text(title)
                    .font(.subheadline.weight(.bold))
                    .foregroundStyle(Theme.textPrimary)
                    .textCase(.uppercase)
                    .kerning(0.4)
                Spacer(minLength: 0)
            }
            VStack(alignment: .leading, spacing: 10) {
                content
            }
            if let footer {
                Text(footer)
                    .font(.caption)
                    .foregroundStyle(Theme.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .cardSurface()
    }
}

/// Результат проверки подключения.
struct StatusLine: View {
    let status: CheckStatus

    var body: some View {
        switch status {
        case .idle, .checking:
            EmptyView()
        case .ok(let message):
            Label(message, systemImage: "checkmark.seal.fill")
                .font(.footnote.weight(.medium))
                .foregroundStyle(Theme.accent)
                .fixedSize(horizontal: false, vertical: true)
        case .fail(let message):
            Label(message, systemImage: "exclamationmark.triangle.fill")
                .font(.footnote.weight(.medium))
                .foregroundStyle(Theme.danger)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
