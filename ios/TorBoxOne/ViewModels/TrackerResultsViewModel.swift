import Foundation
import Observation

/// Результаты поиска по трекерам для одного запроса: живут вместе с экраном,
/// поэтому уход назад отменяет долгий (до 90 с) запрос сам собой.
@Observable
final class TrackerResultsViewModel {
    enum Phase: Equatable {
        case loading
        case ready
        case cancelled
        case failed(String)
    }

    enum AddState: Equatable {
        case idle
        case adding
        case added
        case failed(String)
    }

    enum Sort: String, CaseIterable, Identifiable {
        case seeders
        case size
        case quality
        case name

        var id: String { rawValue }

        var title: String {
            switch self {
            case .seeders: "По раздающим"
            case .size: "По размеру"
            case .quality: "По качеству"
            case .name: "По названию"
            }
        }

        var icon: String {
            switch self {
            case .seeders: "arrow.up.arrow.down"
            case .size: "externaldrive"
            case .quality: "4k.tv"
            case .name: "textformat.abc"
            }
        }
    }

    private(set) var phase: Phase = .loading
    private(set) var all: [ReleaseResult] = []
    private(set) var providerStatus: [String: String] = [:]
    private(set) var cachedHashes: Set<String> = []
    private(set) var detailsCache: [String: ReleaseDetails] = [:]
    var addStates: [String: AddState] = [:]

    var sort: Sort = .seeders
    var onlyCached = false
    var onlyHD = false
    var onlyVoiceover = false

    /// Сколько фильтров включено — для точки на кнопке фильтра.
    var activeFilterCount: Int {
        [onlyCached, onlyHD, onlyVoiceover].filter { $0 }.count
    }

    var visible: [ReleaseResult] {
        var list = all
        if onlyCached { list = list.filter { isCached($0) } }
        if onlyHD { list = list.filter { $0.qualityRank >= 3 } }
        if onlyVoiceover { list = list.filter(\.hasVoiceover) }
        switch sort {
        case .seeders:
            list.sort { $0.seeders > $1.seeders }
        case .size:
            list.sort { $0.sizeBytes > $1.sizeBytes }
        case .quality:
            list.sort {
                $0.qualityRank != $1.qualityRank
                    ? $0.qualityRank > $1.qualityRank
                    : $0.seeders > $1.seeders
            }
        case .name:
            list.sort { $0.title.localizedStandardCompare($1.title) == .orderedAscending }
        }
        // В кэше TorBox — всегда наверх: такие раздачи стартуют мгновенно
        return list.sorted { isCached($0) && !isCached($1) }
    }

    var hiddenByFilters: Int { max(0, all.count - visible.count) }

    /// Все источники ответили ошибкой — это не «ничего не нашлось», а поломка.
    var allProvidersFailed: Bool {
        !providerStatus.isEmpty && providerStatus.values.allSatisfy { $0.lowercased().contains("ошиб") }
    }

    func isCached(_ release: ReleaseResult) -> Bool {
        !release.infoHash.isEmpty && cachedHashes.contains(release.infoHash.lowercased())
    }

    // MARK: - Загрузка

    func load(client: SearchAPIClient, torBox: TorBoxClient?, query: String) async {
        phase = .loading
        do {
            let response = try await client.trackerSearch(query)
            all = response.results
            providerStatus = response.status
            phase = .ready
            await refreshCachedBadges(torBox: torBox)
        } catch is CancellationError {
            phase = .cancelled
        } catch let error as URLError where error.code == .cancelled {
            phase = .cancelled
        } catch {
            phase = .failed(error.localizedDescription)
        }
    }

    /// «В кэше» спрашиваем у TorBox напрямую: ключ мини-API не передаётся.
    private func refreshCachedBadges(torBox: TorBoxClient?) async {
        guard let torBox else { return }
        let hashes = all.map(\.infoHash).filter { !$0.isEmpty }
        guard !hashes.isEmpty else { return }
        cachedHashes = (try? await torBox.checkCached(hashes: hashes)) ?? []
    }

    func loadDetails(client: SearchAPIClient, release: ReleaseResult) async {
        guard detailsCache[release.id] == nil else { return }
        detailsCache[release.id] = try? await client.releaseDetails(id: release.id)
    }

    // MARK: - Добавление в TorBox

    @discardableResult
    func add(release: ReleaseResult, client: SearchAPIClient, torBox: TorBoxClient) async -> Bool {
        addStates[release.id] = .adding
        do {
            if let magnet = release.magnet {
                try await torBox.addMagnet(magnet, name: release.title)
            } else {
                let torrentData = try await client.torrentBytes(id: release.id)
                try await torBox.addTorrentFile(torrentData, name: release.title)
            }
            addStates[release.id] = .added
            return true
        } catch {
            addStates[release.id] = .failed(error.localizedDescription)
            return false
        }
    }

    func addState(_ release: ReleaseResult) -> AddState {
        addStates[release.id] ?? .idle
    }
}

#if DEBUG
extension TrackerResultsViewModel {
    /// Демо-данные для проверки вида экрана без живых трекеров.
    static var demo: TrackerResultsViewModel {
        let vm = TrackerResultsViewModel()
        vm.loadDemo()
        return vm
    }

    private func loadDemo() {
        all = [
            ReleaseResult(
                id: "1",
                title: "Интерстеллар / Interstellar (2014) UHD BDRemux 2160p HDR / Дубляж",
                sizeText: "54.2 ГБ", sizeBytes: 58_209_986_150, seeders: 42, leechers: 6,
                sourceName: "NNM-Club", category: "Зарубежное кино",
                magnetUrl: "magnet:?xt=urn:btih:demo1", infoHash: "demo1",
                qualitySummary: "2160p HDR · BDRemux", voiceoverSummary: "Дубляж, LostFilm",
                sourceUrl: "https://nnmclub.to/forum/viewtopic.php?t=1"
            ),
            ReleaseResult(
                id: "2",
                title: "Интерстеллар / Interstellar (2014) BDRip 1080p от HDCLUB",
                sizeText: "12.1 ГБ", sizeBytes: 12_992_276_070, seeders: 128, leechers: 14,
                sourceName: "Prowlarr · RuTracker", category: "Кино",
                magnetUrl: "magnet:?xt=urn:btih:demo2", infoHash: "demo2",
                qualitySummary: "1080p BDRip", voiceoverSummary: "Дубляж",
                sourceUrl: "https://example.org/2"
            ),
            ReleaseResult(
                id: "3",
                title: "Interstellar (2014) 720p WEB-DL x264 AAC",
                sizeText: "3.4 ГБ", sizeBytes: 3_650_722_201, seeders: 3, leechers: 0,
                sourceName: "Prowlarr · 1337x", category: "Movies",
                magnetUrl: "magnet:?xt=urn:btih:demo3", infoHash: "demo3",
                qualitySummary: "720p WEB-DL", voiceoverSummary: "",
                sourceUrl: "https://example.org/3"
            ),
        ]
        providerStatus = ["NNM-Club": "2", "Prowlarr": "1"]
        cachedHashes = ["demo2"]
        phase = .ready
    }
}
#endif
