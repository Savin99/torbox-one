import AVFoundation
import Foundation
import MediaPlayer
import Observation
import UIKit
import VLCKit

@Observable
final class PlayerViewModel: NSObject {
    enum Phase: Equatable {
        /// Получаем ссылку на файл (для локальных — мгновенно).
        case resolving
        case opening
        case buffering(Float)
        case playing
        case paused
        /// Серия закончилась: показываем итоговый экран или автопереход.
        case ended
        case failed(String)
    }

    struct TrackChoice: Identifiable, Equatable {
        let id: String
        let name: String
        let language: String?
    }

    // MARK: - Наблюдаемое состояние

    private(set) var phase: Phase = .resolving
    private(set) var positionSeconds: Double = 0
    private(set) var durationSeconds: Double = 0
    private(set) var audioChoices: [TrackChoice] = []
    private(set) var textChoices: [TrackChoice] = []
    private(set) var selectedAudioID: String?
    private(set) var selectedTextID: String?
    private(set) var isFillMode = false
    private(set) var queue: PlayerQueue
    /// Секунды до автоперехода на следующую серию (nil — автоперехода нет).
    private(set) var autoNextCountdown: Int?
    /// Позиция, с которой продолжили просмотр — для плашки «Продолжаю с …».
    private(set) var resumedFrom: Double?
    /// Экран заблокирован от случайных касаний.
    var isLocked = false

    var title: String { queue.current?.title ?? "" }
    var subtitle: String? { queue.current?.subtitle }
    var episodeLabel: String? { queue.current?.episodeLabel }
    var hasNext: Bool { queue.hasNext }
    var hasPrevious: Bool { queue.hasPrevious }
    var positionLabel: String? { queue.positionLabel }
    /// Название следующей серии — для экрана «серия закончилась».
    var nextTitle: String? {
        guard queue.items.indices.contains(queue.index + 1) else { return nil }
        let next = queue.items[queue.index + 1]
        return [next.episodeLabel, next.title].compactMap { $0 }.joined(separator: " · ")
    }

    var rate: Float = 1.0 {
        didSet {
            player.rate = rate
            updateNowPlaying()
        }
    }

    /// Ссылка на текущий файл — из неё берём кадры для перелистывания.
    private(set) var mediaURL: URL?

    /// Звук выключен: кнопка в шапке, как в референсе.
    var isMuted = false {
        didSet { player.audio?.isMuted = isMuted }
    }

    /// Громкость плеера 0…1 (VLC умеет свою, независимо от системной).
    var volume: Double = 1.0 {
        didSet {
            player.audio?.volume = Int32((volume * 100).rounded())
        }
    }

    /// Яркость экрана 0…1 — привычный жест по левой половине.
    var brightness: Double {
        get { Double(screen?.brightness ?? 0.5) }
        set { screen?.brightness = CGFloat(min(max(newValue, 0.05), 1)) }
    }

    // MARK: - Внутреннее

    private let player = VLCMediaPlayer()
    private var pendingSeekMs: Int64?
    private var lastEmit = Date.distantPast
    private var didAutoRetry = false
    private var isScrubbing = false
    /// Куда мы только что прыгнули: VLC после перемотки на секунду отдаёт 0.
    private var seekTarget: Double?
    /// Троттлинг живой перемотки во время скраба локального файла.
    private var lastLiveSeek = Date.distantPast
    private var seekGuardUntil = Date.distantPast
    private var didShutdown = false
    private var isSwitchingMedia = false
    /// Пользователь выбрал дорожку руками — больше не переключаем автоматически.
    private var didUserPickTracks = false
    private var interruptionObserver: (any NSObjectProtocol)?
    private var countdownTask: Task<Void, Never>?
    private var resolveTask: Task<Void, Never>?
    /// Системная яркость до входа в плеер — вернём её при выходе.
    private var initialBrightness: CGFloat?
    /// Куда складывать кадры для карточек «Продолжить просмотр».
    private let thumbnails: ThumbnailStore?
    private var lastSnapshot = Date.distantPast
    private var currentURL: URL?
    private var drawable: UIView?

    private var screen: UIScreen? {
        UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .first { $0.activationState == .foregroundActive }?
            .screen
    }

    init(queue: PlayerQueue, thumbnails: ThumbnailStore? = nil) {
        self.queue = queue
        self.thumbnails = thumbnails
        super.init()
    }

    // MARK: - Жизненный цикл

