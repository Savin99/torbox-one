import SwiftUI

/// Один лист настроек воспроизведения вместо трёх иконок в шапке:
/// дорожки, субтитры, скорость и картинка — там, где их ищут.
struct PlayerSheetView: View {
    let vm: PlayerViewModel
    let kind: PlayerSheet

    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ZStack {
                Theme.canvasBottom.ignoresSafeArea()
                content
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Готово") { dismiss() }
                }
            }
        }
        .presentationDetents([.medium, .large])
        .presentationBackground(Theme.canvasBottom)
        .preferredColorScheme(.dark)
    }

    private var title: String {
        switch kind {
        case .settings: "Воспроизведение"
        case .queue: "Серии"
        }
    }

    @ViewBuilder
    private var content: some View {
        switch kind {
        case .settings:
            settingsList
        case .queue:
            queueList
        }
    }

    // MARK: - Настройки

    private var settingsList: some View {
        List {
            if !vm.audioChoices.isEmpty {
                Section("Звуковая дорожка") {
                    ForEach(vm.audioChoices) { choice in
                        row(
                            title: choice.name,
                            subtitle: language(choice),
                            selected: choice.id == vm.selectedAudioID
                        ) {
                            vm.select(audioID: choice.id)
                        }
                    }
                }
            }

            if !vm.textChoices.isEmpty {
                Section("Субтитры") {
                    row(title: "Выключены", subtitle: nil, selected: vm.selectedTextID == nil) {
                        vm.selectText(id: nil)
                    }
                    ForEach(vm.textChoices) { choice in
                        row(
                            title: choice.name,
                            subtitle: language(choice),
                            selected: choice.id == vm.selectedTextID
                        ) {
                            vm.selectText(id: choice.id)
                        }
                    }
                }
            }

            Section("Скорость") {
                Picker("Скорость", selection: Binding(
                    get: { vm.rate },
                    set: { vm.rate = $0; Haptics.tap() }
                )) {
                    ForEach([Float(0.75), 1.0, 1.25, 1.5, 2.0], id: \.self) { value in
                        Text(value == 1.0 ? "1×" : String(format: "%g×", value)).tag(value)
                    }
                }
                .pickerStyle(.segmented)
                .listRowBackground(Color.clear)
            }

            Section("Экран") {
                Button {
                    vm.toggleFill()
                } label: {
                    HStack {
                        Label(
                            vm.isFillMode ? "Вписать в экран" : "Заполнить экран",
                            systemImage: vm.isFillMode ? "rectangle" : "rectangle.inset.filled"
                        )
                        .foregroundStyle(Theme.textPrimary)
                        Spacer()
                    }
                    .frame(minHeight: Theme.hit)
                }
                .buttonStyle(.plain)
                Button {
                    vm.isLocked = true
                    Haptics.press()
                    dismiss()
                } label: {
                    HStack {
                        Label("Заблокировать касания", systemImage: "lock.fill")
                            .foregroundStyle(Theme.textPrimary)
                        Spacer()
                    }
                    .frame(minHeight: Theme.hit)
                }
                .buttonStyle(.plain)
            }
        }
        .listStyle(.insetGrouped)
        .scrollContentBackground(.hidden)
    }

    // MARK: - Серии

    private var queueList: some View {
        List {
            ForEach(Array(vm.queue.items.enumerated()), id: \.element.id) { index, item in
                row(
                    title: item.title,
                    subtitle: item.episodeLabel,
                    selected: index == vm.queue.index
                ) {
                    vm.play(at: index)
                    dismiss()
                }
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
    }

    // MARK: - Общее

    private func language(_ choice: PlayerViewModel.TrackChoice) -> String? {
        guard let language = choice.language, !language.isEmpty,
              !["und", "undefined", "unknown"].contains(language.lowercased()),
              !choice.name.localizedCaseInsensitiveContains(language) else { return nil }
        return language
    }

    private func row(
        title: String,
        subtitle: String?,
        selected: Bool,
        action: @escaping () -> Void
    ) -> some View {
        Button {
            action()
        } label: {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.body.weight(selected ? .semibold : .regular))
                        .foregroundStyle(Theme.textPrimary)
                    if let subtitle {
                        Text(subtitle)
                            .font(.caption)
                            .foregroundStyle(Theme.textSecondary)
                    }
                }
                Spacer(minLength: 8)
                if selected {
                    Image(systemName: "checkmark")
                        .font(.headline)
                        .foregroundStyle(Theme.accent)
                }
            }
            .frame(minHeight: Theme.hit)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? [.isSelected, .isButton] : .isButton)
    }
}
