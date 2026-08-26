import Foundation

/// Одна серия для плеера. Ссылку получаем лениво: экран открывается сразу,
/// а «получаю ссылку» показывает уже сам плеер, а не список файлов.
struct PlayerTrackItem: Identifiable {
    let id: String
    let title: String
    /// Название раздачи — вторая строка в шапке плеера.
    let subtitle: String?
    /// «S1 · E2», если удалось распознать.
    let episodeLabel: String?
    let isLocal: Bool
    let resolveURL: () async throws -> URL
    let startAt: () -> Double?
    let onProgress: (Double, Double) -> Void
    /// Побочный эффект старта: например, скачивание серии в память телефона.
    /// Получает уже готовую ссылку — ту же, что играет плеер.
    let onStart: ((URL) -> Void)?

    init(
        id: String,
        title: String,
        subtitle: String? = nil,
        episodeLabel: String? = nil,
        isLocal: Bool = false,
        resolveURL: @escaping () async throws -> URL,
        startAt: @escaping () -> Double? = { nil },
        onProgress: @escaping (Double, Double) -> Void = { _, _ in },
        onStart: ((URL) -> Void)? = nil
    ) {
        self.id = id
        self.title = title
        self.subtitle = subtitle
        self.episodeLabel = episodeLabel
        self.isLocal = isLocal
        self.resolveURL = resolveURL
        self.startAt = startAt
        self.onProgress = onProgress
        self.onStart = onStart
    }

    /// Готовый локальный файл — ссылка известна сразу.
    static func local(
        id: String,
        title: String,
        subtitle: String? = nil,
        episodeLabel: String? = nil,
        url: URL,
        startAt: @escaping () -> Double? = { nil },
        onProgress: @escaping (Double, Double) -> Void = { _, _ in }
    ) -> PlayerTrackItem {
        PlayerTrackItem(
            id: id,
            title: title,
            subtitle: subtitle,
            episodeLabel: episodeLabel,
            isLocal: true,
            resolveURL: { url },
            startAt: startAt,
            onProgress: onProgress
        )
    }
}

/// Очередь просмотра: одна серия или весь сезон подряд.
struct PlayerQueue: Identifiable {
    var items: [PlayerTrackItem]
    var index: Int

    var id: String { (items.first?.id ?? "empty") + "#\(items.count)" }

    init(items: [PlayerTrackItem], index: Int = 0) {
        self.items = items
        self.index = min(max(0, index), max(0, items.count - 1))
    }

    init(single item: PlayerTrackItem) {
        self.items = [item]
        self.index = 0
    }

    var current: PlayerTrackItem? {
        items.indices.contains(index) ? items[index] : nil
    }

    var hasNext: Bool { items.indices.contains(index + 1) }
    var hasPrevious: Bool { items.indices.contains(index - 1) }
    /// «Серия 3 из 10» — только когда в очереди действительно несколько файлов.
    var positionLabel: String? {
        items.count > 1 ? "\(index + 1) из \(items.count)" : nil
    }
}
