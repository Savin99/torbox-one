import SwiftUI

/// Служебные состояния поверх видео: получение ссылки, буферизация,
/// ошибка и конец серии с автопереходом.
struct PlayerStatusLayer: View {
    let vm: PlayerViewModel
    let onClose: () -> Void
    let onRetry: () -> Void

    var body: some View {
        ZStack {
            switch vm.phase {
            case .resolving:
                busyCard(title: "Готовлю \(vm.title)", subtitle: "Получаю ссылку на файл…")
            case .opening:
                busyCard(title: vm.title, subtitle: "Открываю поток…")
            case .buffering(let progress):
                bufferingCard(progress: progress)
            case .failed(let message):
                failedCard(message)
            case .ended:
                endedCard
            case .playing, .paused:
                EmptyView()
            }
        }
        .animation(.easeInOut(duration: 0.2), value: vm.phase)
    }

    private func busyCard(title: String, subtitle: String) -> some View {
        VStack(spacing: 12) {
            ProgressView()
                .controlSize(.large)
                .tint(.white)
            Text(title)
                .font(.headline)
                .foregroundStyle(.white)
                .multilineTextAlignment(.center)
                .lineLimit(2)
            Text(subtitle)
                .font(.footnote)
                .foregroundStyle(.white.opacity(0.7))
        }
        .padding(28)
        .background(RoundedRectangle(cornerRadius: Theme.radiusL, style: .continuous).fill(.ultraThinMaterial))
        .padding(32)
        .transition(.opacity)
    }

    private func bufferingCard(progress: Float) -> some View {
        VStack(spacing: 10) {
            ProgressRing(
                fraction: Double(progress),
                tint: .white,
                diameter: 56,
                lineWidth: 5
            )
            Text("Буферизация")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.white.opacity(0.85))
        }
        .padding(20)
        .background(Circle().fill(.black.opacity(0.35)))
        .transition(.opacity)
    }

    private func failedCard(_ message: String) -> some View {
        VStack(spacing: 16) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 38))
                .foregroundStyle(Theme.instant)
            Text("Не получается воспроизвести")
                .font(.headline)
                .foregroundStyle(.white)
            Text(message)
                .font(.footnote)
                .foregroundStyle(.white.opacity(0.75))
                .multilineTextAlignment(.center)
                .lineLimit(4)
            HStack(spacing: 12) {
                Button("Обновить ссылку") {
                    Haptics.press()
                    onRetry()
                }
                .buttonStyle(AccentButtonStyle(fullWidth: false))
                if vm.hasNext {
                    Button("Следующая") {
                        vm.playNext()
                    }
                    .buttonStyle(SoftButtonStyle(tint: .white))
                }
                Button("Закрыть") {
                    onClose()
                }
                .buttonStyle(SoftButtonStyle(tint: .white))
            }
        }
        .padding(28)
        .frame(maxWidth: 460)
        .background(RoundedRectangle(cornerRadius: Theme.radiusL, style: .continuous).fill(.ultraThinMaterial))
        .padding(24)
        .transition(.opacity)
    }

    private var endedCard: some View {
        VStack(spacing: 16) {
            if let countdown = vm.autoNextCountdown, vm.hasNext {
                ZStack {
                    ProgressRing(
                        fraction: Double(countdown) / 5.0,
                        tint: Theme.accent,
                        diameter: 64,
                        lineWidth: 5,
                        symbol: "forward.fill"
                    )
                }
                VStack(spacing: 4) {
                    Text("Следующая серия через \(countdown)")
                        .font(.headline)
                        .foregroundStyle(.white)
                    if let next = vm.nextTitle {
                        Text(next)
                            .font(.footnote)
                            .foregroundStyle(.white.opacity(0.75))
                            .lineLimit(2)
                            .multilineTextAlignment(.center)
                    }
                }
                HStack(spacing: 12) {
                    Button("Смотреть сейчас") {
                        vm.playNext()
                    }
                    .buttonStyle(AccentButtonStyle(fullWidth: false))
                    Button("Остаться") {
                        Haptics.tap()
                        vm.cancelAutoNext()
                    }
                    .buttonStyle(SoftButtonStyle(tint: .white))
                }
            } else {
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 38))
                    .foregroundStyle(Theme.accent)
                VStack(spacing: 4) {
                    Text(vm.hasNext ? "Серия закончилась" : "Досмотрено")
                        .font(.headline)
                        .foregroundStyle(.white)
                    if vm.hasNext, let next = vm.nextTitle {
                        Text("Далее: \(next)")
                            .font(.footnote)
                            .foregroundStyle(.white.opacity(0.75))
                            .lineLimit(2)
                            .multilineTextAlignment(.center)
                    }
                }
                HStack(spacing: 12) {
                    if vm.hasNext {
                        Button("Следующая серия") {
                            vm.playNext()
                        }
                        .buttonStyle(AccentButtonStyle(fullWidth: false))
                    }
                    Button("Заново") {
                        Haptics.tap()
                        vm.restartCurrent()
                    }
                    .buttonStyle(SoftButtonStyle(tint: .white))
                    Button("Закрыть") {
                        onClose()
                    }
                    .buttonStyle(SoftButtonStyle(tint: .white))
                }
            }
        }
        .padding(28)
        .frame(maxWidth: 520)
        .background(RoundedRectangle(cornerRadius: Theme.radiusL, style: .continuous).fill(.ultraThinMaterial))
        .padding(24)
        .transition(.opacity)
    }
}

