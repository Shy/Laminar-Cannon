"""Create, inspect and control the tracking Schedule.

The Schedule is what makes this loop run: one ``TrackPerson`` execution per
tick, each independent. Two settings here are load-bearing:

* ``overlap=SKIP`` - a tick is dropped if the previous frame is still running,
  so the loop can never back up. ``ALLOW_ALL`` would be a bug, not merely
  inefficient: two concurrent runs would each read the same starting angles
  and both apply a delta, double-moving the servos.
* ``execution_timeout`` - a hard ceiling on one frame. With SKIP, a run that
  hung would make every later tick skip and the loop would stop silently.
  This guarantees that cannot happen.
"""

import argparse
import asyncio
import contextlib
import sys
from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.service import RPCError

from purifier.client import connect
from purifier.shared import (
    MAC_TASK_QUEUE,
    SCHEDULE_ID,
    WORKFLOW_ID_PREFIX,
)

#: How often to attempt a frame. Measured pipeline is ~1.7 s without a servo
#: move and ~3 s with one, so a tick longer than that wastes the difference
#: waiting for the next boundary: a 5 s tick on a 3 s pipeline idled for 2 s
#: every cycle. A 2 s tick with BUFFER_ONE keeps a run queued so frames go
#: back to back at whatever rate the pipeline actually sustains.
TICK_INTERVAL = timedelta(seconds=2)

#: Hard ceiling on a single frame. Must stay below a couple of ticks so a bad
#: frame cannot stall the loop for long.
EXECUTION_TIMEOUT = timedelta(seconds=30)


def build_schedule() -> Schedule:
    """Describe the tracking Schedule.

    :returns: The Schedule definition.
    """
    return Schedule(
        action=ScheduleActionStartWorkflow(
            "TrackPerson",
            id=WORKFLOW_ID_PREFIX,
            task_queue=MAC_TASK_QUEUE,
            execution_timeout=EXECUTION_TIMEOUT,
        ),
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=TICK_INTERVAL)],
        ),
        # BUFFER_ONE, not SKIP: keep exactly one run queued so the next frame
        # starts the moment the current one finishes, rather than waiting for
        # a tick boundary. Still never concurrent, which matters - two
        # overlapping runs would both read the same start angles and each
        # apply a delta, double-moving the servos. ALLOW_ALL remains a bug.
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.BUFFER_ONE),
        state=ScheduleState(
            note="Person-tracking camera loop",
            paused=True,
        ),
    )


async def create(client: Client, start_paused: bool) -> None:
    """Create the Schedule, or report that it already exists.

    Created paused by default, so hardware is never commanded the instant the
    Schedule lands. Unpause explicitly once both workers are confirmed up.

    :param client: Connected Temporal client.
    :param start_paused: Whether to leave the Schedule paused.
    """
    definition = build_schedule()
    definition.state.paused = start_paused
    try:
        await client.create_schedule(SCHEDULE_ID, definition)
    except ScheduleAlreadyRunningError:
        print(f"schedule {SCHEDULE_ID!r} already exists; use --update")
        return
    state = "paused" if start_paused else "running"
    print(f"created schedule {SCHEDULE_ID!r} ({state}), tick {TICK_INTERVAL}")


async def update(client: Client) -> None:
    """Replace the existing Schedule's definition with the current code.

    :param client: Connected Temporal client.
    """
    handle = client.get_schedule_handle(SCHEDULE_ID)
    description = await handle.describe()
    was_paused = description.schedule.state.paused

    async def replace(_input: ScheduleUpdateInput) -> ScheduleUpdate:
        # The updater must return a ScheduleUpdate wrapper, not a bare
        # Schedule; returning the Schedule itself trips an assertion inside
        # the SDK with no useful message.
        definition = build_schedule()
        definition.state.paused = was_paused
        return ScheduleUpdate(schedule=definition)

    await handle.update(replace)
    print(f"updated schedule {SCHEDULE_ID!r} (paused={was_paused})")


async def set_paused(client: Client, paused: bool) -> None:
    """Pause or resume the Schedule.

    :param client: Connected Temporal client.
    :param paused: ``True`` to pause, ``False`` to resume.
    """
    handle = client.get_schedule_handle(SCHEDULE_ID)
    if paused:
        await handle.pause(note="paused by schedule.py")
        print(f"paused {SCHEDULE_ID!r}")
    else:
        await handle.unpause(note="resumed by schedule.py")
        print(f"resumed {SCHEDULE_ID!r}")


async def describe(client: Client) -> None:
    """Print the Schedule's current state and recent actions.

    :param client: Connected Temporal client.
    """
    handle = client.get_schedule_handle(SCHEDULE_ID)
    description = await handle.describe()
    state = description.schedule.state
    info = description.info

    print(f"schedule:        {SCHEDULE_ID}")
    print(f"paused:          {state.paused}")
    print(f"tick:            {TICK_INTERVAL}")
    print(f"overlap:         {description.schedule.policy.overlap.name}")
    print(f"running now:     {len(info.running_actions)}")
    print(f"actions taken:   {info.num_actions}")
    print(f"actions skipped: {info.num_actions_skipped_overlap}")
    if info.recent_actions:
        print("recent:")
        for action in info.recent_actions[-5:]:
            started = action.started_at.isoformat(timespec="seconds")
            execution = action.action
            workflow_id = getattr(execution, "workflow_id", "?")
            print(f"  {started}  {workflow_id}")


async def delete(client: Client) -> None:
    """Delete the Schedule.

    :param client: Connected Temporal client.
    """
    handle = client.get_schedule_handle(SCHEDULE_ID)
    await handle.delete()
    print(f"deleted {SCHEDULE_ID!r}")


async def trigger(client: Client) -> None:
    """Run one frame immediately, regardless of pause state.

    The fastest way to check the whole pipeline end to end without letting the
    loop run continuously.

    :param client: Connected Temporal client.
    """
    handle = client.get_schedule_handle(SCHEDULE_ID)
    await handle.trigger(overlap=ScheduleOverlapPolicy.SKIP)
    print(f"triggered one frame on {SCHEDULE_ID!r}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments.

    :param argv: Argument list, defaulting to ``sys.argv[1:]``.
    :returns: Parsed arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--create", action="store_true", help="create the schedule")
    group.add_argument("--update", action="store_true", help="push definition changes")
    group.add_argument("--start", action="store_true", help="resume the schedule")
    group.add_argument("--stop", action="store_true", help="pause the schedule")
    group.add_argument("--status", action="store_true", help="show current state")
    group.add_argument("--trigger", action="store_true", help="run one frame now")
    group.add_argument("--delete", action="store_true", help="delete the schedule")
    parser.add_argument(
        "--unpaused",
        action="store_true",
        help="with --create, start running immediately instead of paused",
    )
    return parser.parse_args(argv)


async def main() -> None:
    """Entry point."""
    args = parse_args()
    client = await connect()

    try:
        if args.create:
            await create(client, start_paused=not args.unpaused)
        elif args.update:
            await update(client)
        elif args.start:
            await set_paused(client, paused=False)
        elif args.stop:
            await set_paused(client, paused=True)
        elif args.status:
            await describe(client)
        elif args.trigger:
            await trigger(client)
        elif args.delete:
            await delete(client)
    except RPCError as rpc_error:
        raise SystemExit(f"Temporal rejected the call: {rpc_error}") from rpc_error


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(main()))
