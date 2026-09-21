import Foundation
import Observation
import UIKit

/// Сохраняет доступ к кадрам старого плеера. Новые обложки приходят из каталога.
@Observable
final class ThumbnailStore {
    /// Версия меняется при сохранении нового кадра, чтобы вид обновился.
    private(set) var revision = 0

    private static let directory: URL = {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let dir = base.appending(path: "thumbs", directoryHint: .isDirectory)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }()

    /// Путь, куда плееру писать снимок. Ключ — тот же, что у позиции просмотра.
    static func path(for key: String) -> URL {
        Self.directory.appending(path: safeName(key) + ".png")
    }

    func image(for key: String) -> UIImage? {
        let url = Self.path(for: key)
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        return UIImage(contentsOfFile: url.path)
    }

    func hasImage(for key: String) -> Bool {
        FileManager.default.fileExists(atPath: Self.path(for: key).path)
    }

    /// Сообщить, что появился новый кадр (вызывает плеер).
    func didSave(key: String) {
        revision += 1
    }

    func remove(key: String) {
        try? FileManager.default.removeItem(at: Self.path(for: key))
        revision += 1
    }

    /// Сколько занимают кадры — для экрана памяти.
    var totalBytes: Int64 {
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: Self.directory, includingPropertiesForKeys: [.fileSizeKey]
        ) else { return 0 }
        return files.reduce(0) { sum, url in
            sum + Int64((try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0)
        }
    }

    func clear() {
        try? FileManager.default.removeItem(at: Self.directory)
        try? FileManager.default.createDirectory(at: Self.directory, withIntermediateDirectories: true)
        revision += 1
    }

    private static func safeName(_ key: String) -> String {
        key.replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: ":", with: "-")
    }
}
