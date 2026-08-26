import SwiftUI

/// Шапка карточки: обложка тайтла во всю ширину, под ней — логотип и мета-строка.
/// Текст держим под картинкой, а не поверх: логотипы бывают любой высоты.
struct BackdropHeader: View {
    let meta: TitleMeta?
    let fallbackTitle: String
    /// Высота картинки: на детальном экране больше, в списках меньше.
    var height: CGFloat = 260

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ZStack(alignment: .bottom) {
                backdrop
                    .frame(height: height)
                    .clipped()
                LinearGradient(
                    colors: [.clear, Theme.canvasTop.opacity(0.55), Theme.canvasTop],
                    startPoint: .init(x: 0.5, y: 0.45),
                    endPoint: .bottom
                )
                .frame(height: height)
                .allowsHitTesting(false)
            }
            .frame(height: height)

            VStack(alignment: .leading, spacing: 6) {
                logoOrTitle
                if let line = meta?.metaLine, !line.isEmpty {
                    Text(line)
                        .font(.footnote.weight(.medium))
                        .foregroundStyle(Theme.textSecondary)
                        .lineLimit(2)
                }
            }
            .padding(.horizontal, Theme.gutter)
        }
    }

    @ViewBuilder
    private var logoOrTitle: some View {
        if let logo = meta?.logoUrl, !logo.isEmpty, let url = URL(string: logo) {
            AsyncImage(url: url) { phase in
                if let image = phase.image {
                    image
                        .resizable()
                        .scaledToFit()
                        .frame(height: 52)
                        .frame(maxWidth: 260, alignment: .leading)
                } else {
                    titleText
                }
            }
        } else {
            titleText
        }
    }

    private var titleText: some View {
        Text(meta?.title.isEmpty == false ? meta!.title : fallbackTitle)
            .font(.system(size: 26, weight: .heavy, design: .rounded))
            .foregroundStyle(Theme.textPrimary)
            .lineLimit(2)
    }

    @ViewBuilder
    private var backdrop: some View {
        let url = meta?.backdropUrl.isEmpty == false ? meta?.backdropUrl : meta?.posterUrl
        if let url, let parsed = URL(string: url) {
            AsyncImage(url: parsed, transaction: Transaction(animation: .easeOut(duration: 0.3))) { phase in
                switch phase {
                case .success(let image):
                    image.resizable().aspectRatio(contentMode: .fill)
                default:
                    placeholder
                }
            }
        } else {
            placeholder
        }
    }

    private var placeholder: some View {
        LinearGradient(
            colors: [Theme.violet.opacity(0.45), Theme.accent.opacity(0.30)],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
    }
}

/// Небольшой постер для строк списка: 2:3, свой фолбэк, поверх — прогресс.
struct PosterThumb: View {
    let meta: TitleMeta?
    let fallbackTitle: String
    var width: CGFloat = 46
    /// Кольцо прогресса поверх постера для активных загрузок.
    var progress: Double?
    var tint: Color = Theme.accent

    var body: some View {
        ZStack {
            if let url = meta?.posterPreviewUrl, !url.isEmpty, let parsed = URL(string: url) {
                AsyncImage(url: parsed) { phase in
                    switch phase {
                    case .success(let image):
                        image.resizable().aspectRatio(contentMode: .fill)
                    case .empty:
                        Theme.hairline.opacity(0.08)
                    default:
                        fallback
                    }
                }
            } else {
                fallback
            }
            if let progress {
                Color.black.opacity(0.45)
                ProgressRing(fraction: progress, tint: tint, diameter: width * 0.72, lineWidth: 3)
            }
        }
        .frame(width: width, height: width * 1.5)
        .clipShape(RoundedRectangle(cornerRadius: Theme.radiusS, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: Theme.radiusS, style: .continuous)
                .strokeBorder(Theme.hairline.opacity(0.10), lineWidth: 0.5)
        )
        .accessibilityHidden(true)
    }

    private var fallback: some View {
        ZStack {
            LinearGradient(
                colors: [Theme.violet.opacity(0.35), Theme.accent.opacity(0.25)],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
            Image(systemName: "film.stack")
                .font(.system(size: width * 0.38, weight: .semibold))
                .foregroundStyle(.white.opacity(0.8))
        }
    }
}

/// «Актёрский состав и съёмочная группа» — карусель как в Infuse.
struct CastRow: View {
    let people: [PersonMeta]

