import Foundation

/// Состояние проверки подключения. Один тип для онбординга и настроек.
enum CheckStatus: Equatable {
    case idle
    case checking
    case ok(String)
    case fail(String)

    var isOK: Bool {
        if case .ok = self { return true }
        return false
    }

    var isChecking: Bool { self == .checking }
}

/// Проверки кредов: живой ответ сервиса вместо «сохранено, надеемся, что верно».
enum SetupChecks {
    static func torBox(key: String) async -> CheckStatus {
        let trimmed = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return .fail("Ключ пустой") }
        do {
            let torrents = try await TorBoxClient(apiKey: trimmed).myList()
            let word = Format.plural(torrents.count, "загрузка", "загрузки", "загрузок")
            return .ok("Ключ работает · \(torrents.count) \(word) в аккаунте")
        } catch {
            return .fail(error.localizedDescription)
        }
    }

    static func searchAPI(url: String, token: String) async -> CheckStatus {
        let trimmed = url.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let parsed = URL(string: trimmed), parsed.host() != nil else {
            return .fail("Похоже, это не адрес сервера")
        }
        do {
            try await SearchAPIClient(baseURL: parsed, token: token).health()
            return .ok("Сервер отвечает")
        } catch {
            return .fail(error.localizedDescription)
        }
    }
}
