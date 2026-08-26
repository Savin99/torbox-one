import SwiftUI

/// Главная кнопка экрана: градиент акцента, отклик на нажатие, хаптика.
struct AccentButtonStyle: ButtonStyle {
    /// Выключенную кнопку нужно гасить: иначе она выглядит рабочей.
    @Environment(\.isEnabled) private var isEnabled
    var tint: Color = Theme.accent
    var fullWidth = true

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.headline)
            .foregroundStyle(.white)
            .frame(maxWidth: fullWidth ? .infinity : nil, minHeight: 52)
            .padding(.horizontal, 20)
            .background(
                RoundedRectangle(cornerRadius: Theme.radiusM, style: .continuous)
                    .fill(
                        LinearGradient(
                            colors: [tint, tint.opacity(0.82)],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        )
                    )
            )
            .shadow(color: tint.opacity(configuration.isPressed ? 0.15 : 0.35), radius: 12, x: 0, y: 6)
            .opacity(isEnabled ? 1 : 0.4)
            .scaleEffect(configuration.isPressed ? 0.975 : 1)
            .animation(.spring(response: 0.28, dampingFraction: 0.7), value: configuration.isPressed)
    }
}

/// Второстепенное действие: стеклянная подложка, тот же ритм.
struct SoftButtonStyle: ButtonStyle {
    var tint: Color = Theme.textPrimary

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(tint)
            .frame(minHeight: Theme.hit)
            .padding(.horizontal, 16)
            .background(
                RoundedRectangle(cornerRadius: Theme.radiusM, style: .continuous)
                    .fill(Theme.hairline.opacity(configuration.isPressed ? 0.14 : 0.07))
            )
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .animation(.spring(response: 0.26, dampingFraction: 0.72), value: configuration.isPressed)
    }
}

/// Круглая иконочная кнопка (плеер, тулбары поверх постеров).
struct CircleIconButtonStyle: ButtonStyle {
    var diameter: CGFloat = Theme.hit
    var material: Bool = true

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .frame(width: diameter, height: diameter)
            .background {
                if material {
                    Circle().fill(.ultraThinMaterial)
                } else {
                    Circle().fill(Theme.hairline.opacity(0.1))
                }
            }
            .contentShape(Circle())
            .scaleEffect(configuration.isPressed ? 0.9 : 1)
            .animation(.spring(response: 0.24, dampingFraction: 0.7), value: configuration.isPressed)
    }
}
