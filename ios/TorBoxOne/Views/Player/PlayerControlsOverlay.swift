import SwiftUI

enum PlayerSheet: String, Identifiable {
    case settings
    case queue

    var id: String { rawValue }
}

/// Контролы плеера: минимум хрома, всё важное по углам, крупная шкала внизу.
struct PlayerControlsOverlay: View {
    let vm: PlayerViewModel
    let preview: ScrubPreviewProvider
    let visible: Bool
    @Binding var scrubFraction: Double?
    let onClose: () -> Void
    let onPoke: () -> Void
    let onSheet: (PlayerSheet) -> Void

    @State private var scrubSpeed: ScrubSpeed = .fast
    /// Показ кадра ещё секунду после отпускания пальца.
    @State private var previewLingerToken: UUID?

    /// Точность перелистывания зависит от того, как далеко палец от шкалы.
    enum ScrubSpeed {
        case fast
        case medium
        case fine
        case precise

        var factor: Double {
            switch self {
            case .fast: 1
            case .medium: 0.5
            case .fine: 0.2
            case .precise: 0.06
            }
        }

        var title: String {
            switch self {
            case .fast: "высокая"
            case .medium: "средняя"
            case .fine: "точная"
            case .precise: "очень точная"
            }
        }
    }

    /// В отладке показ кадра можно продлить (осмотр интерфейса на симуляторе).
    private static var previewLinger: Double {
        #if DEBUG
        let hold = UserDefaults.standard.double(forKey: "scrubPreviewHoldSeconds")
        if hold > 0 { return hold }
        #endif
        return 1.2
    }

    var body: some View {
        ZStack {
            if visible {
                LinearGradient(
                    colors: [.black.opacity(0.55), .clear, .clear, .black.opacity(0.75)],
                    startPoint: .top, endPoint: .bottom
                )
                .ignoresSafeArea()
                .allowsHitTesting(false)
                .transition(.opacity)

                VStack(spacing: 0) {
                    topBar
                    Spacer(minLength: 0)
                    centerButtons
                    Spacer(minLength: 0)
                    bottomBar
                }
                .transition(.opacity)
            }
        }
        .task(id: previewLingerToken) {
            guard previewLingerToken != nil else { return }
            try? await Task.sleep(for: .seconds(Self.previewLinger))
            guard !Task.isCancelled else { return }
            previewLingerToken = nil
            preview.cancel()
        }
    }

    // MARK: - Шапка

    private var topBar: some View {
        HStack(alignment: .top, spacing: 12) {
            Button {
                onClose()
            } label: {
                Image(systemName: "xmark")
                    .font(.headline.weight(.bold))
                    .foregroundStyle(.white)
            }
            .buttonStyle(CircleIconButtonStyle(diameter: 40))
            .accessibilityLabel("Закрыть плеер")

            fillToggle

            Spacer(minLength: 0)

            volumeControl
        }
        .padding(.horizontal, 16)
        .padding(.top, 10)
    }

    /// Пара «вписать / заполнить» — как сегментированный переключатель в референсе.
    private var fillToggle: some View {
        HStack(spacing: 2) {
            segment(icon: "rectangle", isOn: !vm.isFillMode, label: "Вписать в экран") {
                if vm.isFillMode { vm.toggleFill() }
                onPoke()
            }
            segment(icon: "rectangle.inset.filled", isOn: vm.isFillMode, label: "Заполнить экран") {
                if !vm.isFillMode { vm.toggleFill() }
                onPoke()
            }
        }
        .padding(3)
        .background(Capsule().fill(.ultraThinMaterial))
    }

