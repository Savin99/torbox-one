import Foundation
import Observation

/// Единая точка коротких сообщений. Раньше ошибки жили тремя способами:
/// алертом, оранжевой строкой в списке и молча — теперь всегда одинаково.
@Observable
final class ToastCenter {
    struct Toast: Identifiable {
        enum Kind {
            case success
            case error
            case info

            var icon: String {
                switch self {
                case .success: "checkmark.circle.fill"
                case .error: "exclamationmark.triangle.fill"
                case .info: "info.circle.fill"
                }
            }
        }

        let id = UUID()
        let kind: Kind
        let title: String
        var message: String?
        var actionTitle: String?
        var action: (() -> Void)?
    }

    private(set) var current: Toast?

    private var dismissTask: Task<Void, Never>?

    func success(_ title: String, message: String? = nil, actionTitle: String? = nil, action: (() -> Void)? = nil) {
        Haptics.success()
        show(Toast(kind: .success, title: title, message: message, actionTitle: actionTitle, action: action))
    }

    func error(_ title: String, message: String? = nil, actionTitle: String? = nil, action: (() -> Void)? = nil) {
        Haptics.error()
        show(Toast(kind: .error, title: title, message: message, actionTitle: actionTitle, action: action))
    }

    func info(_ title: String, message: String? = nil, actionTitle: String? = nil, action: (() -> Void)? = nil) {
        show(Toast(kind: .info, title: title, message: message, actionTitle: actionTitle, action: action))
    }

    /// Ошибка из `catch`: заголовок человеческий, детали — из системы.
    func failure(_ title: String, _ error: any Error) {
        self.error(title, message: error.localizedDescription)
    }

    func dismiss() {
        dismissTask?.cancel()
        dismissTask = nil
        current = nil
    }

    private func show(_ toast: Toast) {
        current = toast
        dismissTask?.cancel()
        // С действием держим дольше: на кнопку нужно успеть нажать
        var seconds = toast.actionTitle == nil ? 3.2 : 5.0
        #if DEBUG
        // Для осмотра интерфейса на симуляторе задержку можно продлить
        let hold = UserDefaults.standard.double(forKey: "toastHoldSeconds")
        if hold > 0 { seconds = hold }
        #endif
        dismissTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(seconds))
            guard !Task.isCancelled else { return }
            self?.current = nil
        }
    }
}
