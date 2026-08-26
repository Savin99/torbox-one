import Foundation

/// Человеческое имя серии из имени файла: «Game.of.Thrones.S01E06.A.Golden.Crown.mkv»
/// превращается в «A Golden Crown». Технические токены отбрасываются.
enum MediaName {
    /// Мусор релизов: качество, кодеки, звук, группы.
    private static let noise: Set<String> = [
        "1080p", "1080i", "720p", "480p", "2160p", "4k", "uhd", "hd", "sd",
        "webrip", "web", "webdl", "web-dl", "hdrip", "bdrip", "bluray", "blu-ray", "brrip",
        "dvdrip", "dvd", "hdtv", "remux", "proper", "repack", "extended", "unrated",
        "x264", "x265", "h264", "h265", "avc", "hevc", "xvid", "divx", "10bit", "8bit",
        "aac", "ac3", "eac3", "dts", "dtshd", "truehd", "flac", "mp3", "dd", "dd5", "ddp",
        "hdr", "hdr10", "dv", "sdr", "atmos", "5", "1", "2", "0",
        "rus", "eng", "ukr", "multi", "dual", "sub", "subs", "subbed", "dub", "dubbed",
        "amzn", "nf", "hulu", "dsnp", "hmax", "atvp", "ivi", "lostfilm", "kravec", "alexfilm",
        "seasons", "season", "complete",
    ]

    private static let episodePattern = #"[sS]\d{1,2}[\s._-]?[eEхx]\d{1,3}"#

    static func pretty(_ fileName: String, seriesName: String? = nil) -> String {
        let base = (fileName as NSString).deletingPathExtension
        var working = base

        // Название сериала в начале имени файла — уже видно в шапке экрана
        if let seriesName, !seriesName.isEmpty {
            let normalizedSeries = normalize(seriesName)
            let normalizedWorking = normalize(working)
            if !normalizedSeries.isEmpty, normalizedWorking.hasPrefix(normalizedSeries) {
                working = String(normalizedWorking.dropFirst(normalizedSeries.count))
            }
        }

        // После SxxExx обычно идёт настоящее название серии
        if let regex = try? NSRegularExpression(pattern: episodePattern),
           let match = regex.firstMatch(in: working, range: NSRange(working.startIndex..., in: working)),
           let range = Range(match.range, in: working) {
            let tail = String(working[range.upperBound...])
            if cleaned(tail).count >= 2 {
                working = tail
            } else {
                working = String(working[..<range.lowerBound])
            }
        }

        let result = cleaned(working)
        return result.count >= 2 ? result : (fileName as NSString).deletingPathExtension
    }

    /// Точки и подчёркивания в пробелы, регистр не трогаем.
    private static func normalize(_ text: String) -> String {
        text.replacingOccurrences(of: "[._]+", with: " ", options: .regularExpression)
            .replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
    }

    private static func cleaned(_ text: String) -> String {
        let words = normalize(text)
            .components(separatedBy: CharacterSet(charactersIn: " -–—[](){}"))
            .filter { !$0.isEmpty }
        var kept: [String] = []
        for word in words {
            let lower = word.lowercased()
            // Технический хвост отрезаем целиком: после него название не продолжается
            if noise.contains(lower) { break }
            if lower.range(of: #"^\d{3,4}[pi]$"#, options: .regularExpression) != nil { break }
            kept.append(word)
        }
        return kept.joined(separator: " ").trimmingCharacters(in: .whitespaces)
    }
}

extension TBFile {
    /// Читаемое имя серии для интерфейса (в путях загрузок по-прежнему исходное).
    func prettyName(seriesName: String? = nil) -> String {
        MediaName.pretty(displayName, seriesName: seriesName)
    }
}