    private func segment(icon: String, isOn: Bool, label: String, action: @escaping () -> Void) -> some View {
        Button {
            Haptics.tap()
            action()
        } label: {
            Image(systemName: icon)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(isOn ? .black : .white)
                .frame(width: 44, height: 32)
                .background(
                    Capsule().fill(isOn ? Color.white : .clear)
                )
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityAddTraits(isOn ? [.isSelected, .isButton] : .isButton)
    }

    /// Громкость: в ландшафте — ползунок, в портрете — только кнопка звука.
    private var volumeControl: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 10) {
                Text("\(Int(vm.volume * 100)) %")
                    .font(.caption.weight(.semibold))
                    .monospacedDigit()
                    .foregroundStyle(.white)
                Slider(value: Binding(
                    get: { vm.volume },
                    set: { vm.volume = $0; onPoke() }
                ), in: 0...1)
                .frame(width: 160)
                .tint(.white)
                muteButton
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(Capsule().fill(.ultraThinMaterial))

            muteButton
        }
    }

    private var muteButton: some View {
        Button {
            Haptics.tap()
            vm.isMuted.toggle()
            onPoke()
        } label: {
            Image(systemName: vm.isMuted ? "speaker.slash.fill" : "speaker.wave.2.fill")
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(.white)
                .contentTransition(.symbolEffect(.replace))
        }
        .buttonStyle(CircleIconButtonStyle(diameter: 40))
        .accessibilityLabel(vm.isMuted ? "Включить звук" : "Выключить звук")
    }

    // MARK: - Центр

    private var centerButtons: some View {
        HStack(spacing: 44) {
            seekButton(seconds: -10)
            Button {
                vm.togglePlay()
                onPoke()
            } label: {
                Image(systemName: playIcon)
                    .font(.system(size: 44, weight: .semibold))
                    .foregroundStyle(.white)
                    .contentTransition(.symbolEffect(.replace))
                    .hitTarget(64)
            }
            .buttonStyle(.plain)
            .accessibilityLabel(vm.phase == .playing ? "Пауза" : "Играть")
            seekButton(seconds: 10)
        }
        .opacity(isBusy ? 0 : 1)
        .disabled(isBusy)
    }

    /// Тонкие круговые стрелки, как в референсе: число — подписью внутри.
    private func seekButton(seconds: Double) -> some View {
        Button {
            vm.jump(seconds)
            onPoke()
        } label: {
            ZStack {
                Image(systemName: seconds < 0 ? "arrow.counterclockwise" : "arrow.clockwise")
                    .font(.system(size: 30, weight: .light))
                Text("\(abs(Int(seconds)))")
                    .font(.system(size: 10, weight: .bold))
                    .offset(y: 1)
            }
            .foregroundStyle(.white)
            .hitTarget(52)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(seconds < 0 ? "Назад на 10 секунд" : "Вперёд на 10 секунд")
    }

    private var playIcon: String {
        switch vm.phase {
        case .playing: "pause.fill"
        case .ended: "arrow.counterclockwise"
        default: "play.fill"
        }
    }

    private var isBusy: Bool {
        switch vm.phase {
        case .resolving, .opening, .buffering: true
        default: false
        }
    }

    // MARK: - Низ

    private var bottomBar: some View {
        VStack(spacing: 10) {
            if scrubFraction != nil || previewLingerToken != nil {
                scrubPreview
            }
            HStack(alignment: .bottom, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    if let line = subtitleLine {
                        Text(line)
                            .font(.caption)
                            .foregroundStyle(.white.opacity(0.75))
                            .lineLimit(1)
                    }
                    Text(vm.title)
                        .font(.title3.weight(.bold))
                        .foregroundStyle(.white)
                        .lineLimit(1)
                }
                Spacer(minLength: 8)
                actionPill
            }
            ScrubBar(
                fraction: scrubFraction ?? currentFraction,
                speed: scrubSpeed,
                onScrubStart: {
                    vm.beginScrub()
                    preview.request(fraction: currentFraction)
                },
                onScrub: { value, speed in
                    scrubSpeed = speed
                    scrubFraction = value
                    vm.scrubPreview(fraction: value)
                    preview.request(fraction: value)
                },
                onScrubEnd: { value in
                    vm.endScrub(fraction: value)
                    scrubFraction = nil
                    previewLingerToken = UUID()
                    onPoke()
                }
            )
            HStack {
                Text(Format.clock(scrubPosition))
                    .monospacedDigit()
                if let position = vm.positionLabel {
                    Text("· \(position)")
                        .foregroundStyle(.white.opacity(0.6))
                }
                Spacer()
                if vm.hasNext, vm.durationSeconds > 0, vm.durationSeconds - scrubPosition < 300 {
                    Button {
                        vm.playNext()
                    } label: {
                        Label("Следующая", systemImage: "forward.end.fill")
                            .font(.caption.weight(.bold))
                            .padding(.horizontal, 10)
                            .padding(.vertical, 6)
                            .background(Capsule().fill(.white.opacity(0.2)))
                    }
                    .accessibilityLabel("Включить следующую серию")
                }
                Text("−" + Format.clock(max(0, vm.durationSeconds - scrubPosition)))
                    .monospacedDigit()
            }
            .font(.caption.weight(.medium))
            .foregroundStyle(.white)
        }
        .padding(.horizontal, 20)
        .padding(.bottom, 14)
    }

    /// Строка над названием: сезон и серия, а если их нет — название раздачи.
    private var subtitleLine: String? {
        if let episode = vm.episodeLabel, let subtitle = vm.subtitle {
            return "\(episode) · \(subtitle)"
        }
        return vm.episodeLabel ?? vm.subtitle
    }

    private var actionPill: some View {
        HStack(spacing: 4) {
            Button {
                Haptics.tap()
                onSheet(.settings)
            } label: {
                Image(systemName: "gearshape.fill")
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: 44, height: 40)
            }
            .accessibilityLabel("Настройки воспроизведения")
            if vm.queue.items.count > 1 {
                Button {
                    Haptics.tap()
                    onSheet(.queue)
                } label: {
                    Image(systemName: "list.and.film")
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundStyle(.white)
                        .frame(width: 44, height: 40)
                }
                .accessibilityLabel("Список серий")
            }
        }
        .background(Capsule().fill(.ultraThinMaterial))
    }

    /// Куда попадём: кадр (для файлов на телефоне) или время со смещением.
    private var scrubPreview: some View {
        VStack(spacing: 6) {
            if preview.isSupported {
                ZStack {
                    RoundedRectangle(cornerRadius: Theme.radiusM, style: .continuous)
                        .fill(.black.opacity(0.55))
                    if let image = preview.image {
                        Image(uiImage: image)
                            .resizable()
                            .aspectRatio(contentMode: .fill)
                    } else {
                        ProgressView()
                            .tint(.white)
                    }
                }
                .frame(width: 176, height: 99)
                .clipShape(RoundedRectangle(cornerRadius: Theme.radiusM, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: Theme.radiusM, style: .continuous)
                        .strokeBorder(.white.opacity(0.25), lineWidth: 1)
                )
                .overlay(alignment: .bottom) {
                    timeBubble
                        .padding(4)
                }
            } else {
                timeBubble
            }

            VStack(spacing: 1) {
                Text("Скорость перелистывания: \(scrubSpeed.title)")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.white.opacity(0.9))
                Text("Отведи палец от шкалы — станет точнее")
                    .font(.caption2)
                    .foregroundStyle(.white.opacity(0.6))
            }
        }
        .frame(maxWidth: .infinity)
        .transition(.opacity)
        .accessibilityHidden(true)
    }

    /// Время под пальцем и на сколько это вперёд или назад.
    private var timeBubble: some View {
        HStack(spacing: 8) {
            Text(Format.clock(scrubPosition))
                .font(.title3.weight(.bold))
                .monospacedDigit()
            if let delta = deltaLabel {
                Text(delta)
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(.white.opacity(0.7))
                    .monospacedDigit()
            }
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(Capsule().fill(.black.opacity(0.6)))
    }

    private var deltaLabel: String? {
        guard scrubFraction != nil else { return nil }
        let delta = scrubPosition - vm.positionSeconds
        guard abs(delta) >= 1 else { return nil }
        let sign = delta > 0 ? "+" : "−"
        return sign + Format.clock(abs(delta))
    }

    private var currentFraction: Double {
        vm.durationSeconds > 0 ? min(vm.positionSeconds / vm.durationSeconds, 1) : 0
    }

    private var scrubPosition: Double {
        if let fraction = scrubFraction {
            return fraction * vm.durationSeconds
        }
        return vm.positionSeconds
    }
}

