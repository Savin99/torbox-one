import UIKit

/// Тактильный отклик на ключевые действия: без него интерфейс кажется «плоским».
enum Haptics {
    static func tap() {
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
    }

    static func press() {
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
    }

    static func success() {
        UINotificationFeedbackGenerator().notificationOccurred(.success)
    }

    static func warning() {
        UINotificationFeedbackGenerator().notificationOccurred(.warning)
    }

    static func error() {
        UINotificationFeedbackGenerator().notificationOccurred(.error)
    }

    /// Щелчок при перетаскивании (скраб, слайдеры).
    static func tick() {
        UISelectionFeedbackGenerator().selectionChanged()
    }
}
