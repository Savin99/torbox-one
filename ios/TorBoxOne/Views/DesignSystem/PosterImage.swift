import SwiftUI

/// Постер каталога: правильная пропорция 2:3, мерцающая заглушка, аккуратный
/// фолбэк вместо серого прямоугольника, когда постера нет.
struct PosterImage: View {
    let url: String
    /// Подпись для фолбэка — первая буква названия.
    var fallbackTitle: String = ""
    var radius: CGFloat = Theme.radiusM

    var body: some View {
        AsyncImage(url: URL(string: url), transaction: Transaction(animation: .easeOut(duration: 0.25))) { phase in
            switch phase {
            case .success(let image):
                image
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .transition(.opacity)
            case .failure:
                fallback
            case .empty:
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .fill(Theme.hairline.opacity(0.09))
                    .shimmering()
            @unknown default:
                fallback
            }
        }
        .aspectRatio(2.0 / 3.0, contentMode: .fill)
        .frame(maxWidth: .infinity)
        .clipShape(RoundedRectangle(cornerRadius: radius, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: radius, style: .continuous)
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
            VStack(spacing: 8) {
                Image(systemName: "film.stack")
                    .font(.system(size: 30, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.75))
                if !fallbackTitle.isEmpty {
                    Text(fallbackTitle)
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.white.opacity(0.85))
                        .multilineTextAlignment(.center)
                        .lineLimit(3)
                        .padding(.horizontal, 10)
                }
            }
        }
    }
}

/// Обложка для файла раздачи: у трекеров постеров нет, поэтому рисуем
/// узнаваемую «плёнку» с акцентом по типу контента.
struct FileArtwork: View {
    let title: String
    var isVideo = true
    var side: CGFloat = 56

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: Theme.radiusS, style: .continuous)
                .fill(
                    LinearGradient(
                        colors: [Theme.info.opacity(0.35), Theme.violet.opacity(0.30)],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )
                )
            Image(systemName: isVideo ? "film.stack" : "doc")
                .font(.system(size: side * 0.34, weight: .semibold))
                .foregroundStyle(.white.opacity(0.9))
        }
        .frame(width: side, height: side)
        .accessibilityHidden(true)
    }
}

extension CatalogItem {
    /// Каталог отдаёт превью (kp_small); для крупной обложки берём полноразмерный файл.
    var posterLargeURL: String {
        posterUrl.replacingOccurrences(of: "/kp_small/", with: "/kp/")
    }

    var subtitleLine: String {
        var parts: [String] = []
        if !year.isEmpty, year != "null" { parts.append(year) }
        if !genres.isEmpty { parts.append(genres.prefix(2).joined(separator: ", ")) }
        if !countries.isEmpty { parts.append(countries[0]) }
        return parts.joined(separator: " · ")
    }

    var ratingValue: Double? {
        guard !rating.isEmpty, rating != "null" else { return nil }
        return Double(rating.replacingOccurrences(of: ",", with: "."))
    }

    var isSeries: Bool {
        mediaType.uppercased().contains("SERIAL") || mediaType.uppercased().contains("TV")
    }
}
