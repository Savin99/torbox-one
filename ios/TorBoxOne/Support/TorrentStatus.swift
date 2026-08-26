import SwiftUI

/// Состояние раздачи в человеческом виде: символ, цвет, подпись.
/// Раньше здесь были эмодзи — теперь символы SF, которые читает VoiceOver.
struct TorrentStatus {
    enum Kind {
        case ready
        case downloading
        case preparing
        case paused
        case problem
        case waiting
    }

    let kind: Kind
    let label: String

    var isFinished: Bool { kind == .ready }
    var isActive: Bool { kind == .downloading || kind == .preparing }

    var icon: String {
        switch kind {
        case .ready: "checkmark.circle.fill"
        case .downloading: "arrow.down.circle.fill"
        case .preparing: "hourglass"
        case .paused: "pause.circle.fill"
        case .problem: "exclamationmark.triangle.fill"
        case .waiting: "clock"
        }
    }

    var tint: Color {
        switch kind {
        case .ready: Theme.accent
        case .downloading: Theme.info
        case .preparing: Theme.instant
        case .paused: Theme.idle
        case .problem: Theme.danger
        case .waiting: Theme.idle
        }
    }

    init(torrent: TBTorrent) {
        let state = (torrent.downloadState ?? "unknown")
            .trimmingCharacters(in: .whitespaces).lowercased()
        let finished = torrent.downloadFinished == true || torrent.cached == true
            || ["cached", "completed", "uploading"].contains(state)
        if finished {
            (kind, label) = (.ready, "готово")
        } else if state.contains("error") || state.contains("missing") || state.contains("no seeds") {
            (kind, label) = (.problem, "нужна проверка")
        } else if state.contains("pause") {
            (kind, label) = (.paused, "на паузе")
        } else if ["metadl", "checkingresumedata", "queued", "checking"].contains(state) {
            (kind, label) = (.preparing, "подготовка")
        } else if torrent.active == true || state.contains("down") {
            (kind, label) = (.downloading, "скачивается")
        } else {
            (kind, label) = (.waiting, state.isEmpty ? "ожидание" : state)
        }
    }
}

/// Компактная плашка состояния: символ + подпись в цвете состояния.
struct StatusPill: View {
    let status: TorrentStatus

    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: status.icon)
                .font(.system(size: 11, weight: .bold))
            Text(status.label)
                .font(.caption.weight(.semibold))
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(Capsule().fill(status.tint.opacity(0.16)))
        .foregroundStyle(status.tint)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Состояние: \(status.label)")
    }
}
