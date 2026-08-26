import SwiftUI
import UIKit

/// Поверхность отрисовки VLC: чёрный UIView, отданный плееру как drawable.
struct VLCVideoSurface: UIViewRepresentable {
    let vm: PlayerViewModel

    func makeUIView(context: Context) -> UIView {
        let view = UIView()
        view.backgroundColor = .black
        vm.start(drawable: view)
        return view
    }

    func updateUIView(_ uiView: UIView, context: Context) {}
}
