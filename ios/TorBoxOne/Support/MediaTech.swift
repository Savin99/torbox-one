import Foundation

/// Технические свойства релиза, вытащенные из имени файла: разрешение, кодек,
/// HDR и звук. Точных данных о потоке нет, зато релизы это честно пишут в имени.
nonisolated struct MediaTech: Equatable {
    var resolution: String?
    var codec: String?
    var dynamicRange: String?
    var audio: String?
    var source: String?

    /// Всё, что нашлось — для строки чипов.
    var chips: [String] {
        [resolution, dynamicRange, codec, audio, source].compactMap { $0 }
    }

    var isEmpty: Bool { chips.isEmpty }

    static func parse(_ name: String) -> MediaTech {
        let text = name.lowercased()
        func has(_ needles: [String]) -> Bool {
            needles.contains { text.contains($0) }
        }

        var tech = MediaTech()

        if has(["2160p", "4k", "uhd"]) {
            tech.resolution = "4K"
        } else if has(["1080p", "1080i"]) {
            tech.resolution = "1080p"
        } else if has(["720p"]) {
            tech.resolution = "720p"
        } else if has(["480p", "576p"]) {
            tech.resolution = "SD"
        }

        if has(["dolby vision", "dovi", "-dv", ".dv.", " dv "]) {
            tech.dynamicRange = "Dolby Vision"
        } else if has(["hdr10+", "hdr10plus"]) {
            tech.dynamicRange = "HDR10+"
        } else if has(["hdr"]) {
            tech.dynamicRange = "HDR"
        }

        if has(["av1"]) {
            tech.codec = "AV1"
        } else if has(["x265", "h265", "h.265", "hevc"]) {
            tech.codec = "HEVC"
        } else if has(["x264", "h264", "h.264", "avc"]) {
            tech.codec = "H.264"
        }

        if has(["atmos"]) {
            tech.audio = "Dolby Atmos"
        } else if has(["truehd"]) {
            tech.audio = "TrueHD"
        } else if has(["dts-hd", "dtshd", "dts-ma"]) {
            tech.audio = "DTS-HD"
        } else if has(["dts"]) {
            tech.audio = "DTS"
        } else if has(["eac3", "ddp", "dd+"]) {
            tech.audio = "E-AC3"
        } else if has(["ac3", "dd5", "dd 5"]) {
            tech.audio = "AC3"
        } else if has(["flac"]) {
            tech.audio = "FLAC"
        } else if has(["aac"]) {
            tech.audio = "AAC"
        }

        if has(["remux"]) {
            tech.source = "Remux"
        } else if has(["bdrip", "brrip"]) {
            tech.source = "BDRip"
        } else if has(["bluray", "blu-ray"]) {
            tech.source = "BluRay"
        } else if has(["web-dl", "webdl", "web dl"]) {
            tech.source = "WEB-DL"
        } else if has(["webrip", "web-rip"]) {
            tech.source = "WEBRip"
        } else if has(["hdtv"]) {
            tech.source = "HDTV"
        } else if has(["dvdrip"]) {
            tech.source = "DVDRip"
        }

        return tech
    }
}
