import SwiftUI

/// Первый запуск: раньше приложение сразу показывало пустые экраны с надписью
/// «загляни в настройки». Теперь оно за минуту доводит до рабочего состояния.
struct OnboardingView: View {
    @Environment(AppState.self) private var appState
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL

    @State private var step = 0
    @State private var keyInput = ""
    @State private var keyStatus: CheckStatus = .idle
    @State private var urlInput = ""
    @State private var tokenInput = ""
    @State private var searchStatus: CheckStatus = .idle

    private let lastStep = 3

    var body: some View {
        ZStack {
            AppBackground()
            VStack(spacing: 0) {
                header
                TabView(selection: $step) {
                    welcomePage.tag(0)
                    torBoxPage.tag(1)
                    searchPage.tag(2)
                    readyPage.tag(3)
                }
                .tabViewStyle(.page(indexDisplayMode: .never))
                .animation(.easeInOut(duration: 0.3), value: step)
            }
        }
        .preferredColorScheme(nil)
        .onAppear {
            keyInput = appState.torBoxKey
            urlInput = appState.searchAPIURL
            tokenInput = appState.searchAPIToken
            if appState.hasTorBoxKey { keyStatus = .ok("Ключ уже сохранён") }
            if appState.hasSearchAPI { searchStatus = .ok("Адрес уже сохранён") }
        }
    }

    // MARK: - Каркас

    private var header: some View {
        HStack {
            if step > 0 {
                Button {
                    Haptics.tap()
                    withAnimation { step -= 1 }
                } label: {
                    Image(systemName: "chevron.left")
                        .font(.headline)
                        .foregroundStyle(Theme.textSecondary)
                        .hitTarget()
                }
                .accessibilityLabel("Назад")
            }
            Spacer()
            HStack(spacing: 6) {
                ForEach(0...lastStep, id: \.self) { index in
                    Capsule()
                        .fill(index == step ? Theme.accent : Theme.hairline.opacity(0.18))
                        .frame(width: index == step ? 22 : 7, height: 7)
                        .animation(.spring(response: 0.3, dampingFraction: 0.8), value: step)
                }
            }
            .accessibilityHidden(true)
            Spacer()
            if step < lastStep {
                Button("Позже") {
                    Haptics.tap()
                    finish()
                }
                .font(.subheadline.weight(.medium))
                .foregroundStyle(Theme.textSecondary)
                .hitTarget()
            } else {
                Color.clear.frame(width: Theme.hit, height: Theme.hit)
            }
        }
        .padding(.horizontal, Theme.gutter)
        .padding(.top, 8)
    }

