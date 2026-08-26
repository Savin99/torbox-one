import Foundation

/// Память выбранной дорожки и субтитров для раздачи: выбрал озвучку в первой
/// серии — следующие включаются с ней же. Ключ — хеш раздачи из ключа просмотра.
enum TrackPreferences {
    private static let audioKey = "trackAudioPreference"
    private static let textKey = "trackTextPreference"

    /// «hash:fileID» → «hash»: настройка живёт на всю раздачу, а не на серию.
    static func group(for itemID: String) -> String {
        itemID.split(separator: ":").first.map(String.init) ?? itemID
    }

    static func audio(for itemID: String) -> String? {
        map(audioKey)[group(for: itemID)]
    }

    static func text(for itemID: String) -> String? {
        map(textKey)[group(for: itemID)]
    }

    /// Пустая строка означает «субтитры выключены» — это тоже осознанный выбор.
    static func setAudio(_ name: String?, for itemID: String) {
        store(audioKey, name: name, group: group(for: itemID))
    }

    static func setText(_ name: String?, for itemID: String) {
        store(textKey, name: name ?? "", group: group(for: itemID))
    }

    private static func map(_ key: String) -> [String: String] {
        UserDefaults.standard.dictionary(forKey: key) as? [String: String] ?? [:]
    }

    private static func store(_ key: String, name: String?, group: String) {
        var current = map(key)
        if let name {
            current[group] = name
        } else {
            current.removeValue(forKey: group)
        }
        UserDefaults.standard.set(current, forKey: key)
    }
}