/// Индикатор яркости и громкости при вертикальном свайпе.
struct HUDBadge: View {
    let kind: PlayerScreen.HUDKind
    let value: Double

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: icon)
                .font(.title2)
                .foregroundStyle(.white)
                .contentTransition(.symbolEffect(.replace))
            ZStack(alignment: .bottom) {
                Capsule().fill(.white.opacity(0.22))
                Capsule().fill(.white)
                    .frame(height: max(4, 120 * value))
            }
            .frame(width: 6, height: 120)
            Text("\(Int(value * 100))")
                .font(.caption2.weight(.bold))
                .monospacedDigit()
                .foregroundStyle(.white)
        }
        .padding(.vertical, 16)
        .padding(.horizontal, 14)
        .background(RoundedRectangle(cornerRadius: Theme.radiusM, style: .continuous).fill(.ultraThinMaterial))
        .transition(.opacity)
    }

    private var icon: String {
        switch kind {
        case .brightness: value > 0.5 ? "sun.max.fill" : "sun.min.fill"
        case .volume: value == 0 ? "speaker.slash.fill" : value > 0.5 ? "speaker.wave.3.fill" : "speaker.wave.1.fill"
        }
    }
}

/// Всплеск при двойном тапе: «+10 сек» на нужной половине экрана.
struct SeekRipple: View {
    let flash: PlayerScreen.SeekFlash

    @State private var shown = false

    var body: some View {
        HStack {
            if flash.forward { Spacer() }
            VStack(spacing: 6) {
                Image(systemName: flash.forward ? "goforward.10" : "gobackward.10")
                    .font(.system(size: 34, weight: .semibold))
                Text("\(flash.forward ? "+" : "−")\(flash.seconds) сек")
                    .font(.caption.weight(.bold))
            }
            .foregroundStyle(.white)
            .padding(24)
            .background(Circle().fill(.white.opacity(0.14)))
            .padding(.horizontal, 28)
            .opacity(shown ? 1 : 0)
            .scaleEffect(shown ? 1 : 0.8)
            if !flash.forward { Spacer() }
        }
        .allowsHitTesting(false)
        .task {
            withAnimation(.easeOut(duration: 0.12)) { shown = true }
            try? await Task.sleep(for: .milliseconds(420))
            withAnimation(.easeIn(duration: 0.2)) { shown = false }
        }
    }
}
