# Orchestrator V2 — local verification gates.
# This repo has no git remote, so there is no hosted CI; `make verify` is the
# canonical gate. Run it before shipping anything (docs reference this target
# instead of hardcoding test counts).

ENGINE := orchestrator-v2-source

.PHONY: verify test test-strict gui-build gui-test doctor seed run app dmg

verify: test-strict gui-build gui-test doctor

test:
	cd $(ENGINE) && python3 -m unittest discover -s tests -v

# Warnings-as-errors: unclosed file handles etc. fail the run.
test-strict:
	cd $(ENGINE) && python3 -W error::ResourceWarning -m unittest discover -s tests

gui-build:
	cd $(ENGINE)/gui && swift build -c release

gui-test:
	cd $(ENGINE)/gui && swift test

doctor:
	cd $(ENGINE) && python3 orchestrator.py --doctor --json > /dev/null && \
	  python3 orchestrator.py --doctor

# Fresh-clone conveniences: seed a demo project into ./workspace, launch the GUI.
seed:
	cd $(ENGINE) && python3 seed_demo.py

run:
	bash run-orchestrator.sh

# Release artifacts — rebuild BEFORE distributing; never ship a dist/ that
# predates the current engine source.
app:
	cd $(ENGINE)/gui && bash build_app.sh

dmg: app
	cd $(ENGINE)/gui && bash make_dmg.sh
