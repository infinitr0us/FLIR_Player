# Contributing to FLIR Thermal Player

Thanks for your interest in improving the FLIR Thermal Player! This is a
research-oriented open-source project and contributions of all kinds — bug
reports, feature ideas, documentation, and code — are welcome.

## Before you start

This project is a front end for the **FLIR File SDK** (the `fnv` / `FileSDK`
package). The SDK is proprietary to FLIR/Teledyne and export-controlled (EAR99),
so it is **not** distributed here. To run the application or the SDK-dependent
tests you must obtain the SDK from FLIR and install it yourself. See the
[Prerequisites](README.md#prerequisites-the-flir-file-sdk) section of the README.

**Please never commit any of the following** (they are covered by
`.gitignore`, but double-check before pushing):

- FLIR File SDK wheels or native binaries (`*.whl`, `fnv/`, DLLs)
- Thermal recordings (`*.ats`, `*.seq`, `*.csq`, `*.sfmov`, `*.fff`, `*.ptw`)
- The FLIR ResearchIR manual or any other FLIR-copyrighted document
- Build output (`build/`, `release/`, `*.exe`)

## Development setup

```powershell
# 1. Install the FLIR File SDK you obtained from FLIR, e.g.:
pip install FileSDK-5.0.1-cp311-cp311-win_amd64.whl

# 2. Install the project in editable mode with dev extras:
pip install -e ".[dev]"

# 3. Run the app:
python flir_player_app.py
```

## Running the tests

```powershell
python -m pytest -q
```

The suite is data-aware: tests that need the FLIR File SDK or a real recording
(`1.ats` / `2.seq` in the repository root) are **skipped automatically** when
those are absent, so `pytest` runs cleanly on a bare clone. To exercise the full
suite, install the SDK and place your own recordings at those two paths.

## Pull requests

1. Fork the repository and create a topic branch off `main`.
2. Keep changes focused; match the surrounding code style (the codebase uses
   `from __future__ import annotations`, type hints, and small modules).
3. Add or update tests for behavior changes. Prefer tests that work on synthetic
   NumPy data (no SDK required) where possible, so CI can run them.
4. Make sure `python -m pytest -q` passes locally.
5. Open a pull request describing the change and the motivation. Continuous
   integration will run the SDK-independent portion of the suite automatically.

## Reporting bugs

Please use the issue templates. Include your OS, Python version, the FLIR File
SDK version, the recording format involved (SEQ/ATS/CSQ/…), and steps to
reproduce. Do **not** attach proprietary recordings or SDK binaries to issues.

## License

By contributing, you agree that your contributions will be licensed under the
**GNU General Public License v3.0 or later**, the same license as the project
(see [LICENSE](LICENSE)).
