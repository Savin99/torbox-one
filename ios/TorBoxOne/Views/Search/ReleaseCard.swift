import SwiftUI

/// Карточка раздачи: название и всё, что важно для выбора. Кнопка «добавить»
/// живёт отдельным слоем поверх — вложенная в NavigationLink она глотала нажатия.
struct ReleaseCard: View {
    let release: ReleaseResult
    let isCached: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 8) {
                Text(release.title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(3)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)

                if isCached || release.resolutionLabel != nil || release.hasVoiceover {
                    FlowLayout(spacing: 6) {
                        if isCached { Chip.instant }
                        if let resolution = release.resolutionLabel {
                            Chip.quality(resolution)
                        }
                        if release.hasVoiceover {
                            Chip.voiceover(shortVoiceover)
                        }
                    }
                }

                FlowLayout(spacing: 10) {
                    metric(icon: "externaldrive", text: release.sizeText, tint: Theme.textSecondary)
                    metric(
                        icon: "arrow.up.arrow.down",
                        text: "\(release.seeders)",
                        tint: seedTint,
                        accessibility: "\(release.seeders) раздающих"
                    )
                    metric(icon: "globe", text: release.sourceName, tint: Theme.textTertiary)
                }
            }
            Spacer(minLength: 0)
            // Место под кнопку «добавить», которая рисуется поверх карточки
            Color.clear.frame(width: 44, height: 1)
        }
        .padding(14)
        .cardSurface()
        .contentShape(Rectangle())
    }

    /// Список озвучек бывает длинным — в карточке хватает первой и счётчика.
    private var shortVoiceover: String {
        let parts = release.voiceoverSummary
            .components(separatedBy: CharacterSet(charactersIn: ",·/"))
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        guard let first = parts.first else { return release.voiceoverSummary }
        return parts.count > 1 ? "\(first) +\(parts.count - 1)" : first
    }

    private var seedTint: Color {
        release.seeders > 20 ? Theme.accent : release.seeders > 3 ? Theme.instant : Theme.danger
    }

    private func metric(icon: String, text: String, tint: Color, accessibility: String? = nil) -> some View {
        HStack(spacing: 4) {
            Image(systemName: icon)
                .font(.system(size: 10, weight: .semibold))
            Text(text)
                .font(.caption.weight(.medium))
                .lineLimit(1)
        }
        .foregroundStyle(tint)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibility ?? text)
    }

}

/// Кнопка «добавить в TorBox» с состояниями — отдельным слоем над карточкой.
struct ReleaseAddButton: View {
    let isCached: Bool
    let state: TrackerResultsViewModel.AddState
    let onAdd: () -> Void

    var body: some View {
        switch state {
        case .adding:
            ProgressView()
                .frame(width: 44, height: 44)
        case .added:
            Image(systemName: "checkmark.circle.fill")
                .font(.title2)
                .foregroundStyle(Theme.accent)
                .frame(width: 44, height: 44)
                .accessibilityLabel("Уже добавлено в TorBox")
        case .idle, .failed:
            Button {
                onAdd()
            } label: {
                Image(systemName: "plus")
                    .font(.headline.weight(.bold))
                    .foregroundStyle(isCached ? Theme.instant : Theme.accent)
            }
            .buttonStyle(CircleIconButtonStyle(diameter: 44, material: false))
            .accessibilityLabel("Добавить в TorBox")
        }
    }
}
