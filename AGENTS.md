# Repository Guidelines

## Project Structure & Module Organization

The maintained application is under `container-profiler/`. `main.py` launches the PyQt6 GUI; `src/profiler/core/` contains Docker, power, and data collection logic; `src/profiler/gui/` contains widgets and worker-thread orchestration; and `src/profiler/utils/` handles portable paths. Runtime styles and icons belong in `resources/`, tests in `tests/`, and Docker-related files in `docker/`. Project decisions and progress are recorded in `文檔/`, especially `LOG.md` and `DEVELOPMENT_GUIDE.md`. Treat root-level `build/`, `dist/`, and executables as generated artifacts. The separate `skills/` checkout is reference material, not application code.

## Build, Test, and Development Commands

Run from `JNUFINAL/container-profiler`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
python tests/test_docker_core.py
pyinstaller build.spec
```

The smoke test requires Docker Desktop. Full power metrics additionally require HWiNFO shared memory and, for NVIDIA data, a working NVML driver. PyInstaller writes the Windows executable to `dist/`.

## Coding Style & Naming Conventions

Follow PEP 8 with four spaces, type hints for public interfaces, `PascalCase` classes, `snake_case` functions, and leading underscores for internal Qt callbacks. Keep hardware access in `core/`; GUI modules should consume structured stats rather than query Docker or sensors directly. Resolve resources through `profiler.utils.paths` so source and frozen builds behave consistently. Optional hardware dependencies must fail gracefully. Update `文檔/LOG.md` after material code, test, or packaging changes.

## Testing Guidelines

The current `test_docker_core.py` is an integration smoke script rather than an isolated unit suite, and no coverage threshold is configured. Name new tests `test_<component>.py`. Mock Docker, HWiNFO, and NVML for deterministic unit tests; reserve live-hardware checks for clearly labeled integration tests. Verify GUI changes manually for startup, container selection, monitoring start/stop, chart updates, CSV export, and clean shutdown.

## Commit & Pull Request Guidelines

The application root has no Git history; do not derive conventions from the unrelated `skills/` repository. Use Conventional Commits such as `fix: stop worker thread on exit`. Pull requests should summarize user-visible behavior, list hardware/software prerequisites and tests, link issues, include GUI screenshots, and note packaging impact.

## Security & Generated Files

Never commit local sensor dumps, exported monitoring data, machine-specific paths, virtual environments, or rebuilt `build/` and `dist/` contents unless a release explicitly requires binaries.
