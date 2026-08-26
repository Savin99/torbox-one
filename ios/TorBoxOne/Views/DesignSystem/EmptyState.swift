import SwiftUI

/// Пустое состояние с понятным следующим шагом: у каждого «пусто» есть кнопка.
struct EmptyState: View {
    let icon: String
    let title: String
    var message: String?
    var actionTitle: String?
    var action: (() -> Void)?
    var tint: Color = Theme.accent

    var body: some View {
        VStack(spacing: 14) {
            ZStack {
                Circle()
                    .fill(
                        LinearGradient(
                            colors: [tint.opacity(0.22), tint.opacity(0.06)],
                            startPoint: .top,
                            endPoint: .bottom
                        )
                    )
                    .frame(width: 92, height: 92)
                Image(systemName: icon)
                    .font(.system(size: 38, weight: .medium))
                    .foregroundStyle(tint)
            }
            Text(title)
                .font(.title3.weight(.semibold))
                .foregroundStyle(Theme.textPrimary)
                .multilineTextAlignment(.center)
            if let message {
                Text(message)
                    .font(.callout)
                    .foregroundStyle(Theme.textSecondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 320)
            }
            if let actionTitle, let action {
                Button(actionTitle) {
                    Haptics.tap()
                    action()
                }
                .buttonStyle(AccentButtonStyle(tint: tint, fullWidth: false))
                .padding(.top, 4)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 24)
        .padding(.vertical, 32)
    }
}

/// Заголовок секции: единый ритм на всех экранах, опциональное действие справа.
struct SectionHeader: View {
    let title: String
    var subtitle: String?
    var actionTitle: String?
    var action: (() -> Void)?

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.title3.weight(.bold))
                    .foregroundStyle(Theme.textPrimary)
                if let subtitle {
                    Text(subtitle)
                        .font(.caption)
                        .foregroundStyle(Theme.textSecondary)
                }
            }
            Spacer(minLength: 8)
            if let actionTitle, let action {
                Button(actionTitle) {
                    Haptics.tap()
                    action()
                }
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(Theme.accent)
            }
        }
    }
}
