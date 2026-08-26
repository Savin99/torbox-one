import Foundation

/// Чистые форматтеры — порт human_size / human_duration / progress_percent / status_label из бота.
/// `nonisolated`: ими пользуются и модели, которые живут вне MainActor.
nonisolated enum Format {
    static func bytes(_ value: Int64) -> String {
        let units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
        var size = Double(value)
        for (index, unit) in units.enumerated() {
            if size < 1024 || index == units.count - 1 {
                return index == 0 ? "\(Int(size)) \(unit)" : String(format: "%.1f %@", size, unit)
            }
            size /= 1024
        }
        return "\(value) Б"
    }

    static func speed(_ bytesPerSecond: Int64) -> String {
        "\(bytes(bytesPerSecond))/с"
    }

    static func eta(_ seconds: Int) -> String {
        guard seconds > 0 else { return "—" }
        let minutes = seconds / 60
        let hours = minutes / 60
        let days = hours / 24
        if days > 0 { return "\(days) д \(hours % 24) ч" }
        if hours > 0 { return "\(hours) ч \(minutes % 60) мин" }
        return "\(max(1, minutes)) мин"
    }

    /// Часы-минуты-секунды для позиций просмотра: 47:13 или 1:02:45.
    static func clock(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        let secs = total % 60
        return hours > 0
            ? String(format: "%d:%02d:%02d", hours, minutes, secs)
            : String(format: "%d:%02d", minutes, secs)
    }

    /// Русская форма числительного: 1 загрузка, 2 загрузки, 5 загрузок.
    static func plural(_ count: Int, _ one: String, _ few: String, _ many: String) -> String {
        let mod100 = abs(count) % 100
        let mod10 = mod100 % 10
        if mod100 >= 11 && mod100 <= 14 { return many }
        if mod10 == 1 { return one }
        if mod10 >= 2 && mod10 <= 4 { return few }
        return many
    }

    /// «1 ч 50 мин» — длительность фильма из каталога.
    static func duration(minutes: Int) -> String {
        guard minutes > 0 else { return "" }
        let hours = minutes / 60
        let rest = minutes % 60
        if hours == 0 { return "\(rest) мин" }
        return rest == 0 ? "\(hours) ч" : "\(hours) ч \(rest) мин"
    }

    /// «только что», «5 мин», «вчера», «2 нед» — компактно и по-русски.
    /// Системный RelativeDateTimeFormatter выдавал «+0 с» и «-1 нед.».
    static func relative(_ date: Date) -> String {
        let seconds = Date().timeIntervalSince(date)
        guard seconds >= 0 else { return "только что" }
        let minutes = Int(seconds / 60)
        if minutes < 1 { return "только что" }
        if minutes < 60 { return "\(minutes) мин" }
        let hours = minutes / 60
        if hours < 24 { return "\(hours) \(plural(hours, "час", "часа", "часов"))" }
        let days = hours / 24
        if days == 1 { return "вчера" }
        if days < 7 { return "\(days) \(plural(days, "день", "дня", "дней"))" }
        let weeks = days / 7
        if weeks < 5 { return "\(weeks) \(plural(weeks, "неделя", "недели", "недель"))" }
        let months = days / 30
        if months < 12 { return "\(months) \(plural(months, "месяц", "месяца", "месяцев"))" }
        let years = days / 365
        return "\(years) \(plural(years, "год", "года", "лет"))"
    }

    /// Дата из ответов TorBox (ISO-8601 с дробными секундами и без).
    static func parseAPIDate(_ raw: String?) -> Date? {
        guard let raw, !raw.isEmpty else { return nil }
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = iso.date(from: raw) { return date }
        iso.formatOptions = [.withInternetDateTime]
        if let date = iso.date(from: raw) { return date }
        let fallback = DateFormatter()
        fallback.locale = Locale(identifier: "en_US_POSIX")
        fallback.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSSSS"
        return fallback.date(from: raw)
    }

    /// Остаток серии словами: «осталось 24 мин».
    static func remaining(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        guard total > 0 else { return "почти конец" }
        if total < 60 { return "осталось меньше минуты" }
        let minutes = total / 60
        if minutes < 60 { return "осталось \(minutes) \(plural(minutes, "минута", "минуты", "минут"))" }
        let hours = minutes / 60
        let rest = minutes % 60
        return rest == 0
            ? "осталось \(hours) \(plural(hours, "час", "часа", "часов"))"
            : "осталось \(hours) ч \(rest) мин"
    }

    /// TorBox отдаёт progress долей 0..1 (источник — qBittorrent), но на всякий случай принимаем и проценты.
    static func progressFraction(_ raw: Double?) -> Double {
        let value = max(0.0, raw ?? 0.0)
        return min(value <= 1 ? value : value / 100, 1.0)
    }
}
