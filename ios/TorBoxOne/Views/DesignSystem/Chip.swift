import SwiftUI

/// Компактная метка «иконка + текст». Заменила эмодзи-бейджи: символы SF
/// масштабируются с Dynamic Type и читаются VoiceOver.
struct Chip: View {
    @Environment(\.colorScheme) private var scheme

    enum Style {
        /// Нейтральная: размер, источник.
        case plain
        /// Цветная полупрозрачная — основной вариант.
        case tinted(Color)
        /// Заливка цветом: одна-две на экран, для самого важного.
        case solid(Color)
    }

    let text: String
    var icon: String?
    var style: Style = .plain
    /// Метка для VoiceOver, если текст сам по себе непонятен («12» → «12 сидов»).
    var accessibility: String?

    var body: some View {
        HStack(spacing: 3.5) {
            if let icon {
                Image(systemName: icon)
                    .font(.system(size: 10, weight: .bold))
            }
            Text(text)
                .font(.caption2.weight(.semibold))
                .lineLimit(1)
        }
        .padding(.horizontal, 7)
        .padding(.vertical, 3.5)
        .background(background)
        .foregroundStyle(foreground)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibility ?? text)
    }

    @ViewBuilder
    private var background: some View {
        switch style {
        case .plain:
            Capsule().fill(Theme.hairline.opacity(0.08))
        case .tinted(let color):
            Capsule().fill(color.opacity(0.16))
        case .solid(let color):
            Capsule().fill(color)
        }
    }

    private var foreground: Color {
        switch style {
        case .plain: Theme.textSecondary
        case .tinted(let color): color
        // На светлой заливке (жёлтый «мгновенно» в тёмной схеме) белый текст не читается
        case .solid(let color): readableText(on: color)
        }
    }

    private func readableText(on fill: Color) -> Color {
        let traits = UITraitCollection(userInterfaceStyle: scheme == .dark ? .dark : .light)
        let resolved = UIColor(fill).resolvedColor(with: traits)
        var brightness: CGFloat = 0
        var alpha: CGFloat = 0
        resolved.getWhite(&brightness, alpha: &alpha)
        return brightness > 0.6 ? Color(rgb: 0x11161C) : .white
    }
}

// MARK: - Готовые чипы предметной области

extension Chip {
    /// Раздача в кэше TorBox: скачается мгновенно.
    static var instant: Chip {
        Chip(text: "мгновенно", icon: "bolt.fill", style: .solid(Theme.instant), accessibility: "Уже в кэше TorBox, добавится мгновенно")
    }

    static func seeders(_ count: Int) -> Chip {
        Chip(
            text: "\(count)",
            icon: "arrow.up.arrow.down",
            style: .tinted(count > 20 ? Theme.accent : count > 3 ? Theme.instant : Theme.danger),
            accessibility: "\(count) раздающих"
        )
    }

    static func size(_ bytes: Int64) -> Chip {
        Chip(text: Format.bytes(bytes), icon: "externaldrive", accessibility: "Размер \(Format.bytes(bytes))")
    }

    static func quality(_ text: String) -> Chip {
        let is4K = text.localizedCaseInsensitiveContains("4k") || text.contains("2160")
        return Chip(
            text: text,
            icon: is4K ? "4k.tv" : "tv",
            style: .tinted(Theme.quality),
            accessibility: "Качество: \(text)"
        )
    }

    static func voiceover(_ text: String) -> Chip {
        Chip(text: text, icon: "waveform", style: .tinted(Theme.violet), accessibility: "Озвучка: \(text)")
    }

    static func onDevice() -> Chip {
        Chip(text: "на телефоне", icon: "iphone", style: .tinted(Theme.info), accessibility: "Файл скачан в память телефона")
    }
}