/// Шкала времени: тонкая полоса, растущая при перетаскивании, с переменной
/// точностью — чем дальше палец от шкалы, тем мельче шаг.
struct ScrubBar: View {
    let fraction: Double
    let speed: PlayerControlsOverlay.ScrubSpeed
    let onScrubStart: () -> Void
    let onScrub: (Double, PlayerControlsOverlay.ScrubSpeed) -> Void
    let onScrubEnd: (Double) -> Void

    @State private var isDragging = false
    @State private var startFraction: Double = 0
    @State private var lastTickBucket = -1

    private let trackHeight: CGFloat = 4

    var body: some View {
        GeometryReader { geo in
            let width = max(geo.size.width, 1)
            ZStack(alignment: .leading) {
                Capsule()
                    .fill(.white.opacity(0.28))
                    .frame(height: isDragging ? trackHeight + 4 : trackHeight)
                Capsule()
                    .fill(.white)
                    .frame(
                        width: width * min(max(fraction, 0), 1),
                        height: isDragging ? trackHeight + 4 : trackHeight
                    )
                if isDragging {
                    Circle()
                        .fill(.white)
                        .frame(width: 14, height: 14)
                        .shadow(color: .black.opacity(0.4), radius: 4, y: 1)
                        .offset(x: width * min(max(fraction, 0), 1) - 7)
                }
            }
            .frame(height: 28)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { value in
                        if !isDragging {
                            isDragging = true
                            startFraction = fraction
                            onScrubStart()
                            Haptics.tick()
                        }
                        let speed = Self.speed(forVerticalOffset: value.translation.height)
                        let delta = value.translation.width / width * speed.factor
                        let next = min(max(startFraction + delta, 0), 1)
                        let bucket = Int(next * 40)
                        if bucket != lastTickBucket {
                            lastTickBucket = bucket
                            Haptics.tick()
                        }
                        onScrub(next, speed)
                    }
                    .onEnded { value in
                        let speed = Self.speed(forVerticalOffset: value.translation.height)
                        let delta = value.translation.width / width * speed.factor
                        // Тап без движения — прыжок в точку касания
                        let next = abs(value.translation.width) < 2
                            ? min(max(value.location.x / width, 0), 1)
                            : min(max(startFraction + delta, 0), 1)
                        isDragging = false
                        onScrubEnd(next)
                    }
            )
            .animation(.spring(response: 0.25, dampingFraction: 0.75), value: isDragging)
        }
        .frame(height: 28)
        .accessibilityElement()
        .accessibilityLabel("Позиция в серии")
        .accessibilityValue("\(Int(fraction * 100)) процентов")
        .accessibilityAdjustableAction { direction in
            let step = 0.02
            switch direction {
            case .increment: onScrubEnd(min(fraction + step, 1))
            case .decrement: onScrubEnd(max(fraction - step, 0))
            default: break
            }
        }
    }

    private static func speed(forVerticalOffset offset: CGFloat) -> PlayerControlsOverlay.ScrubSpeed {
        switch abs(offset) {
        case ..<40: .fast
        case ..<90: .medium
        case ..<150: .fine
        default: .precise
        }
    }
}
