# Air purifier person-tracking camera.
# Run `just` for the list of targets.

pi_host := "airpurifier"
pi_dir := "/home/shy/purifier"
service := "purifier-worker"

default:
    @just --list

# --- MacBook -----------------------------------------------------------------

# Install Mac dependencies, including torch and ultralytics.
sync:
    uv sync --extra mac

# Run the MacBook worker: hosts the workflow and pose detection.
worker-mac:
    uv run python -m purifier.worker_mac

# Lint and type-check.
check:
    uv run ruff check src
    uv run ruff format --check src
    uv run mypy src

fix:
    uv run ruff check --fix src
    uv run ruff format src

# --- Tracking loop -----------------------------------------------------------

# Start the loop: one long-running parent, one child workflow per frame.
start:
    uv run python -m purifier.tracker --start

# Signal it to finish the current frame and exit. Never terminate it - that
# can abandon a frame mid-ramp with the servos still moving.
stop:
    uv run python -m purifier.tracker --stop

status:
    uv run python -m purifier.tracker --status

# Run a fixed number of frames then exit. `just frames 1` is the quickest
# end-to-end check.
frames n:
    uv run python -m purifier.tracker --start --frames {{n}}

# --- Schedule (superseded) ---------------------------------------------------
# The loop used to be a Schedule firing one workflow per frame. Kept so the
# leftover paused Schedule in Cloud can still be inspected or deleted.

schedule-status:
    uv run python -m purifier.schedule --status

schedule-delete:
    uv run python -m purifier.schedule --delete

# --- Raspberry Pi ------------------------------------------------------------

# One-time Pi setup: install uv, create the service directory.
pi-bootstrap:
    ssh {{pi_host}} 'curl -LsSf https://astral.sh/uv/install.sh | sh'
    ssh {{pi_host}} 'mkdir -p {{pi_dir}}'
    @echo "Now run: just pi-deploy && just pi-install-service"

# Push code and credentials to the Pi, then sync its dependencies.
pi-deploy:
    ssh {{pi_host}} 'mkdir -p {{pi_dir}}/src'
    rsync -az --delete \
        --exclude '__pycache__' \
        src/ {{pi_host}}:{{pi_dir}}/src/
    rsync -az pyproject.toml .python-version .env {{pi_host}}:{{pi_dir}}/
    rsync -az camera_test.py {{pi_host}}:{{pi_dir}}/
    ssh {{pi_host}} 'cd {{pi_dir}} && ~/.local/bin/uv sync --extra pi'

# Install and enable the systemd unit. Needs sudo on the Pi.
pi-install-service:
    rsync -az deploy/{{service}}.service {{pi_host}}:/tmp/
    ssh -t {{pi_host}} 'sudo mv /tmp/{{service}}.service /etc/systemd/system/ && \
        sudo systemctl daemon-reload && \
        sudo systemctl enable --now {{service}}'
    @echo "Logs: just pi-logs"

# Push code and restart the worker. The everyday deploy loop.
pi-restart: pi-deploy
    ssh {{pi_host}} 'sudo systemctl restart {{service}}'
    @echo "Logs: just pi-logs"

pi-logs:
    ssh -t {{pi_host}} 'journalctl -fu {{service}} -n 50'

pi-service-status:
    ssh {{pi_host}} 'systemctl status {{service}} --no-pager'

# Run the Pi worker in the foreground, for tracebacks while tuning.
pi-worker-fg: pi-deploy
    ssh -t {{pi_host}} 'cd {{pi_dir}} && ~/.local/bin/uv run python -m purifier.worker_pi'

# --- Hardware diagnostics ----------------------------------------------------

# Capture a still on the Pi and copy it back to ./captures/.
pi-capture:
    ssh {{pi_host}} 'cd {{pi_dir}} && python3 camera_test.py --output /tmp/just-capture.jpg'
    mkdir -p captures
    scp {{pi_host}}:/tmp/just-capture.jpg captures/
    @echo "captures/just-capture.jpg"

# Report servo angles as read back off the PCA9685 registers.
pi-servo-read:
    ssh {{pi_host}} 'cd {{pi_dir}} && ~/.local/bin/uv run python -m purifier.servo_tool --read'

# Relative servo nudge, for direction and range calibration. e.g. just pi-servo-nudge 10 0
pi-servo-nudge pan tilt:
    ssh {{pi_host}} 'cd {{pi_dir}} && ~/.local/bin/uv run python -m purifier.servo_tool --pan {{pan}} --tilt {{tilt}}'

# Centre both axes.
pi-servo-centre:
    ssh {{pi_host}} 'cd {{pi_dir}} && ~/.local/bin/uv run python -m purifier.servo_tool --centre'

# Release both channels so the servos go limp.
pi-servo-release:
    ssh {{pi_host}} 'cd {{pi_dir}} && ~/.local/bin/uv run python -m purifier.servo_tool --release'
