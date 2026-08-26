import SwiftUI

/// Круговой индикатор: в строке загрузки читается быстрее полосы.
struct ProgressRing: View {
    let fraction: Double
    var tint: Color = Theme.accent
    var diameter: CGFloat = 38
    var lineWidth: CGFloat = 4
    /// Символ поверх кольца вместо процентов (галочка, пауза).
    var symbol: String?

    var body: some View {
        ZStack {
            Circle()
                .stroke(Theme.hairline.opacity(0.12), lineWidth: lineWidth)
            Circle()
                .trim(from: 0, to: max(0.005, min(fraction, 1)))
                .stroke(
                    AngularGradient(colors: [tint.opacity(0.65), tint], center: .center),
                    style: StrokeStyle(lineWidth: lineWidth, lineCap: .round)
                )
                .rotationEffect(.degrees(-90))
                .animation(.easeOut(duration: 0.35), value: fraction)
            if let symbol {
                Image(systemName: symbol)
                    .font(.system(size: diameter * 0.38, weight: .bold))
                    .foregroundStyle(tint)
            } else {
                Text("\(Int((fraction * 100).rounded()))")
                    .font(.system(size: diameter * 0.30, weight: .bold, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(Theme.textPrimary)
                    .contentTransition(.numericText())
            }
        }
        .frame(width: diameter, height: diameter)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(symbol == nil ? "Готово \(Int(fraction * 100)) процентов" : "")
    }
}

/// Тонкая полоса прогресса с закруглением — для карточек «продолжить просмотр».
struct SlimProgressBar: View {
    let fraction: Double
    var tint: Color = Theme.accent
    var height: CGFloat = 4

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Theme.hairline.opacity(0.14))
                Capsule()
                    .fill(LinearGradient(colors: [tint.opacity(0.75), tint], startPoint: .leading, endPoint: .trailing))
                    .frame(width: max(height, geo.size.width * max(0, min(fraction, 1))))
            }
        }
        .frame(height: height)
        .accessibilityHidden(true)
    }
}
