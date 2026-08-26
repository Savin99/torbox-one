import UIKit

/// Единственная задача — отдавать системе актуальную маску ориентаций:
/// приложение живёт в портрете, плеер разрешает ландшафт.
final class AppDelegate: NSObject, UIApplicationDelegate {
    static var orientationMask: UIInterfaceOrientationMask = .portrait

    func application(
        _ application: UIApplication,
        supportedInterfaceOrientationsFor window: UIWindow?
    ) -> UIInterfaceOrientationMask {
        Self.orientationMask
    }
}
