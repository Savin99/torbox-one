import SwiftUI

/// Мерцающая заглушка вместо спиннера: экран сразу показывает свою структуру.
struct Shimmer: ViewModifier {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var phase: CGFloat = -1

    func body(content: Content) -> some View {
        content
            .overlay {
                if !reduceMotion {
                    GeometryReader { geo in
                        LinearGradient(
                            colors: [.clear, Theme.hairline.opacity(0.10), .clear],
                            startPoint: .leading,
                            endPoint: .trailing
                        )
                        .frame(width: geo.size.width * 0.6)
                        .offset(x: phase * geo.size.width * 1.6)
                    }
                    .allowsHitTesting(false)
                }
            }
            .onAppear {
                guard !reduceMotion else { return }
                withAnimation(.linear(duration: 1.3).repeatForever(autoreverses: false)) {
                    phase = 1
                }
            }
    }
}

extension View {
    func shimmering() -> some View { modifier(Shimmer()) }
}

/// Прямоугольник-заглушка нужного размера.
struct SkeletonBlock: View {
    var width: CGFloat?
    var height: CGFloat = 14
    var radius: CGFloat = 6

    var body: some View {
        RoundedRectangle(cornerRadius: radius, style: .continuous)
            .fill(Theme.hairline.opacity(0.09))
            .frame(width: width, height: height)
            .clipShape(RoundedRectangle(cornerRadius: radius, style: .continuous))
            .shimmering()
    }
}

/// Сетка постеров-заглушек для каталога.
struct PosterGridSkeleton: View {
    var count = 6
    private let columns = [GridItem(.flexible(), spacing: 14), GridItem(.flexible(), spacing: 14)]

    var body: some View {
        LazyVGrid(columns: columns, spacing: 18) {
            ForEach(0..<count, id: \.self) { _ in
                VStack(alignment: .leading, spacing: 8) {
                    RoundedRectangle(cornerRadius: Theme.radiusM, style: .continuous)
                        .fill(Theme.hairline.opacity(0.09))
                        .aspectRatio(2.0 / 3.0, contentMode: .fit)
                        .shimmering()
                    SkeletonBlock(height: 12)
                    SkeletonBlock(width: 70, height: 10)
                }
            }
        }
        .padding(.horizontal, Theme.gutter)
        .accessibilityHidden(true)
    }
}

/// Заглушки строк релизов / загрузок.
struct RowsSkeleton: View {
    var count = 5

    var body: some View {
        VStack(spacing: 12) {
            ForEach(0..<count, id: \.self) { _ in
                VStack(alignment: .leading, spacing: 10) {
                    SkeletonBlock(height: 15)
                    SkeletonBlock(width: 190, height: 12)
                    HStack(spacing: 6) {
                        SkeletonBlock(width: 58, height: 18, radius: 9)
                        SkeletonBlock(width: 44, height: 18, radius: 9)
                        SkeletonBlock(width: 72, height: 18, radius: 9)
                    }
                }
                .padding(14)
                .cardSurface()
            }
        }
        .padding(.horizontal, Theme.gutter)
        .accessibilityHidden(true)
    }
}
