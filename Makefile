.PHONY: test setup
test:
	python scripts/test.py
setup:
	git config core.hooksPath .githooks
