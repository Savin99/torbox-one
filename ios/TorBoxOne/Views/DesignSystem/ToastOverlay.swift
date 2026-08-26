import SwiftUI

/// Плашка сверху: иконка, текст, необязательное действие. Смахивается вверх.
struct ToastOverlay: View {
    let toasts: ToastCenter

    @State private var dragOffset: CGFloat = 0

    var body: some View {
        VStack {
            if let toast = toasts.current {
                banner(toast)
                    .offset(y: min(0, dragOffset))
                    .gesture(
                        DragGesture()
                            .onChanged { value in
                                dragOffset = value.translation.height
                            }
                            .onEnded { value in
                                if value.translation.height < -20 {
                                    toasts.dismiss()
                                }
                                dragOffset = 0
                            }
                    )
                    .transition(.move(edge: .top).combined(with: .opacity))
                    .id(toast.id)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .animation(.spring(response: 0.38, dampingFraction: 0.82), value: toasts.current?.id)
    }

    private func banner(_ toast: ToastCenter.Toast) -> some View {
        HStack(spacing: 12) {
            Image(systemName: toast.kind.icon)
                .font(.title3)
                .foregroundStyle(tint(toast.kind))
            VStack(alignment: .leading, spacing: 2) {
                Text(toast.title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.textPrimary)
                if let message = toast.message, !message.isEmpty {
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(Theme.textSecondary)
                        .lineLimit(3)
                }
            }
            Spacer(minLength: 4)
            if let actionTitle = toast.actionTitle, let action = toast.action {
                Button(actionTitle) {
                    Haptics.tap()
                    toasts.dismiss()
                    action()
                }
                .font(.subheadline.weight(.bold))
                .foregroundStyle(tint(toast.kind))
                .padding(.horizontal, 10)
                .frame(minHeight: Theme.hit - 8)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .background(
            RoundedRectangle(cornerRadius: Theme.radiusM, style: .continuous)
                .fill(.regularMaterial)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.radiusM, style: .continuous)
                .strokeBorder(tint(toast.kind).opacity(0.35), lineWidth: 1)
        )
        .shadow(color: Theme.shadow.opacity(0.25), radius: 18, y: 8)
        .onTapGesture { toasts.dismiss() }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isStaticText)
    }

    private func tint(_ kind: ToastCenter.Toast.Kind) -> Color {
        switch kind {
        case .success: Theme.accent
        case .error: Theme.danger
        case .info: Theme.info
        }
    }
}
