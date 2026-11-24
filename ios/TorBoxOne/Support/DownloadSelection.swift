import Foundation

nonisolated struct InfuseDownloadItem: Codable, Identifiable, Sendable {
    var id: String
    var torrentID: Int
    var fileID: Int
    var torrentName: String
    var filename: String
    var bytes: Int64?
    var subtitleFileID: Int?
}

enum DownloadSelection {
    static func videoFiles(of torrent: TBTorrent) -> [TBFile] {
        (torrent.files ?? []).filter(\.isVideo).sorted(by: TBFile.orderedBefore)
    }

    /// A bulk download is one explicit season, never an entire multi-season torrent.
    static func seasonFiles(_ files: [TBFile], season: Int?) -> [TBFile] {
        let videos = files.filter(\.isVideo).sorted(by: TBFile.orderedBefore)
        let seasons = Set(videos.compactMap { $0.episode.season }).sorted()
        if let selected = season ?? seasons.first {
            return videos.filter { $0.episode.season == selected }
        }
        return videos.count == 1 ? videos : [] // Unrecognized series require an explicit file selection.
    }

    static func items(torrent: TBTorrent, files: [TBFile]) -> [InfuseDownloadItem] {
        var seen = Set<Int>()
        return files.filter { $0.isVideo && seen.insert($0.id).inserted }.sorted(by: TBFile.orderedBefore).map { file in
            InfuseDownloadItem(id: PlaybackStore.key(torrent: torrent, fileID: file.id),
                               torrentID: torrent.id, fileID: file.id, torrentName: torrent.name ?? "Без названия",
                               filename: file.displayName, bytes: file.size,
                               subtitleFileID: matchingSubtitle(for: file, in: torrent.files ?? [])?.id)
        }
    }

    static func matchingSubtitle(for video: TBFile, in files: [TBFile]) -> TBFile? {
        let stem = (video.displayName as NSString).deletingPathExtension.lowercased()
        return files.filter { file in
            let name = file.displayName.lowercased()
            let ext = (name as NSString).pathExtension
            let base = (name as NSString).deletingPathExtension
            return ["srt", "ass", "ssa", "vtt"].contains(ext)
                && (base == stem || ["ru", "rus", "en", "eng"].contains(where: { base == stem + "." + $0 }))
        }.sorted {
            func rank(_ file: TBFile) -> Int {
                let base = (file.displayName as NSString).deletingPathExtension.lowercased()
                return base.hasSuffix(".ru") || base.hasSuffix(".rus") ? 0 : (base == stem ? 1 : 2)
            }
            return rank($0) == rank($1) ? $0.id < $1.id : rank($0) < rank($1)
        }.first
    }
}
