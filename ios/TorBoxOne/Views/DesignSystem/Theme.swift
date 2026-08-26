import SwiftUI
import UIKit

/// Единственный источник цветов, радиусов и отступов. Тёмная схема — основная
/// («кинозал»), светлая — полноценная: каждый токен задан парой значений.
nonisolated enum Theme {
    // MARK: - Смысловые акценты

    /// Смотреть / готово / основное действие.
    static let accent = Color(light: 0x00A05F, dark: 0x22D98C)
    /// Раздача уже в кэше TorBox — стартует мгновенно.
    static let instant = Color(light: 0xB27700, dark: 0xFFC94D)
    /// Файл на телефоне, скорость, ETA.
    static let info = Color(light: 0x0A63E8, dark: 0x62ABFF)
    /// Озвучки и дорожки.
    static let violet = Color(light: 0x6E36D6, dark: 0xB694FF)
    /// Разрешение и качество видео.
    static let quality = Color(light: 0x0D7A84, dark: 0x54DCE8)
    /// Внимание: ошибки, нехватка места, мёртвые раздачи.
    static let danger = Color(light: 0xC7362B, dark: 0xFF7062)
    /// Ожидание, пауза.
    static let idle = Color(light: 0x6A7280, dark: 0x9AA4B2)

    // MARK: - Поверхности

    static let canvasTop = Color(light: 0xF7F8FA, dark: 0x090C0F)
    static let canvasBottom = Color(light: 0xEBEEF2, dark: 0x141A20)
    static let card = Color(light: 0xFFFFFF, dark: 0x171D24)
    static let cardRaised = Color(light: 0xFFFFFF, dark: 0x1E252D)
    static let hairline = Color(light: 0x0B1220, dark: 0xFFFFFF)
    static let shadow = Color(light: 0x0B1220, dark: 0x000000)

    // MARK: - Текст

    static let textPrimary = Color(light: 0x0B1220, dark: 0xF2F5F8)
    static let textSecondary = Color(light: 0x5B6474, dark: 0x9BA6B4)
    static let textTertiary = Color(light: 0x8A93A3, dark: 0x6E7885)

    // MARK: - Метрики

    /// Радиус карточек и крупных блоков.
    static let radiusL: CGFloat = 20
    /// Радиус вложенных элементов: постеры в строках, кнопки.
    static let radiusM: CGFloat = 14
    static let radiusS: CGFloat = 10

    /// Минимальная площадь нажатия по HIG.
    static let hit: CGFloat = 44

    static let gutter: CGFloat = 16
    static let rowSpacing: CGFloat = 12

    /// Отступ снизу, чтобы контент не залезал под таб-бар.
    static let tabBarInset: CGFloat = 48
}

// MARK: - Фон приложения

/// Фирменный фон: вертикальный градиент плюс мягкое свечение акцента сверху.
struct AppBackground: View {
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        ZStack {
            LinearGradient(
                colors: [Theme.canvasTop, Theme.canvasBottom],
                startPoint: .top,
                endPoint: .bottom
            )
            RadialGradient(
                colors: [Theme.accent.opacity(scheme == .dark ? 0.16 : 0.07), .clear],
                center: .init(x: 0.85, y: -0.05),
                startRadius: 0,
                endRadius: 420
            )
            RadialGradient(
                colors: [Theme.violet.opacity(scheme == .dark ? 0.12 : 0.05), .clear],
                center: .init(x: 0.05, y: 0.12),
                startRadius: 0,
                endRadius: 360
            )
        }
        .ignoresSafeArea()
    }
}

// MARK: - Карточки

private struct CardModifier: ViewModifier {
    let radius: CGFloat
    let raised: Bool

    func body(content: Content) -> some View {
        content
            .background(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .fill(raised ? Theme.cardRaised : Theme.card)
            )
            .overlay(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .strokeBorder(Theme.hairline.opacity(0.08), lineWidth: 0.5)
            )
            .shadow(color: Theme.shadow.opacity(0.10), radius: raised ? 14 : 8, x: 0, y: raised ? 8 : 4)
    }
}

extension View {
    /// Карточка: подложка, волосяная обводка, мягкая тень.
    func cardSurface(radius: CGFloat = Theme.radiusL, raised: Bool = false) -> some View {
        modifier(CardModifier(radius: radius, raised: raised))
    }

    /// Строка списка внутри `List` без штатной подложки и разделителей.
    func plainListRow(insets: EdgeInsets = EdgeInsets(top: 6, leading: Theme.gutter, bottom: 6, trailing: Theme.gutter)) -> some View {
        listRowBackground(Color.clear)
            .listRowSeparator(.hidden)
            .listRowInsets(insets)
    }

    /// Список на фирменном фоне: без системной подложки, с чистыми строками.
    func themedList() -> some View {
        scrollContentBackground(.hidden)
            .listStyle(.plain)
            .background(AppBackground())
    }

    /// Гарантирует площадь нажатия 44×44 даже у иконки 17 pt.
    func hitTarget(_ size: CGFloat = Theme.hit) -> some View {
        frame(minWidth: size, minHeight: size)
            .contentShape(Rectangle())
    }
}

// MARK: - Цвет из hex

extension Color {
    /// Пара значений на схему: приложение выглядит одинаково продуманно в обеих.
    /// `nonisolated`: UIKit дёргает провайдер динамического цвета не только на main.
    nonisolated init(light: UInt32, dark: UInt32) {
        self = Color(uiColor: UIColor { traits in
            UIColor(rgb: traits.userInterfaceStyle == .dark ? dark : light)
        })
    }

    nonisolated init(rgb: UInt32) {
        self = Color(uiColor: UIColor(rgb: rgb))
    }
}

extension UIColor {
    nonisolated convenience init(rgb: UInt32) {
        self.init(
            red: CGFloat((rgb >> 16) & 0xFF) / 255,
            green: CGFloat((rgb >> 8) & 0xFF) / 255,
            blue: CGFloat(rgb & 0xFF) / 255,
            alpha: 1
        )
    }
}
