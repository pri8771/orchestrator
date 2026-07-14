<!-- keywords: product requirements, prd, scope, mvp, user story, acceptance criteria, success metrics, non-functional requirements, edge cases, error states, empty state, loading state, accessibility, performance budget, quality bar, definition of done, prioritization, must have, nice to have, assumptions, risks, trade-offs, user experience, onboarding, feedback, validation, release readiness, launch checklist, rollback, observability, analytics -->

# Product Definition & Quality Bar — Build-Agent Cheatsheet

For any project, before and around the build: how to pin down *what to build* and *when it's actually done*. Applies to CLIs, tools, services, and apps alike.

## Define the thing before building it

- **State the one-sentence job.** Who is this for, and what job does it do for them? If you can't say it in a sentence, the scope is unclear.
- **Separate must-have from nice-to-have explicitly.** A prototype/MVP ships the smallest set that delivers the core value; everything else is a labeled backlog, not silent scope creep.
- **Write acceptance criteria as observable behavior.** "User can X and sees Y" — testable, not "the app should be good". Each criterion is something you can demo or assert.
- **Name assumptions and risks.** What are you unsure about? What breaks the plan if wrong? Surface these early; they drive what to validate first.

## Cover the states, not just the happy path

Real quality lives in the states people forget:

- **Empty** (no data yet — first run, no results): show guidance, not a blank void.
- **Loading** (work in progress): show progress or at least that something is happening; never a frozen UI.
- **Error** (something failed): say what failed and what the user can do; never a silent no-op or a raw stack trace.
- **Edge inputs:** very long, empty, zero, negative, duplicate, unicode, offline. Decide behavior deliberately.

## Non-functional requirements are requirements

- **Performance:** set a rough budget (startup time, response time, memory). "Fast enough" is measurable — pick a number.
- **Accessibility & clarity:** legible output, keyboard/screen-reader reachable where relevant, no meaning conveyed by color alone.
- **Security & privacy:** validate untrusted input, don't log secrets, least privilege, and be explicit about what data leaves the machine.
- **Observability:** enough logging/events to diagnose a failure after the fact without a debugger.

## Definition of done

A change is done when:

1. It meets the acceptance criteria (demoed or asserted), including the error/empty states.
2. It has tests for the core behavior and the notable edges.
3. It builds/verifies cleanly and you've run it end-to-end once.
4. Docs/help reflect the new behavior.
5. There's a way to tell it's working in production (a log line, a metric, a health check) — and, for risky changes, a way to roll back.

## Release readiness

- Walk the primary user path start to finish as a new user would.
- Confirm the failure path is graceful (kill a dependency, feed bad input).
- Know your rollback: what do you do if this is broken in the wild?
- Ship the smallest honest version; iterate from real feedback rather than guessing further in the dark.