    private func page<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                content()
            }
            .padding(.horizontal, 24)
            .padding(.top, 24)
            .padding(.bottom, 40)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollBounceBehavior(.basedOnSize)
        .scrollDismissesKeyboard(.interactively)
    }

    // MARK: - 1. Приветствие

    private var welcomePage: some View {
        page {
            VStack(alignment: .leading, spacing: 18) {
                appGlyph
                VStack(alignment: .leading, spacing: 8) {
                    Text("TorBox One")
                        .font(.system(size: 40, weight: .heavy, design: .rounded))
                        .foregroundStyle(Theme.textPrimary)
                    Text("Найти фильм, скачать нужные серии в Infuse и смотреть без интернета.")
                        .font(.title3)
                        .foregroundStyle(Theme.textSecondary)
                }
                VStack(spacing: 12) {
                    featureRow(
                        icon: "magnifyingglass",
                        tint: Theme.violet,
                        title: "Поиск как в кинопоиске",
                        text: "Каталог с постерами, а под ним — раздачи с качеством и озвучками."
                    )
                    featureRow(
                        icon: "bolt.fill",
                        tint: Theme.instant,
                        title: "Мгновенно из кэша",
                        text: "Если раздача уже в TorBox, она появляется без ожидания."
                    )
                    featureRow(
                        icon: "play.rectangle.fill",
                        tint: Theme.accent,
                        title: "Просмотр в Infuse",
                        text: "Скачивай фильм, сезон или отдельные серии. Infuse воспроизводит локальные файлы и помнит, где ты остановился."
                    )
                }
                Button("Настроить за минуту") {
                    Haptics.press()
                    withAnimation { step = 1 }
                }
                .buttonStyle(AccentButtonStyle())
                .padding(.top, 4)
            }
        }
    }

    private var appGlyph: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 26, style: .continuous)
                .fill(
                    LinearGradient(
                        colors: [Theme.accent, Theme.accent.opacity(0.55), Theme.violet.opacity(0.7)],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )
                )
                .frame(width: 96, height: 96)
                .shadow(color: Theme.accent.opacity(0.35), radius: 24, y: 10)
            Image(systemName: "play.rectangle.on.rectangle.fill")
                .font(.system(size: 44, weight: .semibold))
                .foregroundStyle(.white)
        }
        .accessibilityHidden(true)
    }

    private func featureRow(icon: String, tint: Color, title: String, text: String) -> some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: icon)
                .font(.system(size: 18, weight: .semibold))
                .foregroundStyle(tint)
                .frame(width: 38, height: 38)
                .background(Circle().fill(tint.opacity(0.15)))
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.textPrimary)
                Text(text)
                    .font(.footnote)
                    .foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(14)
        .cardSurface(radius: Theme.radiusM)
    }

    // MARK: - 2. Ключ TorBox

    private var torBoxPage: some View {
        page {
            stepTitle(
                number: "Шаг 1",
                title: "Подключи TorBox",
                subtitle: "Ключ хранится в Keychain телефона и уходит только на api.torbox.app."
            )
            VStack(spacing: 12) {
                HStack(spacing: 10) {
                    SecureField("API-ключ TorBox", text: $keyInput)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(.callout)
                        .frame(minHeight: Theme.hit)
                    PasteButton(payloadType: String.self) { strings in
                        guard let pasted = strings.first else { return }
                        keyInput = pasted.trimmingCharacters(in: .whitespacesAndNewlines)
                        Haptics.tap()
                    }
                    .labelStyle(.iconOnly)
                    .buttonBorderShape(.circle)
                    .tint(Theme.accent)
                    .accessibilityLabel("Вставить ключ из буфера")
                }
                .padding(.horizontal, 14)
                .cardSurface(radius: Theme.radiusM)

                statusLine(keyStatus)

                Button {
                    Task { await checkKey() }
                } label: {
                    if keyStatus.isChecking {
                        ProgressView().tint(.white)
                    } else {
                        Text(keyStatus.isOK ? "Дальше" : "Проверить ключ")
                    }
                }
                .buttonStyle(AccentButtonStyle())
                .disabled(keyInput.trimmingCharacters(in: .whitespaces).isEmpty || keyStatus.isChecking)

                Button {
                    Haptics.tap()
                    if let url = URL(string: "https://torbox.app/settings") {
                        openURL(url)
                    }
                } label: {
                    Label("Где взять ключ", systemImage: "arrow.up.right.square")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(SoftButtonStyle(tint: Theme.textSecondary))
            }
        }
    }

    private func checkKey() async {
        keyStatus = .checking
        let result = await SetupChecks.torBox(key: keyInput)
        keyStatus = result
        if result.isOK {
            appState.torBoxKey = keyInput.trimmingCharacters(in: .whitespacesAndNewlines)
            Haptics.success()
            withAnimation { step = 2 }
        } else {
            Haptics.error()
        }
    }

    // MARK: - 3. Сервер поиска

    private var searchPage: some View {
        page {
            stepTitle(
                number: "Шаг 2",
                title: "Сервер поиска",
                subtitle: "Мини-API с каталогом и поиском по трекерам. Дома — адрес компьютера, в дороге — VPS."
            )
            VStack(spacing: 12) {
                VStack(spacing: 0) {
                    TextField("http://192.168.1.10:8787", text: $urlInput)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .font(.callout)
                        .frame(minHeight: Theme.hit)
                        .padding(.horizontal, 14)
                    Divider().overlay(Theme.hairline.opacity(0.1))
                    SecureField("Токен (если включён)", text: $tokenInput)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(.callout)
                        .frame(minHeight: Theme.hit)
                        .padding(.horizontal, 14)
                }
                .cardSurface(radius: Theme.radiusM)

                statusLine(searchStatus)

                Button {
                    Task { await checkSearch() }
                } label: {
                    if searchStatus.isChecking {
                        ProgressView().tint(.white)
                    } else {
                        Text(searchStatus.isOK ? "Дальше" : "Проверить сервер")
                    }
                }
                .buttonStyle(AccentButtonStyle())
                .disabled(urlInput.trimmingCharacters(in: .whitespaces).isEmpty || searchStatus.isChecking)

                Text("Без сервера поиска приложение всё равно работает: остаются библиотека, плеер и офлайн-файлы.")
                    .font(.footnote)
                    .foregroundStyle(Theme.textTertiary)
            }
        }
    }

    private func checkSearch() async {
        searchStatus = .checking
        let result = await SetupChecks.searchAPI(url: urlInput, token: tokenInput)
        searchStatus = result
        if result.isOK {
            appState.searchAPIURL = urlInput.trimmingCharacters(in: .whitespacesAndNewlines)
            appState.searchAPIToken = tokenInput
            Haptics.success()
            withAnimation { step = 3 }
        } else {
            Haptics.error()
        }
    }

    // MARK: - 4. Готово

    private var readyPage: some View {
        page {
            VStack(alignment: .leading, spacing: 18) {
                ZStack {
                    Circle()
                        .fill(Theme.accent.opacity(0.18))
                        .frame(width: 96, height: 96)
                    Image(systemName: "checkmark")
                        .font(.system(size: 44, weight: .bold))
                        .foregroundStyle(Theme.accent)
                }
                .accessibilityHidden(true)
                Text(appState.isFullyConfigured ? "Всё готово" : "Почти готово")
                    .font(.system(size: 34, weight: .heavy, design: .rounded))
                    .foregroundStyle(Theme.textPrimary)
                VStack(spacing: 10) {
                    checklistRow(done: appState.hasTorBoxKey, title: "TorBox подключён", hint: "Облачная библиотека и ссылки на видео")
                    checklistRow(done: appState.hasSearchAPI, title: "Сервер поиска подключён", hint: "Каталог и поиск по трекерам")
                    checklistRow(done: appState.infuse.isInstalled, title: "Infuse установлен", hint: "Версия \(InfuseProtocol.minimumVersion) или новее для скачивания выбранных файлов")
                    Link("Установить или обновить Infuse", destination: InfuseLinks.appStore)
                }
                Text("Настройки можно поменять в любой момент во вкладке «Настройки».")
                    .font(.footnote)
                    .foregroundStyle(Theme.textTertiary)
                Button("Поехали") {
                    Haptics.press()
                    finish()
                }
                .buttonStyle(AccentButtonStyle())
            }
        }
    }

    private func checklistRow(done: Bool, title: String, hint: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: done ? "checkmark.circle.fill" : "circle.dashed")
                .font(.title3)
                .foregroundStyle(done ? Theme.accent : Theme.textTertiary)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.textPrimary)
                Text(hint)
                    .font(.caption)
                    .foregroundStyle(Theme.textSecondary)
            }
            Spacer(minLength: 0)
        }
        .padding(14)
        .cardSurface(radius: Theme.radiusM)
    }

    // MARK: - Общее

    private func stepTitle(number: String, title: String, subtitle: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(number)
                .font(.caption.weight(.bold))
                .foregroundStyle(Theme.accent)
            Text(title)
                .font(.system(size: 30, weight: .heavy, design: .rounded))
                .foregroundStyle(Theme.textPrimary)
            Text(subtitle)
                .font(.callout)
                .foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder
    private func statusLine(_ status: CheckStatus) -> some View {
        switch status {
        case .idle, .checking:
            EmptyView()
        case .ok(let message):
            Label(message, systemImage: "checkmark.seal.fill")
                .font(.footnote.weight(.medium))
                .foregroundStyle(Theme.accent)
                .frame(maxWidth: .infinity, alignment: .leading)
        case .fail(let message):
            Label(message, systemImage: "exclamationmark.triangle.fill")
                .font(.footnote.weight(.medium))
                .foregroundStyle(Theme.danger)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func finish() {
        appState.didOnboard = true
        dismiss()
    }
}
