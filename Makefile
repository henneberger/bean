# bean — dev + release tasks. `make help` lists them.
PY := .venv/bin/python

.PHONY: help venv test check build version release clean

help:
	@echo "bean make targets:"
	@echo "  make venv                 bootstrap .venv + install bean (editable)"
	@echo "  make test                 run the offline test suite"
	@echo "  make check                version-sync + tests + byte-compile (pre-release gate)"
	@echo "  make build                build wheel + sdist into dist/"
	@echo "  make version              print the current version"
	@echo "  make release VERSION=x.y.z [YES=1]   version -> check -> build -> commit + tag"
	@echo "  make clean                remove build artifacts"

venv:
	python3 scripts/bean.py status >/dev/null 2>&1 || python3 scripts/bean.py --help >/dev/null

test:
	$(PY) tests/test_bean.py

check:
	$(PY) dev/release.py check

build:
	$(PY) -m pip install --quiet build >/dev/null 2>&1 || true
	$(PY) dev/release.py build

version:
	$(PY) dev/release.py version

# make release VERSION=0.2.0        # dry run — prints the plan
# make release VERSION=0.2.0 YES=1  # actually commit + tag
release:
	@test -n "$(VERSION)" || (echo "usage: make release VERSION=x.y.z [YES=1]" && exit 2)
	$(PY) dev/release.py cut $(VERSION) $(if $(YES),--yes,)

clean:
	rm -rf dist build *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
