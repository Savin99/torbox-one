import SwiftUI

/// Сетка постеров каталога: две колонки, правильная пропорция 2:3.
struct CatalogGrid: View {
    @Environment(\.dynamicTypeSize) private var typeSize
    let items: [CatalogItem]
    let onSelect: (CatalogItem) -> Void

    /// На accessibility-размерах шрифта две колонки перестают читаться
    private var columns: [GridItem] {
        typeSize >= .accessibility1
            ? [GridItem(.flexible())]
            : [GridItem(.flexible(), spacing: 14), GridItem(.flexible(), spacing: 14)]
    }

    var body: some View {
        LazyVGrid(columns: columns, spacing: 18) {
            ForEach(items) { item in
                Button {
                    Haptics.tap()
                    onSelect(item)
                } label: {
                    CatalogCard(item: item)
                }
                .buttonStyle(PosterPressStyle())
                .contextMenu {
                    Button("Искать раздачи", systemImage: "magnifyingglass") {
                        onSelect(item)
                    }
                    Button("Скопировать название", systemImage: "doc.on.doc") {
                        UIPasteboard.general.string = item.searchQuery
                        Haptics.tap()
                    }
                }
            }
        }
        .padding(.horizontal, Theme.gutter)
    }
}

/// Нажатие на постер: лёгкое «утопление» карточки.
private struct PosterPressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.96 : 1)
            .animation(.spring(response: 0.3, dampingFraction: 0.75), value: configuration.isPressed)
    }
}

struct CatalogCard: View {
    let item: CatalogItem

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            PosterImage(url: item.posterUrl, fallbackTitle: item.name)
                .overlay(alignment: .topLeading) {
                    if item.isSeries {
                        Text("сериал")
                            .font(.caption2.weight(.bold))
                            .padding(.horizontal, 7)
                            .padding(.vertical, 3)
                            .background(Capsule().fill(.black.opacity(0.55)))
                            .foregroundStyle(.white)
                            .padding(8)
                    }
                }
                .overlay(alignment: .bottomTrailing) {
                    if let rating = item.ratingValue {
                        RatingBadge(value: rating)
                            .padding(8)
                    }
                }
            VStack(alignment: .leading, spacing: 2) {
                Text(item.name)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                if !item.subtitleLine.isEmpty {
                    Text(item.subtitleLine)
                        .font(.caption)
                        .foregroundStyle(Theme.textSecondary)
                        .lineLimit(1)
                }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText)
        .accessibilityAddTraits(.isButton)
    }

    private var accessibilityText: String {
        var parts = [item.name]
        if !item.subtitleLine.isEmpty { parts.append(item.subtitleLine) }
        if let rating = item.ratingValue { parts.append("рейтинг \(String(format: "%.1f", rating))") }
        return parts.joined(separator: ", ")
    }
}

/// Рейтинг Кинопоиска: цвет подсказывает, стоит ли смотреть.
struct RatingBadge: View {
    let value: Double

    var body: some View {
        Text(String(format: "%.1f", value))
            .font(.caption.weight(.heavy))
            .monospacedDigit()
            .padding(.horizontal, 7)
            .padding(.vertical, 3.5)
            .background(Capsule().fill(tint))
            .foregroundStyle(.white)
            .shadow(color: .black.opacity(0.25), radius: 4, y: 2)
    }

    private var tint: Color {
        switch value {
        case 7.5...: Theme.accent
        case 5.5..<7.5: Theme.instant
        default: Theme.danger
        }
    }
}
