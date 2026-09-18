"""Start, stop and inspect the tracking loop.

Replaces the Schedule-based control in ``purifier.schedule``. The loop is now
a single long-running workflow, so starting it is one workflow start and
stopping it is a signal, rather than a Schedule that had to be created,
updated, paused and resumed.

Stopping uses a signal rather than termination on purpose: the workflow
finishes the frame it is on, so the servos are never abandoned mid-ramp.
"""

import argparse
import asyncio
import contextlib
import sys

from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHandle
from temporalio.service import RPCError

from purifier.client import connect
from purifier.shared import (
    MAC_TASK_QUEUE,
    TrackerConfig,
    TrackerState,
    TrackerStatus,
)
from purifier.workflows import TrackPerson

#: Stable workflow ID, so stop and status need no run ID.
TRACKER_WORKFLOW_ID = "track-person-loop"


async def _running_handle(
    client: Client,
) -> WorkflowHandle[TrackPerson, TrackerState] | None:
    """Return a handle to the running loop, if there is one.

    :param client: Connected Temporal client.
    :returns: The handle, or ``None`` if the loop is not running.
    """
    handle = client.get_workflow_handle(TRACKER_WORKFLOW_ID)
    try:
        description = await handle.describe()
    except RPCError:
        return None
    if description.status == WorkflowExecutionStatus.RUNNING:
        return handle
    return None


async def start(client: Client, config: TrackerConfig) -> None:
    """Start the tracking loop, unless it is already running.

    :param client: Connected Temporal client.
    :param config: Loop parameters.
    """
    if await _running_handle(client) is not None:
        print(f"{TRACKER_WORKFLOW_ID!r} is already running; use --status or --stop")
        return

    handle = await client.start_workflow(
        TrackPerson.run,
        args=[config, None],
        id=TRACKER_WORKFLOW_ID,
        task_queue=MAC_TASK_QUEUE,
    )
    budget = (
        "until stopped"
        if config.stop_after_frames is None
        else f"for {config.stop_after_frames} frames"
    )
    print(f"started {TRACKER_WORKFLOW_ID!r} ({budget}), run {handle.result_run_id}")
    print(f"  continue-as-new every {config.frames_per_run} frames")


async def stop(client: Client) -> None:
    """Signal the loop to finish its current frame and exit.

    :param client: Connected Temporal client.
    """
    handle = await _running_handle(client)
    if handle is None:
        print(f"{TRACKER_WORKFLOW_ID!r} is not running")
        return
    await handle.signal(TrackPerson.stop)
    print(f"stop signalled; {TRACKER_WORKFLOW_ID!r} will exit after the current frame")


async def status(client: Client) -> None:
    """Query and print the loop's progress.

    :param client: Connected Temporal client.
    """
    handle = client.get_workflow_handle(TRACKER_WORKFLOW_ID)
    try:
        description = await handle.describe()
    except RPCError:
        print(f"{TRACKER_WORKFLOW_ID!r} has never run")
        return

    print(f"workflow:  {TRACKER_WORKFLOW_ID}")
    print(f"status:    {description.status.name if description.status else 'unknown'}")

    if description.status != WorkflowExecutionStatus.RUNNING:
        return

    # Passing the method reference lets the SDK infer the result type, so no
    # result_type is needed here - unlike the string-dispatched activities.
    snapshot: TrackerStatus = await handle.query(TrackPerson.status)
    state = snapshot.state
    print(f"generation:        {state.generation}")
    print(f"frames processed:  {state.frames_processed}")
    print(f"  moved:           {state.moves_made}")
    print(f"  no person:       {state.frames_without_person}")
    print(f"  failed:          {state.frames_failed}")
    print(f"last action:       {snapshot.last_action}")
    if snapshot.last_note:
        print(f"last note:         {snapshot.last_note}")
    if snapshot.stopping:
        print("stop requested, finishing current frame")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments.

    :param argv: Argument list, defaulting to ``sys.argv[1:]``.
    :returns: Parsed arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--start", action="store_true", help="start the loop")
    mode.add_argument("--stop", action="store_true", help="signal it to exit")
    mode.add_argument("--status", action="store_true", help="query progress")

    parser.add_argument(
        "--frames",
        type=int,
        metavar="N",
        help="with --start, run N frames then exit (default: until stopped)",
    )
    parser.add_argument(
        "--frames-per-run",
        type=int,
        default=TrackerConfig.frames_per_run,
        metavar="N",
        help=(
            "with --start, frames before continue-as-new "
            f"(default {TrackerConfig.frames_per_run}); bounds history size"
        ),
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=TrackerConfig.frame_pause_seconds,
        metavar="SECONDS",
        help="with --start, pause between frames (default 0, run flat out)",
    )
    return parser.parse_args(argv)


async def main() -> None:
    """Entry point."""
    args = parse_args()
    client = await connect()

    try:
        if args.start:
            await start(
                client,
                TrackerConfig(
                    frames_per_run=args.frames_per_run,
                    frame_pause_seconds=args.pause,
                    stop_after_frames=args.frames,
                ),
            )
        elif args.stop:
            await stop(client)
        elif args.status:
            await status(client)
    except RPCError as rpc_error:
        raise SystemExit(f"Temporal rejected the call: {rpc_error}") from rpc_error


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(main()))
