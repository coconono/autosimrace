# Specsheet: Setup makeTrack Project

## Overview

Set up a Python pygame project scaffold for `track_sim` with a reproducible local development flow.

## Objective

Create a baseline project setup that can be started with one command and is safe to re-run.

## Source Prompt

- pygame project
- keep venv in the track_sim directory
- maintain a readme.md
- include a requirements.txt for dependencies
- make a run.sh to start the project in the bin folder, it sets up the virtual environment, checks dependencies, runs the main script and deactivates the virtual environment afterward.

## Scope

### In scope

- Python project initialization for pygame under `tools/track_sim`
- Local virtual environment stored inside `tools/track_sim`
- Dependency manifest via `requirements.txt`
- Developer documentation in `README.md`
- Launcher script `tools/track_sim/bin/run.sh` that:
  - Creates virtual environment if missing
  - Activates virtual environment
  - Installs missing dependencies (or installs from `requirements.txt` when needed)
  - Runs the main script
  - Deactivates virtual environment on completion

### Out of scope

- Full game mechanics or track generation features
- Packaging/distribution beyond local execution
- CI/CD or containerization

## File and Folder Requirements

- Keep virtual environment inside `tools/track_sim` (example: `.venv/`)
- Maintain a top-level project README at `tools/track_sim/README.md`
- Maintain dependency list at `tools/track_sim/requirements.txt`
- Add launcher at `tools/track_sim/bin/run.sh`
- Ensure launcher has executable permissions

## Functional Requirements

1. `run.sh` must be idempotent and safe to run repeatedly.
2. If virtual environment does not exist, `run.sh` must create it.
3. `run.sh` must activate the virtual environment before dependency checks and script execution.
4. `run.sh` must ensure pygame is installed from `requirements.txt`.
5. `run.sh` must execute the project entry script (define path in README).
6. `run.sh` must deactivate the virtual environment before exiting.
7. `run.sh` should return a non-zero exit code if setup or execution fails.

## Non-Functional Requirements

- Script compatibility: macOS/Linux shell environment
- Clear terminal output for each setup step
- Minimal manual setup for first run

## Suggested Defaults

- Virtual environment path: `tools/track_sim/.venv`
- Entry script path: `tools/track_sim/main.py`
- Dependency pin in `requirements.txt` includes pygame (version may be pinned)

## Acceptance Criteria

1. Running `tools/track_sim/bin/run.sh` on a clean checkout creates `.venv` and starts the app.
2. Running the script a second time does not recreate environment unnecessarily and still starts the app.
3. `requirements.txt` exists and includes pygame.
4. `README.md` documents prerequisites, quick start, and troubleshooting basics.
5. On exit, the shell is no longer inside an activated virtual environment.

## Validation Checklist

- Verify `.venv` is created under `tools/track_sim`
- Verify `pip install -r requirements.txt` succeeds
- Verify app launch command executes without path errors
- Verify deactivation step runs on normal exit and failure paths
- Verify script is executable (`chmod +x tools/track_sim/bin/run.sh`)

## Risks and Notes

- If system Python lacks `venv`, setup may fail and should print remediation guidance.
- pygame may require SDL/system libraries on some machines.
- Dependency resolution should be explicit to avoid drift across environments.
