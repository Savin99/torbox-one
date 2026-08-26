import Foundation
import Observation
import UIKit
import VLCKit

/// Кадры для перелистывания по времени: пока тянешь шкалу, показываем кадр
/// из этого места. VLCKit умеет достать его отдельно от основного плеера.
@Observable
final class ScrubPreviewProvider {
    private(set) var image: UIImage?
    private(set) var isLoading = false
    /// Кадры достаём только у локальных файлов: по сети это лишний поток
    /// и лишний трафик, а тумбнейлер VLCKit на альфе часто не успевает.
    private(set) var isSupported = false

    @ObservationIgnored private var url: URL?
    @ObservationIgnored private var thumbnailer: VLCMediaThumbnailer?
    @ObservationIgnored private var bridge: ThumbnailerBridge?
    @ObservationIgnored private var pendingFraction: Double?
    @ObservationIgnored private var requestTask: Task<Void, Never>?
    /// Кадр стоит просить не чаще, чем он успевает приходить.
    @ObservationIgnored private var isFetching = false

    func prepare(url: URL?) {
        guard self.url != url else { return }
        self.url = url
        isSupported = url?.isFileURL == true
        image = nil
        cancel()
    }

    /// Позиция 0…1. Запросы схлопываются: пока идёт один, копится последний.
    func request(fraction: Double) {
        guard isSupported, url != nil else { return }
        pendingFraction = min(max(fraction, 0), 1)
        guard !isFetching else { return }
        requestTask?.cancel()
        requestTask = Task { [weak self] in
            // Небольшая пауза: пока палец быстро едет, кадры не нужны
            try? await Task.sleep(for: .milliseconds(180))
            guard !Task.isCancelled else { return }
            self?.fetchPending()
        }
    }

    func cancel() {
        requestTask?.cancel()
        requestTask = nil
        thumbnailer?.cancel()
        thumbnailer = nil
        bridge = nil
        isFetching = false
        isLoading = false
        pendingFraction = nil
    }

    private func fetchPending() {
        guard let url, let fraction = pendingFraction, let media = VLCMedia(url: url) else { return }
        pendingFraction = nil
        isFetching = true
        isLoading = image == nil
        let bridge = ThumbnailerBridge()
        bridge.onFinish = { [weak self] cgImage in
            guard let self else { return }
            if let cgImage {
                self.image = UIImage(cgImage: cgImage)
            }
            self.isLoading = false
            self.isFetching = false
            // Пока тянули, появилась новая позиция — берём следующий кадр
            if self.pendingFraction != nil {
                self.fetchPending()
            }
        }
        self.bridge = bridge
        let thumbnailer = VLCMediaThumbnailer(media: media, andDelegate: bridge)
        thumbnailer.snapshotPosition = Float(fraction)
        thumbnailer.thumbnailWidth = 320
        thumbnailer.thumbnailHeight = 180
        thumbnailer.preciseSeek = false
        self.thumbnailer = thumbnailer
        thumbnailer.fetchThumbnail()
    }
}

/// Мост до делегата VLC: колбэки приходят не на main, поэтому переносим их сами.
private final class ThumbnailerBridge: NSObject, VLCMediaThumbnailerDelegate {
    nonisolated(unsafe) var onFinish: ((CGImage?) -> Void)?

    nonisolated func mediaThumbnailer(_ mediaThumbnailer: VLCMediaThumbnailer, didFinishThumbnail thumbnail: CGImage) {
        deliver(thumbnail)
    }

    nonisolated func mediaThumbnailerDidTimeOut(_ mediaThumbnailer: VLCMediaThumbnailer) {
        deliver(nil)
    }

    private nonisolated func deliver(_ image: CGImage?) {
        let handler = onFinish
        DispatchQueue.main.async {
            MainActor.assumeIsolated {
                handler?(image)
            }
        }
    }
}
