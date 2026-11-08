# Contributing

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest -n auto --cov
```

The probes are tested against real servers on loopback (see `tests/conftest.py`), not mocks; keep it that way where
possible. Changes to how outages or availability are computed need a case in `tests/test_analysis.py` and, if the
definition changes, an update to the list at the top of `src/ispq/analysis.py`.

Claims in the docs are checked against `docs/report.json` by `tests/test_docs.py`. Re-run `python tools/evaluate.py`
and update the text when the numbers change.
