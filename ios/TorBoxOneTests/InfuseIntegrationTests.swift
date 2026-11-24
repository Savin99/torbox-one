import XCTest
@testable import TorBoxOne

@MainActor
final class InfuseIntegrationTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_780_000_000)
    private let signedURL = URL(string: "https://cdn.example/movie.mkv?token=a%2Bb&name=A%26B#part")!

    private func item(_ id: Int = 1) -> InfuseDownloadItem {
        .init(id: "hash:\(id)", torrentID: 7, fileID: id, torrentName: "Series",
              filename: "Series.S01E\(id).mkv", bytes: 1000, subtitleFileID: nil)
    }
    private func session() -> InfuseSession {
        .init(id: UUID(), account: "account", createdAt: now,
              tracks: [.init(item: item(), urlDigest: InfuseProtocol.digest(signedURL.absoluteString))])
    }
    private func callback(_ session: InfuseSession, path: String = "success", extra: [URLQueryItem] = []) -> URL {
        var components = URLComponents(string: "torboxone://infuse/\(path)")!
        components.queryItems = [.init(name: "session", value: session.id.uuidString)] + extra
        return components.url!
    }
    private func file(_ id: Int, _ name: String) -> TBFile {
        .init(id: id, name: name, shortName: nil, size: 1000, mimetype: nil)
    }

    func testDownloadEncodingPreservesSignedURLsFilenamesAndSubtitles() throws {
        let subtitle = URL(string: "https://cdn.example/sub.srt?x=1&signature=a+b%3D")!
        let id = UUID()
        let url = try InfuseProtocol.downloadURL(media: [
            .init(url: signedURL, filename: "Серия & + # 1.mkv", subtitle: subtitle),
            .init(url: URL(string: "https://cdn.example/2.mkv")!, filename: "S01E02.mkv"),
        ], sessionID: id)
        let query = try XCTUnwrap(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems)
        XCTAssertEqual(url.scheme, "infuse")
        XCTAssertEqual(url.path, "/save")
        XCTAssertEqual(query.map(\.name), ["url", "filename", "sub", "url", "filename", "download", "x-success", "x-error"])
        XCTAssertEqual(query[0].value, signedURL.absoluteString)
        XCTAssertEqual(query[1].value, "Серия & + # 1.mkv")
        XCTAssertEqual(query[2].value, subtitle.absoluteString)
        XCTAssertEqual(query[5].value, "1")
        XCTAssertFalse(query.contains { $0.name == "position" })
        XCTAssertEqual(query[6].value, "torboxone://infuse/success?session=\(id.uuidString)")
    }

    func testFullSeasonIsNotSilentlyTruncated() throws {
        let media = (1...24).map { InfuseProtocol.Media(url: URL(string: "https://cdn.example/\($0).mkv")!, filename: "Show.S01E\($0).mkv") }
        let url = try InfuseProtocol.downloadURL(media: media, sessionID: UUID())
        let query = try XCTUnwrap(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems)
        XCTAssertEqual(query.filter { $0.name == "url" }.count, 24)
        XCTAssertEqual(url.path, "/save")
    }

    func testRejectsPrivateFilesCredentialsAndEmptySelections() {
        for raw in ["file:///private/movie.mkv", "https://user:password@example.com/video", "infuse://play", "https:/no-host"] {
            XCTAssertThrowsError(try InfuseProtocol.downloadURL(media: [.init(url: URL(string: raw)!, filename: "movie")], sessionID: UUID()))
        }
        XCTAssertThrowsError(try InfuseProtocol.downloadURL(media: [], sessionID: UUID()))
    }

    func testOversizedSelectionFailsInsteadOfDroppingEpisodes() {
        let media = InfuseProtocol.Media(url: signedURL, filename: String(repeating: "a", count: 60_000))
        XCTAssertThrowsError(try InfuseProtocol.downloadURL(media: [media], sessionID: UUID()))
    }

    func testDownloadAcknowledgementHasNoPlaybackOrCompletionStatus() {
        let session = session()
        XCTAssertEqual(InfuseProtocol.callback(callback(session), session: session, account: "account", now: now), .accepted)
    }

    func testRetryContainsOnlyFailedFilesAndDeduplicatesThem() {
        var session = session()
        session.tracks.insert(.init(item: item(2), urlDigest: "unrelated"), at: 0)
        let failed = URLQueryItem(name: "failedUrl", value: signedURL.absoluteString)
        XCTAssertEqual(InfuseProtocol.callback(callback(session, path: "error", extra: [failed, failed]), session: session, account: "account", now: now), .failed(indices: [1]))
        XCTAssertNil(InfuseProtocol.callback(callback(session, path: "error", extra: [.init(name: "failedUrl", value: "https://other.example/video")]), session: session, account: "account", now: now))
    }

    func testFailureWithoutFileListOffersTheWholeSelection() {
        let session = session()
        XCTAssertEqual(InfuseProtocol.callback(callback(session, path: "error"), session: session, account: "account", now: now), .failed(indices: [0]))
    }

    func testRejectsDuplicateSessionIDsAndAlreadyAcceptedCallbacks() {
        var session = session()
        XCTAssertNil(InfuseProtocol.callback(callback(session, extra: [.init(name: "session", value: session.id.uuidString)]), session: session, account: "account", now: now))
        session.acceptedAt = now
        XCTAssertNil(InfuseProtocol.callback(callback(session), session: session, account: "account", now: now))
    }

    func testRejectsForeignAccountExpiredSessionAndWrongRoutes() {
        let session = session()
        let url = callback(session)
        XCTAssertNil(InfuseProtocol.callback(url, session: session, account: "other-account", now: now))
        XCTAssertNil(InfuseProtocol.callback(url, session: session, account: "account", now: now.addingTimeInterval(604_801)))
        XCTAssertNil(InfuseProtocol.callback(url, session: session, account: "account", now: now.addingTimeInterval(-1)))
        XCTAssertNil(InfuseProtocol.callback(callback(session, path: "other"), session: session, account: "account", now: now))
        let foreign = URL(string: url.absoluteString.replacingOccurrences(of: "torboxone://infuse", with: "torboxone://other"))!
        XCTAssertNil(InfuseProtocol.callback(foreign, session: session, account: "account", now: now))
        XCTAssertNil(InfuseProtocol.callback(callback(self.session()), session: session, account: "account", now: now))
    }

    func testSessionSurvivesRelaunchWithoutPersistingSignedURLs() throws {
        let directory = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appending(path: "session.json")
        var session = session()
        try InfuseSessionStore(fileURL: url).save(session)
        let reopened = InfuseSessionStore(fileURL: url)
        XCTAssertEqual(reopened.session?.id, session.id)
        let text = try String(contentsOf: url, encoding: .utf8)
        XCTAssertFalse(text.contains("cdn.example"))
        XCTAssertFalse(text.contains("token="))
        session.acceptedAt = now
        try reopened.save(session)
        let accepted = try XCTUnwrap(InfuseSessionStore(fileURL: url).session)
        XCTAssertNil(InfuseProtocol.callback(callback(accepted), session: accepted, account: "account", now: now))
        reopened.clear()
        XCTAssertNil(InfuseSessionStore(fileURL: url).session)
    }

    func testSeasonSelectionNeverIncludesAnotherSeasonOrExtras() {
        let files = [file(3, "Show.S02E01.mkv"), file(2, "Show.S01E10.mkv"),
                     file(1, "Show.S01E02.mkv"), file(4, "trailer.mkv"), file(5, "Show.S01E01.srt")]
        XCTAssertEqual(DownloadSelection.seasonFiles(files, season: nil).map(\.id), [1, 2])
        XCTAssertEqual(DownloadSelection.seasonFiles(files, season: 2).map(\.id), [3])
        XCTAssertTrue(DownloadSelection.seasonFiles(files, season: 9).isEmpty)
    }

    func testUnrecognizedSeriesRequireManualSelectionButSingleMovieDoesNot() {
        let files = [file(1, "part1.mkv"), file(2, "part2.mkv")]
        XCTAssertTrue(DownloadSelection.seasonFiles(files, season: nil).isEmpty)
        XCTAssertEqual(DownloadSelection.seasonFiles([files[0]], season: nil).map(\.id), [1])
    }

    func testSelectedEpisodesAreSortedDeduplicatedAndExcludeNonVideo() throws {
        let torrent = try JSONDecoder().decode(TBTorrent.self, from: Data(#"{"id":7,"hash":"ABC","name":"Show"}"#.utf8))
        let files = [file(2, "Show.S01E10.mkv"), file(1, "Show.S01E02.mkv"), file(1, "Show.S01E02.mkv"), file(3, "readme.txt")]
        let selected = DownloadSelection.items(torrent: torrent, files: files)
        XCTAssertEqual(selected.map(\.fileID), [1, 2])
        XCTAssertEqual(selected.first?.id, "abc:1")
    }

    func testSubtitleMatchingDoesNotAttachAnotherEpisode() {
        let video = file(1, "Show.S01E01.mkv")
        let files = [file(2, "Show.S01E02.ru.srt"), file(3, "Show.S01E01.eng.srt"), file(4, "Show.S01E01.ru.srt")]
        XCTAssertEqual(DownloadSelection.matchingSubtitle(for: video, in: files)?.id, 4)
        XCTAssertNil(DownloadSelection.matchingSubtitle(for: video, in: [files[0]]))
    }
}