    func start(drawable: UIView) {
        self.drawable = drawable
        player.delegate = self
        player.drawable = drawable
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback)
        try? AVAudioSession.sharedInstance().setActive(true)
        UIApplication.shared.isIdleTimerDisabled = true
        interruptionObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.pauseForBackground()
            }
        }
        volume = 1.0
        initialBrightness = screen?.brightness
        setupRemoteCommands()
        resolveAndPlayCurrent()
    }

    /// Идемпотентный teardown из onDisappear. Ничего в deinit: асинхронный stop VLC 4 оттуда зависает.
    func shutdown() {
        guard !didShutdown else { return }
        didShutdown = true
        countdownTask?.cancel()
        resolveTask?.cancel()
        emitProgress(force: true)
        player.delegate = nil
        player.stop()
        player.drawable = nil
        UIApplication.shared.isIdleTimerDisabled = false
        if let initialBrightness {
            screen?.brightness = initialBrightness
        }
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        if let observer = interruptionObserver {
            NotificationCenter.default.removeObserver(observer)
        }
        teardownRemoteCommands()
    }

    func pauseForBackground() {
        if player.isPlaying {
            player.pause()
            emitProgress(force: true)
        }
        // Приложение в фоне не играет, поэтому убираем и панель «сейчас играет»:
        // иначе на локскрине висит мёртвая карточка, где кнопка play ничего не делает
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
    }

    /// Возврат из фона: возвращаем панель управления, если серия ещё открыта.
    func restoreAfterForeground() {
        guard !didShutdown else { return }
        updateNowPlaying()
    }

    // MARK: - Очередь

    private func resolveAndPlayCurrent() {
        guard let item = queue.current else {
            phase = .failed("Нечего воспроизводить")
            return
        }
        countdownTask?.cancel()
        autoNextCountdown = nil
        resolveTask?.cancel()
        positionSeconds = 0
        durationSeconds = 0
        audioChoices = []
        textChoices = []
        didAutoRetry = false
        didUserPickTracks = false
        phase = .resolving
        let start = item.startAt()
        pendingSeekMs = (start ?? 0) > 1 ? Int64((start ?? 0) * 1000) : nil
        resumedFrom = (start ?? 0) > 1 ? start : nil
        resolveTask = Task { [weak self] in
            guard let self else { return }
            do {
                let url = try await item.resolveURL()
                guard !Task.isCancelled, !self.didShutdown else { return }
                item.onStart?(url)
                self.load(url: url)
            } catch {
                guard !Task.isCancelled else { return }
                self.phase = .failed("Не получилось получить ссылку: \(error.localizedDescription)")
            }
        }
    }

    func playNext() {
        guard queue.hasNext else { return }
        emitProgress(force: true)
        queue.index += 1
        Haptics.tap()
        resolveAndPlayCurrent()
    }

    /// Перейти к конкретной серии из листа очереди.
    func play(at index: Int) {
        guard queue.items.indices.contains(index), index != queue.index else { return }
        emitProgress(force: true)
        queue.index = index
        resolveAndPlayCurrent()
    }

    func playPrevious() {
        guard queue.hasPrevious else { return }
        emitProgress(force: true)
        queue.index -= 1
        Haptics.tap()
        resolveAndPlayCurrent()
    }

    /// Отменить автопереход и остаться на титрах.
    func cancelAutoNext() {
        countdownTask?.cancel()
        autoNextCountdown = nil
    }

    func restartCurrent() {
        pendingSeekMs = nil
        resumedFrom = nil
        cancelAutoNext()
        if let url = currentURL {
            load(url: url)
        } else {
            resolveAndPlayCurrent()
        }
    }

    /// Начать серию с начала, отменив автопродолжение.
    func startOver() {
        pendingSeekMs = nil
        resumedFrom = nil
        player.position = 0
        positionSeconds = 0
        if !player.isPlaying { player.play() }
    }

    private func load(url: URL) {
        guard let media = VLCMedia(url: url) else {
            phase = .failed("Некорректная ссылка")
            return
        }
        currentURL = url
        mediaURL = url
        if !url.isFileURL {
            media.addOption(":network-caching=3000")
        }
        isSwitchingMedia = true
        player.media = media
        player.play()
        player.rate = rate
        isSwitchingMedia = false
        phase = .opening
        updateNowPlaying()
    }

    // MARK: - Управление

    func togglePlay() {
        if player.isPlaying {
            player.pause()
        } else {
            if phase == .ended {
                restartCurrent()
                return
            }
            player.play()
        }
        Haptics.tap()
    }

    func jump(_ seconds: Double) {
        cancelAutoNext()
        if seconds >= 0 {
            player.jumpForward(seconds)
        } else {
            player.jumpBackward(-seconds)
        }
        // Мгновенно двигаем подпись времени: событие от VLC придёт позже
        positionSeconds = max(0, min(positionSeconds + seconds, durationSeconds))
        updateNowPlaying()
    }

    func beginScrub() {
        isScrubbing = true
        cancelAutoNext()
    }

    func scrubPreview(fraction: Double) {
        positionSeconds = fraction * durationSeconds
        // У локального файла перемотка бесплатная: показываем кадры сразу,
        // как в Infuse. По сети так делать нельзя — каждый прыжок это ребуфер.
        guard queue.current?.isLocal == true, player.isSeekable else { return }
        let now = Date()
        guard now.timeIntervalSince(lastLiveSeek) > 0.15 else { return }
        lastLiveSeek = now
        player.position = min(max(fraction, 0), 1)
    }

    func endScrub(fraction: Double) {
        isScrubbing = false
        guard player.isSeekable else { return }
        player.position = min(max(fraction, 0), 1)
        positionSeconds = fraction * durationSeconds
        // Пока поток переоткрывается, показываем то место, куда прыгнули
        seekTarget = positionSeconds
        seekGuardUntil = Date().addingTimeInterval(6)
        if phase == .ended, fraction < 0.98 {
            phase = .playing
            player.play()
        }
        updateNowPlaying()
    }

    func seek(to seconds: Double) {
        guard durationSeconds > 0 else { return }
        endScrub(fraction: seconds / durationSeconds)
    }

    func select(audioID: String) {
        for track in player.audioTracks where track.trackId == audioID {
            track.isSelectedExclusively = true
        }
        selectedAudioID = audioID
        didUserPickTracks = true
        if let item = queue.current,
           let name = audioChoices.first(where: { $0.id == audioID })?.name {
            TrackPreferences.setAudio(name, for: item.id)
        }
        Haptics.tap()
    }

    func selectText(id: String?) {
        if let id {
            for track in player.textTracks where track.trackId == id {
                track.isSelectedExclusively = true
            }
        } else {
            player.deselectAllTextTracks()
        }
        selectedTextID = id
        didUserPickTracks = true
        if let item = queue.current {
            let name = id.flatMap { chosen in textChoices.first { $0.id == chosen }?.name }
            TrackPreferences.setText(name, for: item.id)
        }
        Haptics.tap()
    }

    func toggleFill() {
        isFillMode.toggle()
        player.videoFitMode = isFillMode ? .larger : .smaller
        Haptics.tap()
    }

    /// Ручной повтор с экрана ошибки: сбрасывает лимит авторетрая.
    func retry() {
        didAutoRetry = false
        pendingSeekMs = Int64(positionSeconds * 1000)
        phase = .resolving
        resolveAndPlayCurrent()
    }

    // MARK: - Now Playing и пульт

    private enum RemoteAction {
        case play
        case pause
        case toggle
        case next
        case previous
        case forward
        case backward
        case position
    }

    private func setupRemoteCommands() {
        let center = MPRemoteCommandCenter.shared()
        wire(center.playCommand, .play)
        wire(center.pauseCommand, .pause)
        wire(center.togglePlayPauseCommand, .toggle)
        wire(center.nextTrackCommand, .next)
        wire(center.previousTrackCommand, .previous)
        center.skipForwardCommand.preferredIntervals = [15]
        center.skipBackwardCommand.preferredIntervals = [15]
        wire(center.skipForwardCommand, .forward)
        wire(center.skipBackwardCommand, .backward)
        wire(center.changePlaybackPositionCommand, .position)
        center.nextTrackCommand.isEnabled = queue.hasNext
        center.previousTrackCommand.isEnabled = queue.hasPrevious
    }

    private func wire(_ command: MPRemoteCommand, _ action: RemoteAction) {
        command.isEnabled = true
        command.addTarget { [weak self] event in
            let position = (event as? MPChangePlaybackPositionCommandEvent)?.positionTime
            Task { @MainActor in
                self?.handleRemote(action, position: position)
            }
            return .success
        }
    }

    private func handleRemote(_ action: RemoteAction, position: Double?) {
        switch action {
        case .play: player.play()
        case .pause: player.pause()
        case .toggle: togglePlay()
        case .next: playNext()
        case .previous: playPrevious()
        case .forward: jump(15)
        case .backward: jump(-15)
        case .position:
            if let position { seek(to: position) }
        }
    }

    private func teardownRemoteCommands() {
        let center = MPRemoteCommandCenter.shared()
        for command in [
            center.playCommand, center.pauseCommand, center.togglePlayPauseCommand,
            center.nextTrackCommand, center.previousTrackCommand,
            center.skipForwardCommand, center.skipBackwardCommand,
            center.changePlaybackPositionCommand,
        ] {
            command.removeTarget(nil)
        }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
    }

    private func updateNowPlaying() {
        var info: [String: Any] = [
            MPMediaItemPropertyTitle: title,
            MPNowPlayingInfoPropertyPlaybackRate: player.isPlaying ? Double(rate) : 0,
            MPNowPlayingInfoPropertyElapsedPlaybackTime: positionSeconds,
            MPNowPlayingInfoPropertyMediaType: MPNowPlayingInfoMediaType.video.rawValue,
        ]
        if let subtitle { info[MPMediaItemPropertyArtist] = subtitle }
        if durationSeconds > 0 { info[MPMediaItemPropertyPlaybackDuration] = durationSeconds }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
        let center = MPRemoteCommandCenter.shared()
        center.nextTrackCommand.isEnabled = queue.hasNext
        center.previousTrackCommand.isEnabled = queue.hasPrevious
    }

    // MARK: - Внутренние помощники

    private func applyPendingSeekIfPossible() {
        guard let ms = pendingSeekMs, player.isSeekable else { return }
        pendingSeekMs = nil
        player.time = VLCTime(number: NSNumber(value: ms))
        positionSeconds = Double(ms) / 1000
    }

    private func emitProgress(force: Bool) {
        guard durationSeconds > 0, let item = queue.current else { return }
        let now = Date()
        if !force, now.timeIntervalSince(lastEmit) < 5 { return }
        lastEmit = now
        item.onProgress(positionSeconds, durationSeconds)
        captureSnapshotIfNeeded(force: force)
    }

    /// Снимок кадра для карточки продолжения: не чаще раза в минуту и не на титрах.
    private func captureSnapshotIfNeeded(force: Bool) {
        guard let thumbnails, let item = queue.current, phase == .playing || force else { return }
        guard positionSeconds > 30, durationSeconds > 0 else { return }
        let now = Date()
        guard force || now.timeIntervalSince(lastSnapshot) > 60 else { return }
        lastSnapshot = now
        let path = ThumbnailStore.path(for: item.id)
        player.saveVideoSnapshot(at: path.path, withWidth: 640, andHeight: 0)
        // Файл появляется асинхронно — обновляем вид с небольшой задержкой
        Task { [weak thumbnails] in
            try? await Task.sleep(for: .milliseconds(600))
            thumbnails?.didSave(key: item.id)
        }
    }

    private func refreshTracks() {
        audioChoices = player.audioTracks.map {
            TrackChoice(id: $0.trackId, name: $0.trackName, language: $0.language)
        }
        textChoices = player.textTracks.map {
            TrackChoice(id: $0.trackId, name: $0.trackName, language: $0.language)
        }
        selectedAudioID = player.audioTracks.first { $0.isSelected }?.trackId
        selectedTextID = player.textTracks.first { $0.isSelected }?.trackId
        applyTrackPreferencesIfNeeded()
    }

    /// Выбранная в прошлой серии озвучка включается сама: у релизов с тремя
    /// дубляжами переключать её каждую серию — главное раздражение.
    private func applyTrackPreferencesIfNeeded() {
        // Дорожки VLC добавляет постепенно, поэтому пробуем на каждом обновлении,
        // пока пользователь не выберет что-то сам.
        guard !didUserPickTracks, let item = queue.current else { return }
        if let wanted = TrackPreferences.audio(for: item.id),
           let match = matchingChoice(named: wanted, in: audioChoices),
           match.id != selectedAudioID {
            for track in player.audioTracks where track.trackId == match.id {
                track.isSelectedExclusively = true
            }
            selectedAudioID = match.id
        }
        if let wantedText = TrackPreferences.text(for: item.id) {
            if wantedText.isEmpty {
                if selectedTextID != nil {
                    player.deselectAllTextTracks()
                    selectedTextID = nil
                }
            } else if let match = matchingChoice(named: wantedText, in: textChoices),
                      match.id != selectedTextID {
                for track in player.textTracks where track.trackId == match.id {
                    track.isSelectedExclusively = true
                }
                selectedTextID = match.id
            }
        }
    }

    /// Ищем дорожку по имени, затем по языку: идентификаторы между сериями разные.
    private func matchingChoice(named name: String, in choices: [TrackChoice]) -> TrackChoice? {
        if let exact = choices.first(where: { $0.name == name }) { return exact }
        if let similar = choices.first(where: { $0.name.localizedCaseInsensitiveContains(name) }) {
            return similar
        }
        return choices.first { choice in
            guard let language = choice.language, !language.isEmpty else { return false }
            return name.localizedCaseInsensitiveContains(language)
        }
    }

    /// Конец файла: либо автопереход к следующей серии, либо экран «досмотрено».
    private func finishCurrent() {
        phase = .ended
        emitProgress(force: true)
        updateNowPlaying()
        guard queue.hasNext else { return }
        countdownTask?.cancel()
        countdownTask = Task { [weak self] in
            for remaining in stride(from: 5, through: 1, by: -1) {
                guard let self, !Task.isCancelled else { return }
                self.autoNextCountdown = remaining
                try? await Task.sleep(for: .seconds(1))
            }
            guard let self, !Task.isCancelled else { return }
            self.autoNextCountdown = nil
            self.playNext()
        }
    }

    private func handleStopOrError(message: String?) {
        guard !didShutdown, !isSwitchingMedia else { return }
        // Остановка у самого конца — нормальное завершение серии
        if durationSeconds > 0, positionSeconds > durationSeconds - 5 {
            finishCurrent()
            return
        }
        if didAutoRetry || queue.current?.isLocal == true {
            phase = .failed(message ?? "Воспроизведение остановилось")
            return
        }
        // Скорее всего протухла CDN-ссылка: один авторетрай со свежей ссылкой с той же позиции
        didAutoRetry = true
        let resumeAt = positionSeconds
        guard let item = queue.current else { return }
        phase = .resolving
        resolveTask?.cancel()
        resolveTask = Task { [weak self] in
            guard let self else { return }
            do {
                let fresh = try await item.resolveURL()
                guard !Task.isCancelled, !self.didShutdown else { return }
                self.pendingSeekMs = Int64(resumeAt * 1000)
                self.load(url: fresh)
            } catch {
                self.phase = .failed("Не получилось обновить ссылку: \(error.localizedDescription)")
            }
        }
    }
}

