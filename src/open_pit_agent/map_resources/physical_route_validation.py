"""P6 single-heavy-truck physical route validation for CARLA 0.9.10.

P5 records planner facts.  This module records a separate, append-only
physical driving attempt and deliberately never changes P5 reachability rows.
"""

import math
import time
from dataclasses import dataclass


STATUS_REACHED = "PHYSICAL_REACHED"
STATUS_TIMEOUT = "PHYSICAL_TIMEOUT"
STATUS_STUCK = "PHYSICAL_STUCK"
STATUS_SPAWN_FAILED = "PHYSICAL_SPAWN_FAILED"
STATUS_ENDPOINT_MISMATCH = "PHYSICAL_ENDPOINT_MISMATCH"
STATUS_EXECUTION_ERROR = "PHYSICAL_EXECUTION_ERROR"


def distance_3d(first, second):
    return math.sqrt(
        (float(first.x) - float(second.x)) ** 2
        + (float(first.y) - float(second.y)) ** 2
        + (float(first.z) - float(second.z)) ** 2
    )


def classify_result(final_distance_m, arrival_tolerance_m, timed_out, stuck, agent_done):
    """Classify a finished attempt without inferring route safety."""
    if final_distance_m <= float(arrival_tolerance_m):
        return STATUS_REACHED
    if stuck:
        return STATUS_STUCK
    if agent_done:
        return STATUS_ENDPOINT_MISMATCH
    if timed_out:
        return STATUS_TIMEOUT
    return STATUS_EXECUTION_ERROR


def release_basic_agent(agent):
    """Avoid CARLA 0.9.10 LocalPlanner destructor owning the test actor."""
    if agent is None:
        return
    local_planner = getattr(agent, "_local_planner", None)
    reset_vehicle = getattr(local_planner, "reset_vehicle", None)
    if callable(reset_vehicle):
        reset_vehicle()


@dataclass
class PhysicalRouteResult:
    status: str
    duration_seconds: float
    tick_count: int
    initial_distance_m: float
    final_distance_m: float
    distance_travelled_m: float
    notes: str


def _wait_one_tick(world, timeout_seconds):
    settings = world.get_settings()
    if bool(getattr(settings, "synchronous_mode", False)):
        world.tick()
    else:
        world.wait_for_tick(timeout_seconds)


def drive_single_route(
        carla, world, basic_agent_class, blueprint, start_transform,
        target_location, target_speed_kmh=15.0, arrival_tolerance_m=12.0,
        max_duration_seconds=300.0, stuck_window_seconds=25.0,
        minimum_progress_m=3.0):
    """Spawn, drive, classify and clean up a single mine truck.

    The caller receives factual completion data.  It must persist the result
    separately from planner calibration records.
    """
    spawn_transform = carla.Transform(
        carla.Location(
            x=start_transform.location.x,
            y=start_transform.location.y,
            z=start_transform.location.z + 0.2,
        ),
        start_transform.rotation,
    )
    vehicle = world.try_spawn_actor(blueprint, spawn_transform)
    if vehicle is None:
        return PhysicalRouteResult(
            STATUS_SPAWN_FAILED, 0.0, 0, None, None, 0.0,
            "try_spawn_actor returned None; point may be occupied or insufficient for the blueprint",
        )

    agent = None
    started = time.monotonic()
    tick_count = 0
    initial_location = vehicle.get_location()
    initial_distance = distance_3d(initial_location, target_location)
    previous_location = initial_location
    distance_travelled = 0.0
    window_started = started
    window_distance = initial_distance
    final_distance = initial_distance
    status = STATUS_EXECUTION_ERROR
    notes = ""
    try:
        agent = basic_agent_class(vehicle, target_speed=float(target_speed_kmh))
        agent.set_destination([target_location.x, target_location.y, target_location.z])
        local_planner = getattr(agent, "_local_planner", None)
        set_speed = getattr(local_planner, "set_speed", None)
        if callable(set_speed):
            set_speed(float(target_speed_kmh))
        while True:
            now = time.monotonic()
            current_location = vehicle.get_location()
            final_distance = distance_3d(current_location, target_location)
            distance_travelled += distance_3d(previous_location, current_location)
            previous_location = current_location
            agent_done = bool(getattr(agent, "done", lambda: False)())
            if final_distance <= float(arrival_tolerance_m):
                status = STATUS_REACHED
                notes = "arrived within {:.2f}m tolerance".format(arrival_tolerance_m)
                break
            if agent_done:
                status = STATUS_ENDPOINT_MISMATCH
                notes = "BasicAgent reported route completion {:.2f}m from requested target".format(final_distance)
                break
            if now - window_started >= float(stuck_window_seconds):
                progress = window_distance - final_distance
                if progress < float(minimum_progress_m):
                    status = STATUS_STUCK
                    notes = "distance improved only {:.2f}m in {:.1f}s".format(progress, now - window_started)
                    break
                window_started = now
                window_distance = final_distance
            if now - started >= float(max_duration_seconds):
                status = STATUS_TIMEOUT
                notes = "maximum duration {:.1f}s exceeded".format(max_duration_seconds)
                break
            vehicle.apply_control(agent.run_step())
            _wait_one_tick(world, 2.0)
            tick_count += 1
    except Exception as exc:
        status = STATUS_EXECUTION_ERROR
        notes = "{}: {}".format(type(exc).__name__, exc)
        try:
            final_distance = distance_3d(vehicle.get_location(), target_location)
        except Exception:
            pass
    finally:
        try:
            vehicle.apply_control(carla.VehicleControl(throttle=0.0, brake=1.0, hand_brake=True))
        except Exception:
            pass
        release_basic_agent(agent)
        try:
            vehicle.destroy()
        except Exception:
            pass
    return PhysicalRouteResult(
        status, time.monotonic() - started, tick_count, initial_distance,
        final_distance, distance_travelled, notes,
    )
