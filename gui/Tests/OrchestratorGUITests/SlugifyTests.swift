import XCTest
@testable import OrchestratorGUI

// Coverage for OrchestratorStore.slugify — the single slug function both the
// store and the New App intake now share (they used to diverge on how they
// handled runs of non-alphanumeric characters).
final class SlugifyTests: XCTestCase {

    func testLowercasesAndHyphenatesSpaces() {
        XCTAssertEqual(OrchestratorStore.slugify("My Cool App"), "my-cool-app")
    }

    func testCollapsesConsecutiveSeparators() {
        // The old intake version left double hyphens; the shared one collapses
        // any run of non-alphanumerics into a single hyphen (non-ASCII letters
        // are dropped and act as separators).
        XCTAssertEqual(OrchestratorStore.slugify("Åpp   Ünïq"), "pp-n-q")
        XCTAssertEqual(OrchestratorStore.slugify("a__b--c  d"), "a-b-c-d")
    }

    func testNoLeadingOrTrailingHyphen() {
        XCTAssertEqual(OrchestratorStore.slugify("  Hello!  "), "hello")
        XCTAssertEqual(OrchestratorStore.slugify("---weird---"), "weird")
    }

    func testDigitsKept() {
        XCTAssertEqual(OrchestratorStore.slugify("Route 66 Planner"), "route-66-planner")
    }

    func testEmptyFallsBackToNewChat() {
        // An empty/all-punctuation input yields the store's documented fallback.
        XCTAssertEqual(OrchestratorStore.slugify(""), "new-chat")
        XCTAssertEqual(OrchestratorStore.slugify("!!!"), "new-chat")
    }

    func testCappedLength() {
        XCTAssertLessThanOrEqual(
            OrchestratorStore.slugify(String(repeating: "word ", count: 40)).count, 40)
    }

    func testIntakeSlugifyDelegatesToStore() {
        // NewAppIntakeSheet.slugify must now produce identical output.
        XCTAssertEqual(NewAppIntakeSheet.slugify("Åpp   Ünïq"),
                       OrchestratorStore.slugify("Åpp   Ünïq"))
    }
}
