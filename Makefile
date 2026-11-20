.PHONY: run test clean

run:
	./run.sh --help

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .coverage build dist
	rm -f *.svg *.d2