extension PlayerViewModel: @preconcurrency VLCMediaPlayerDelegate {
    func mediaPlayerStateChanged(_ newState: VLCMediaPlayerState) {
        switch newState {
        case .opening:
            phase = .opening
        case .playing:
            applyPendingSeekIfPossible()
            refreshTracks()
            phase = .playing
            updateNowPlaying()
        case .paused:
            phase = .paused
            emitProgress(force: true)
            updateNowPlaying()
        case .stopped:
            handleStopOrError(message: nil)
        case .error:
            handleStopOrError(message: VLCLibrary.currentErrorMessage ?? "Поток недоступен")
        default:
            break
        }
    }

    func mediaPlayerTimeChanged(_ aNotification: Notification) {
        applyPendingSeekIfPossible()
        guard !isScrubbing else { return }
        let reported = Double(player.time.intValue) / 1000
        // После перемотки VLC какое-то время отдаёт время старого буфера
        if let target = seekTarget {
            if Date() < seekGuardUntil, abs(reported - target) > 5 {
                return
            }
            seekTarget = nil
        }
        positionSeconds = reported
        if phase != .paused, phase != .ended {
            phase = .playing
        }
        emitProgress(force: false)
    }

    func mediaPlayerLengthChanged(_ length: Int64) {
        durationSeconds = Double(length) / 1000
        updateNowPlaying()
    }

    func mediaPlayerBufferingChanged(_ progress: Float) {
        if progress < 1 {
            if phase != .paused, phase != .ended {
                phase = .buffering(progress)
            }
        } else if case .buffering = phase {
            phase = player.isPlaying ? .playing : .paused
        }
    }

    func mediaPlayerTrackAdded(_ trackId: String, with type: VLCMedia.TrackType) {
        refreshTracks()
    }
}
