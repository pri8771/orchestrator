# Canonical build/verify gate for the orchestrator.
#
# The engine is stdlib-only Python (runs anywhere with python3). The GUI is a
# SwiftPM package and only builds on macOS with the Swift toolchain, so the
# gui-*/app/dmg/verify targets are macOS-only; test/test-strict/doctor run
# everywhere and are what CI uses on Linux.

PYTHON ?= python3

.PHONY: help test test-strict doctor lint typecheck gui-build gui-test app dmg verify clean

help:
	@echo "Engine (any platform):"
	@echo "  make test         run the engine unittest suite"
	@echo "  make test-strict  run the suite with warnings promoted to errors"
	@echo "  make doctor       environment preflight"
	@echo "  make lint         ruff check (config: pyproject.toml)"
	@echo "  make typecheck    mypy (config: pyproject.toml)"
	@echo "GUI (macOS only):"
	@echo "  make gui-build    build the SwiftUI app"
	@echo "  make gui-test     run the GUI unit tests"
	@echo "  make app          package gui/dist/Orchestrator.app"
	@echo "  make dmg          package gui/dist/Orchestrator.dmg"
	@echo "  make verify       test-strict + gui-build + gui-test + doctor"

test:
	$(PYTHON) -m unittest discover -s tests

test-strict:
	$(PYTHON) -W error::ResourceWarning -m unittest discover -s tests

doctor:
	$(PYTHON) orchestrator.py --doctor

lint:
	ruff check .

typecheck:
	mypy . --config-file pyproject.toml

gui-build:
	cd gui && swift build -c release

gui-test:
	cd gui && swift test

app:
	bash gui/build_app.sh

dmg:
	bash gui/make_dmg.sh

# The full local gate (macOS). CI (.github/workflows/ci.yml) runs the Python
# suite directly (not through `make`) across 3.9/3.11/3.12 on Linux, advisory
# ruff, and builds+tests the GUI on macOS — see that file for the exact steps.
verify: test-strict gui-build gui-test doctor
	@echo "verify: all gates passed"

clean:
	rm -rf gui/.build gui/dist
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
