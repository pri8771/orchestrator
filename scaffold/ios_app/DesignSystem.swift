import SwiftUI

// Golden-scaffold DesignSystem (task #4). Every build starts from this file
// instead of a blank folder: the design_handoff phase's token spec REPLACES
// the placeholder values below (colors, type ramp, spacing, radii), and every
// view styles itself ONLY through these tokens — the design-lint gate flags
// inline Color(red:)/.font(.system(size:)) usage outside this file.
enum DS {

    // MARK: Palette — replace with the handoff's exact light/dark hexes.
    enum Palette {
        static let accent = Color.accentColor
        static let background = Color(.systemBackground)
        static let surface = Color(.secondarySystemBackground)
        static let textPrimary = Color.primary
        static let textSecondary = Color.secondary
        static let success = Color.green
        static let warning = Color.orange
        static let danger = Color.red
    }

    // MARK: Type ramp — replace with the handoff's roles/sizes/weights.
    enum Font {
        static let largeTitle = SwiftUI.Font.largeTitle.weight(.bold)
        static let title = SwiftUI.Font.title2.weight(.semibold)
        static let headline = SwiftUI.Font.headline
        static let body = SwiftUI.Font.body
        static let caption = SwiftUI.Font.caption
    }

    // MARK: Spacing scale (pt) — 8pt grid by default.
    enum Space {
        static let xs: CGFloat = 4
        static let s: CGFloat = 8
        static let m: CGFloat = 16
        static let l: CGFloat = 24
        static let xl: CGFloat = 32
    }

    // MARK: Corner radii.
    enum Radius {
        static let card: CGFloat = 12
        static let chip: CGFloat = 8
    }
}
