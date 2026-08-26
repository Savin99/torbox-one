import SwiftUI

/// Ручное добавление магнета: из буфера обмена или magnet:-ссылки,
/// открытой в другом приложении.
struct AddMagnetSheet: View {
    @Environment(AppState.self) private var appState
    @Environment(\.dismiss) private var dismiss
    let vm: DownloadsViewModel

    @State private var magnet = ""
    @State private var isAdding = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            ZStack {
                AppBackground()
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        Text("Вставь magnet-ссылку — раздача уйдёт в TorBox и появится в списке.")
                            .font(.callout)
                            .foregroundStyle(Theme.textSecondary)

                        TextEditor(text: $magnet)
                            .font(.footnote.monospaced())
                            .scrollContentBackground(.hidden)
                            .frame(minHeight: 110)
                            .padding(10)
                            .cardSurface(radius: Theme.radiusM)
                            .autocorrectionDisabled()
                            .textInputAutocapitalization(.never)

                        if let name = parsedName {
                            HStack(spacing: 8) {
                                Image(systemName: "checkmark.seal.fill")
                                    .foregroundStyle(Theme.accent)
                                Text(name)
                                    .font(.footnote.weight(.medium))
                                    .foregroundStyle(Theme.textPrimary)
                                    .lineLimit(2)
                            }
                        }

                        if let error {
                            Label(error, systemImage: "exclamationmark.triangle.fill")
                                .font(.footnote)
                                .foregroundStyle(Theme.danger)
                        }

                        HStack {
                            Text("Ссылка в буфере?")
                                .font(.footnote)
                                .foregroundStyle(Theme.textSecondary)
                            Spacer(minLength: 8)
                            PasteButton(payloadType: String.self) { strings in
                                guard let pasted = strings.first else { return }
                                magnet = pasted.trimmingCharacters(in: .whitespacesAndNewlines)
                                Haptics.tap()
                            }
                            .buttonBorderShape(.capsule)
                            .tint(Theme.accent)
                        }

                        Button {
                            add()
                        } label: {
                            if isAdding {
                                ProgressView().tint(.white)
                            } else {
                                Text("Добавить в TorBox")
                            }
                        }
                        .buttonStyle(AccentButtonStyle())
                        .disabled(!isValid || isAdding)
                    }
                    .padding(Theme.gutter)
                }
            }
            .navigationTitle("Магнет")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Отмена") { close() }
                }
            }
        }
        .onAppear {
            // Только явный магнет извне: читать буфер молча нельзя —
            // iOS покажет диалог «разрешить вставку», для этого есть PasteButton
            if let pending = appState.pendingMagnet {
                magnet = pending
            }
        }
    }

    private var trimmed: String {
        magnet.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var isValid: Bool {
        trimmed.hasPrefix("magnet:") && trimmed.contains("xt=urn:btih:")
    }

    private var parsedName: String? {
        guard isValid else { return nil }
        let name = DownloadsViewModel.magnetName(trimmed)
        return name.isEmpty ? nil : name
    }

    private func add() {
        guard let client = appState.torBoxClient else {
            error = "Сначала подключи TorBox в настройках — магнету некуда уйти."
            Haptics.warning()
            return
        }
        isAdding = true
        error = nil
        Task {
            defer { isAdding = false }
            do {
                try await vm.addMagnet(trimmed, client: client)
                appState.toasts.success("Магнет добавлен", message: parsedName)
                close()
            } catch {
                Haptics.error()
                self.error = error.localizedDescription
            }
        }
    }

    private func close() {
        appState.pendingMagnet = nil
        dismiss()
    }
}
