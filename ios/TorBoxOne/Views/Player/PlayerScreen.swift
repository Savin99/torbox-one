import SwiftUI

/// Плеер целиком: видео, жесты, контролы и служебные оверлеи.
struct PlayerScreen: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @State private var vm: PlayerViewModel

    // Состояние взаимодействия
    @State private var controlsVisible = true
    @State private var hideToken = UUID()
    @State private var sheet: PlayerSheet?
    @State private var hud: HUDKind?
    @State private var hudValue: Double = 0
    @State private var seekFlash: SeekFlash?
    @State private var scrubFraction: Double?
    @State private var dragMode: DragMode = .undecided
    @State private var dragStartFraction: Double = 0
    @State private var showResumePill = false
    /// Кадры для перелистывания по времени.
    @State private var preview = ScrubPreviewProvider()
    @State private var hudToken: UUID?

    init(queue: PlayerQueue, thumbnails: ThumbnailStore? = nil) {
        _vm = State(initialValue: PlayerViewModel(queue: queue, thumbnails: thumbnails))
    }

    init(item: PlayerTrackItem, thumbnails: ThumbnailStore? = nil) {
        _vm = State(initialValue: PlayerViewModel(queue: PlayerQueue(single: item), thumbnails: thumbnails))
    }

    enum HUDKind {
        case brightness
        case volume
    }

    enum DragMode {
        case undecided
        case brightness
        case volume
        case scrub
    }

    struct SeekFlash: Equatable {
        let forward: Bool
        let seconds: Int
        let id = UUID()

        static func == (lhs: SeekFlash, rhs: SeekFlash) -> Bool { lhs.id == rhs.id }
    }

    var body: some View {
        GeometryReader { geo in
            ZStack {
                Color.black.ignoresSafeArea()
                VLCVideoSurface(vm: vm)
                    .ignoresSafeArea()

                gestureLayer(size: geo.size)

                if !vm.isLocked {
                    PlayerControlsOverlay(
                        vm: vm,
                        preview: preview,
                        visible: controlsVisible,
                        scrubFraction: $scrubFraction,
                        onClose: close,
                        onPoke: poke,
                        onSheet: { sheet = $0 }
                    )
                }

                PlayerStatusLayer(vm: vm, onClose: close, onRetry: { vm.retry() })

                if let hud {
                    HUDBadge(kind: hud, value: hudValue)
                        .allowsHitTesting(false)
                }
                if let flash = seekFlash {
                    SeekRipple(flash: flash)
                        .allowsHitTesting(false)
                        .id(flash.id)
                }
                if showResumePill, let from = vm.resumedFrom, vm.phase == .playing {
                    resumePill(from: from)
                }
                if vm.isLocked {
                    lockedHint
                }
            }
            .animation(.easeInOut(duration: 0.2), value: controlsVisible)
        }
        .statusBarHidden(true)
        .persistentSystemOverlays(.hidden)
        .preferredColorScheme(.dark)
        .sheet(item: $sheet) { kind in
            PlayerSheetView(vm: vm, kind: kind)
        }
        .task(id: hideToken) {
            // Автоскрытие контролов после бездействия. Цикл нужен потому, что в
            // момент первой попытки видео ещё открывается — тогда пробуем снова.
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(Self.autoHideSeconds))
                guard !Task.isCancelled else { return }
                if vm.phase == .playing, scrubFraction == nil, sheet == nil {
                    withAnimation { controlsVisible = false }
                    return
                }
            }
        }
        .task(id: hudToken) {
            // Показываем значение ещё мгновение после отпускания пальца
            guard hudToken != nil else { return }
            try? await Task.sleep(for: .milliseconds(700))
            guard !Task.isCancelled else { return }
            withAnimation { hud = nil }
        }
        .task(id: vm.resumedFrom) {
            guard vm.resumedFrom != nil else { return }
            showResumePill = true
            try? await Task.sleep(for: .seconds(5))
            withAnimation { showResumePill = false }
        }
        .onAppear {
            AppDelegate.orientationMask = .allButUpsideDown
            activeWindowScene()?.keyWindow?.rootViewController?
                .setNeedsUpdateOfSupportedInterfaceOrientations()
        }
        .onDisappear {
            vm.shutdown()
            AppDelegate.orientationMask = .portrait
            if let scene = activeWindowScene() {
                scene.requestGeometryUpdate(.iOS(interfaceOrientations: .portrait))
                scene.keyWindow?.rootViewController?.setNeedsUpdateOfSupportedInterfaceOrientations()
            }
        }
        .onChange(of: scenePhase) { _, newPhase in
            if newPhase == .active {
                vm.restoreAfterForeground()
            } else {
                vm.pauseForBackground()
            }
        }
        .onChange(of: vm.mediaURL) { _, url in
            preview.prepare(url: url)
        }
        .onChange(of: vm.phase) { _, newPhase in
            // На паузе, ошибке и в конце серии контролы должны быть видны
            switch newPhase {
            case .paused, .failed, .ended, .resolving:
                withAnimation { controlsVisible = true }
            default:
                break
            }
        }
    }

    // MARK: - Жесты

    private func gestureLayer(size: CGSize) -> some View {
        Color.clear
            .contentShape(Rectangle())
            .onTapGesture(count: 2) { location in
                guard !vm.isLocked else { return }
                let forward = location.x > size.width / 2
                vm.jump(forward ? 10 : -10)
                seekFlash = SeekFlash(forward: forward, seconds: 10)
                Haptics.tap()
                poke()
            }
            .onTapGesture {
                if vm.isLocked {
                    // На заблокированном экране тап только подсказывает, как разблокировать
                    Haptics.warning()
                    return
                }
                withAnimation { controlsVisible.toggle() }
                if controlsVisible { poke() }
            }
            .simultaneousGesture(
                DragGesture(minimumDistance: 12)
                    .onChanged { value in
                        guard !vm.isLocked else { return }
                        decideDragMode(value: value, size: size)
                        applyDrag(value: value, size: size)
                    }
                    .onEnded { _ in
                        finishDrag()
                    }
            )
    }

    private func decideDragMode(value: DragGesture.Value, size: CGSize) {
        guard dragMode == .undecided else { return }
        let dx = abs(value.translation.width)
        let dy = abs(value.translation.height)
        if dy > dx {
            dragMode = value.startLocation.x < size.width / 2 ? .brightness : .volume
            hudValue = dragMode == .brightness ? vm.brightness : vm.volume
            hud = dragMode == .brightness ? .brightness : .volume
        } else {
            guard vm.durationSeconds > 0 else { return }
            dragMode = .scrub
            dragStartFraction = vm.positionSeconds / vm.durationSeconds
            vm.beginScrub()
            withAnimation { controlsVisible = true }
        }
    }

    private func applyDrag(value: DragGesture.Value, size: CGSize) {
        switch dragMode {
        case .undecided:
            break
        case .brightness, .volume:
            // Полная высота экрана = весь диапазон
            let delta = -value.translation.height / max(size.height * 0.7, 1)
            let updated = min(max(hudValue + delta, 0), 1)
            hudValue = updated
            if dragMode == .brightness {
                vm.brightness = updated
            } else {
                vm.volume = updated
            }
        case .scrub:
            // Свайп по всей ширине = до 10 минут: хватает точности и на длинном фильме
            let span = min(vm.durationSeconds, 600)
            let deltaSeconds = value.translation.width / max(size.width, 1) * span
            let fraction = min(max(dragStartFraction + deltaSeconds / vm.durationSeconds, 0), 1)
            scrubFraction = fraction
            vm.scrubPreview(fraction: fraction)
            preview.request(fraction: fraction)
        }
    }

    private func finishDrag() {
        switch dragMode {
        case .scrub:
            if let fraction = scrubFraction {
                vm.endScrub(fraction: fraction)
                Haptics.tick()
            }
            scrubFraction = nil
            preview.cancel()
        case .brightness, .volume:
            hudToken = UUID()
        case .undecided:
            break
        }
        dragMode = .undecided
        poke()
    }

    // MARK: - Мелочи

    private func poke() {
        hideToken = UUID()
        if !controlsVisible {
            withAnimation { controlsVisible = true }
        }
    }

    private func close() {
        Haptics.tap()
        dismiss()
    }

    private func resumePill(from: Double) -> some View {
        VStack {
            Spacer()
            HStack(spacing: 10) {
                Image(systemName: "clock.arrow.circlepath")
                Text("Продолжаю с \(Format.clock(from))")
                    .font(.footnote.weight(.semibold))
                Button("Сначала") {
                    vm.startOver()
                    withAnimation { showResumePill = false }
                }
                .font(.footnote.weight(.bold))
                .foregroundStyle(Theme.accent)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(Capsule().fill(.ultraThinMaterial))
            .foregroundStyle(.white)
            .padding(.bottom, 110)
            .transition(.move(edge: .bottom).combined(with: .opacity))
        }
    }

    private var lockedHint: some View {
        VStack {
            HStack {
                Spacer()
                Button {
                    vm.isLocked = false
                    Haptics.press()
                    poke()
                } label: {
                    Image(systemName: "lock.open.fill")
                        .foregroundStyle(.white)
                }
                .buttonStyle(CircleIconButtonStyle())
                .accessibilityLabel("Разблокировать экран")
            }
            .padding(.horizontal, 20)
            .padding(.top, 12)
            Spacer()
        }
    }

    /// В отладке задержку автоскрытия можно продлить (осмотр интерфейса на симуляторе).
    private static var autoHideSeconds: Double {
        #if DEBUG
        let hold = UserDefaults.standard.double(forKey: "playerControlsHoldSeconds")
        if hold > 0 { return hold }
        #endif
        return 3.5
    }

    private func activeWindowScene() -> UIWindowScene? {
        UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .first { $0.activationState == .foregroundActive }
    }
}
