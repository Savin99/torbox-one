import Foundation

enum AppError: LocalizedError {
    case torBox(detail: String)
    case searchAPI(status: Int)
    case badResponse
    case notConfigured

    var errorDescription: String? {
        switch self {
        case .torBox(let detail): "TorBox: \(detail)"
        case .searchAPI(let status): "Сервер поиска ответил ошибкой (HTTP \(status))"
        case .badResponse: "Некорректный ответ сервера"
        case .notConfigured: "Сначала заполни настройки"
        }
    }
}