    var body: some View {
        if !people.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                SectionHeader(title: "Актёры и съёмочная группа")
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(alignment: .top, spacing: 12) {
                        ForEach(people) { person in
                            card(person)
                        }
                    }
                    .padding(.vertical, 2)
                }
            }
        }
    }

    private func card(_ person: PersonMeta) -> some View {
        VStack(spacing: 6) {
            ZStack {
                RoundedRectangle(cornerRadius: Theme.radiusM, style: .continuous)
                    .fill(Theme.hairline.opacity(0.08))
                if let url = URL(string: person.photoUrl), !person.photoUrl.isEmpty {
                    AsyncImage(url: url) { phase in
                        switch phase {
                        case .success(let image):
                            image.resizable().aspectRatio(contentMode: .fill)
                        default:
                            Image(systemName: person.isDirector ? "megaphone" : "person.fill")
                                .font(.title2)
                                .foregroundStyle(Theme.textTertiary)
                        }
                    }
                } else {
                    Image(systemName: person.isDirector ? "megaphone" : "person.fill")
                        .font(.title2)
                        .foregroundStyle(Theme.textTertiary)
                }
            }
            .frame(width: 78, height: 78)
            .clipShape(RoundedRectangle(cornerRadius: Theme.radiusM, style: .continuous))

            Text(person.name)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(Theme.textPrimary)
                .lineLimit(2)
                .multilineTextAlignment(.center)
            if !person.subtitle.isEmpty {
                Text(person.subtitle)
                    .font(.caption2)
                    .foregroundStyle(Theme.textTertiary)
                    .lineLimit(2)
                    .multilineTextAlignment(.center)
            }
        }
        .frame(width: 88)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel([person.name, person.subtitle].filter { !$0.isEmpty }.joined(separator: ", "))
    }
}

/// Рейтинг источника рядом с описанием — как «TMDB 4,0» в Infuse.
struct RatingBadgeRow: View {
    let meta: TitleMeta

    var body: some View {
        if let value = meta.ratingValue {
            HStack(spacing: 8) {
                HStack(spacing: 6) {
                    Image(systemName: "star.fill")
                        .font(.caption2)
                    Text(String(format: "%.1f", value))
                        .font(.subheadline.weight(.bold))
                        .monospacedDigit()
                }
                .foregroundStyle(ratingTint(value))
                Text("Кинопоиск")
                    .font(.caption)
                    .foregroundStyle(Theme.textTertiary)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(Capsule().fill(ratingTint(value).opacity(0.14)))
            .accessibilityLabel("Рейтинг Кинопоиска \(String(format: "%.1f", value))")
        }
    }

    private func ratingTint(_ value: Double) -> Color {
        switch value {
        case 7.0...: Theme.accent
        case 5.0..<7.0: Theme.instant
        default: Theme.danger
        }
    }
}

/// Картинка для карточки продолжения просмотра. Приоритет: снятый плеером кадр,
/// кадр серии из каталога, обложка тайтла, постер, градиент.
struct WatchArtwork: View {
    @Environment(AppState.self) private var appState
    let entryKey: String
    let torrentName: String
    let fileName: String
    var height: CGFloat = 96

    var body: some View {
        ZStack {
            if let snapshot = appState.thumbnails.image(for: entryKey) {
                Image(uiImage: snapshot)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
            } else if let url = remoteURL {
                AsyncImage(url: url) { phase in
                    switch phase {
                    case .success(let image):
                        image.resizable().aspectRatio(contentMode: .fill)
                    default:
                        gradient
                    }
                }
            } else {
                gradient
            }
        }
        .frame(height: height)
        .clipped()
        .accessibilityHidden(true)
        // Кадр появляется асинхронно: перерисовываемся, когда плеер его сохранил
        .id("\(entryKey)-\(appState.thumbnails.revision)")
    }

    private var remoteURL: URL? {
        guard let meta = appState.meta.meta(for: torrentName) else { return nil }
        let info = EpisodeInfo.parse(fileName)
        if let number = info.episode,
           let episode = appState.meta.episode(titleID: meta.sourceId, season: info.season, number: number),
           !episode.stillUrl.isEmpty {
            return URL(string: episode.stillUrl)
        }
        if !meta.backdropUrl.isEmpty { return URL(string: meta.backdropUrl) }
        return URL(string: meta.posterUrl)
    }

    private var gradient: some View {
        LinearGradient(
            colors: [Theme.violet.opacity(0.55), Theme.accent.opacity(0.35)],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
    }
}
