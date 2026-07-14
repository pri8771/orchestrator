import SwiftUI

// Empty host app — required scaffolding for the UI-test bundle. The tests
// never launch this; they drive the TARGET app by bundle id.
@main
struct CrawlerHostApp: App {
    var body: some Scene {
        WindowGroup {
            Text("Crawler host")
        }
    }
}
