import Foundation

/// Разбор сезона и серии из имени файла. Нужен, чтобы список серий шёл
/// по-человечески (1, 2, …, 10, а не 1, 10, 2) и группировался по сезонам.
struct EpisodeInfo: Equatable {
    let season: Int?
    let episode: Int?

    var isEpisode: Bool { episode != nil }

    /// Короткая подпись: «S1 · E2» либо «Серия 2».
    var label: String? {
        guard let episode else { return nil }
        if let season {
            return "S\(season) · E\(episode)"
        }
        return "Серия \(episode)"
    }

    static let none = EpisodeInfo(season: nil, episode: nil)

    private static let patterns: [(String, Bool)] = [
        // (регулярка, есть ли группа сезона)
        (#"[sS](\d{1,2})[\s._-]?[eEхx](\d{1,3})"#, true),
        (#"(\d{1,2})[xх](\d{2,3})"#, true),
        (#"[Сс]езон[\s._-]*(\d{1,2}).{0,12}?[Сс]ери[яию][\s._-]*(\d{1,3})"#, true),
        (#"[Сс]ери[яию][\s._-]*(\d{1,3})"#, false),
        (#"(?:^|[\s._\-\[(])[eEеЕ](\d{1,3})(?:[\s._\-\])]|$)"#, false),
    ]

    static func parse(_ name: String) -> EpisodeInfo {
        let base = (name as NSString).deletingPathExtension
        for (pattern, hasSeason) in patterns {
            guard let regex = try? NSRegularExpression(pattern: pattern),
                  let match = regex.firstMatch(
                      in: base, range: NSRange(base.startIndex..., in: base)
                  )
            else { continue }
            let nsBase = base as NSString
            func group(_ index: Int) -> Int? {
                let range = match.range(at: index)
                guard range.location != NSNotFound else { return nil }
                return Int(nsBase.substring(with: range))
            }
            if hasSeason {
                if let season = group(1), let episode = group(2) {
                    return EpisodeInfo(season: season, episode: episode)
                }
            } else if let episode = group(1) {
                return EpisodeInfo(season: nil, episode: episode)
            }
        }
        return .none
    }
}

extension TBFile {
    var episode: EpisodeInfo { EpisodeInfo.parse(displayName) }

    /// Порядок как в проводнике: по сезону, серии, затем numeric-aware по имени.
    static func orderedBefore(_ lhs: TBFile, _ rhs: TBFile) -> Bool {
        let left = lhs.episode
        let right = rhs.episode
        if let ls = left.season, let rs = right.season, ls != rs { return ls < rs }
        if let le = left.episode, let re = right.episode, le != re { return le < re }
        return lhs.displayName.localizedStandardCompare(rhs.displayName) == .orderedAscending
    }
}
