#!/usr/bin/env python3
"""Run a short, isolated smoke test for the cooked Cat 797F blueprint."""

import argparse
import math
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from open_pit_agent.adapters.carla_adapter import CarlaAdapter
from open_pit_agent.config import load_config


DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "mine_competition_demo.json"
DEFAULT_BLUEPRINT = "vehicle.cat_ceshi.car_ceshi"
CAT797F_GEAR_RATIOS = (4.40, 3.27, 2.44, 1.80, 1.35, 1.00, 0.74)
CAT797F_TORQUE_CURVE = (
    (0.0, 0.0),
    (200.0, 30000.0),
    (400.0, 50000.0),
    (800.0, 60000.0),
    (1200.0, 60000.0),
    (1600.0, 52000.0),
    (2200.0, 36000.0),
)


def distance_2d(first, second):
    return math.hypot(first.x - second.x, first.y - second.y)


def wait_step(world, seconds):
    settings = world.get_settings()
    if settings.synchronous_mode:
        delta = settings.fixed_delta_seconds or 0.05
        for _ in range(max(1, int(seconds / delta))):
            world.tick()
    else:
        deadline = time.time() + seconds
        while time.time() < deadline:
            world.wait_for_tick(2.0)


def follow_vehicle(world, vehicle):
    transform = vehicle.get_transform()
    yaw = math.radians(transform.rotation.yaw)
    location = transform.location
    spectator_location = type(location)(
        x=location.x - 24.0 * math.cos(yaw),
        y=location.y - 24.0 * math.sin(yaw),
        z=location.z + 12.0,
    )
    spectator_rotation = type(transform.rotation)(
        pitch=-24.0,
        yaw=transform.rotation.yaw,
        roll=0.0,
    )
    world.get_spectator().set_transform(
        type(transform)(spectator_location, spectator_rotation)
    )


def apply_cat797f_physics(carla, vehicle):
    """Apply the already validated empty-load Cat 797F baseline."""
    physics = vehicle.get_physics_control()
    physics.mass = 258217.0
    physics.drag_coefficient = 0.06
    physics.center_of_mass = carla.Vector3D(-0.20, 0.0, -0.55)
    physics.max_rpm = 2600.0
    physics.moi = 27.0
    physics.damping_rate_full_throttle = 0.22
    physics.damping_rate_zero_throttle_clutch_engaged = 1.15
    physics.damping_rate_zero_throttle_clutch_disengaged = 0.45
    physics.torque_curve = [
        carla.Vector2D(rpm, torque * 6.8)
        for rpm, torque in CAT797F_TORQUE_CURVE
    ]
    physics.use_gear_autobox = True
    physics.gear_switch_time = 0.5
    physics.clutch_strength = 850.0
    physics.final_ratio = 21.26

    gears = []
    for ratio in CAT797F_GEAR_RATIOS:
        gear = carla.GearPhysicsControl()
        gear.ratio = ratio
        gear.down_ratio = 0.5
        gear.up_ratio = 0.9
        gears.append(gear)
    physics.forward_gears = gears

    for wheel in physics.wheels:
        wheel.tire_friction = 8.3
        wheel.damping_rate = 3.2
        wheel.max_brake_torque = 500000.0
        if wheel.max_steer_angle > 0.0:
            wheel.max_steer_angle = 40.0

    if hasattr(physics, "use_sweep_wheel_collision"):
        physics.use_sweep_wheel_collision = True
    vehicle.apply_physics_control(physics)
    print(
        "已应用旧测试验证的Cat 797F空载动力学参数："
        "mass=258217kg, torque_scale=6.8, final_ratio=21.26"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Cat 797F矿卡生成、行驶、转向和制动冒烟测试"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--blueprint", default=DEFAULT_BLUEPRINT)
    parser.add_argument("--map", default="0325_5")
    parser.add_argument("--spawn-point", type=int, default=26)
    parser.add_argument("--drive-seconds", type=float, default=12.0)
    parser.add_argument("--load-map", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument(
        "--apply-cat797f-physics",
        action="store_true",
        help="显式应用旧Cat 797F动力学测试参数",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    adapter = CarlaAdapter(config)
    adapter._import_carla()
    carla = adapter.carla

    client = carla.Client(config.carla.host, config.carla.port)
    client.set_timeout(120.0)
    world = client.get_world()
    current_map = world.get_map().name.split("/")[-1]
    if args.load_map and current_map != args.map:
        print("正在加载矿山地图：{}".format(args.map))
        world = client.load_world(args.map)
        current_map = world.get_map().name.split("/")[-1]

    if current_map != args.map:
        raise RuntimeError(
            "当前地图是{}，请增加 --load-map 切换到{}".format(
                current_map, args.map
            )
        )

    blueprint_library = world.get_blueprint_library()
    blueprint = blueprint_library.find(args.blueprint)
    if blueprint.has_attribute("role_name"):
        blueprint.set_attribute("role_name", "cat797f_smoke_test")

    spawn_points = world.get_map().get_spawn_points()
    if not 0 <= args.spawn_point < len(spawn_points):
        raise RuntimeError(
            "出生点{}超出范围，当前地图共{}个出生点".format(
                args.spawn_point, len(spawn_points)
            )
        )

    base_transform = spawn_points[args.spawn_point]
    spawn_transform = carla.Transform(
        base_transform.location + carla.Location(z=0.2),
        base_transform.rotation,
    )
    vehicle = world.try_spawn_actor(blueprint, spawn_transform)
    if vehicle is None:
        raise RuntimeError(
            "矿卡生成失败：出生点可能被占用或空间不足"
        )

    try:
        print("矿卡已生成：{}".format(vehicle.type_id))
        print("开始位置：{}".format(vehicle.get_location()))
        if args.apply_cat797f_physics:
            apply_cat797f_physics(carla, vehicle)
        else:
            print("使用蓝图默认车辆参数，未注入Cat 797F动力学参数。")
        follow_vehicle(world, vehicle)

        vehicle.apply_control(
            carla.VehicleControl(throttle=0.0, brake=1.0)
        )
        wait_step(world, 3.0)
        start_location = vehicle.get_location()

        print("正在执行低速直行与小角度转向测试……")
        started_at = time.time()
        while time.time() - started_at < args.drive_seconds:
            elapsed = time.time() - started_at
            steer = 0.08 if elapsed > args.drive_seconds * 0.55 else 0.0
            vehicle.apply_control(
                carla.VehicleControl(
                    throttle=0.56,
                    steer=steer,
                    brake=0.0,
                    manual_gear_shift=True,
                    gear=1,
                )
            )
            follow_vehicle(world, vehicle)
            wait_step(world, 0.1)

        vehicle.apply_control(
            carla.VehicleControl(
                throttle=0.0,
                steer=0.0,
                brake=1.0,
                hand_brake=True,
            )
        )
        wait_step(world, 2.0)
        end_location = vehicle.get_location()
        moved_m = distance_2d(start_location, end_location)
        speed_mps = math.sqrt(
            vehicle.get_velocity().x ** 2
            + vehicle.get_velocity().y ** 2
            + vehicle.get_velocity().z ** 2
        )

        print("行驶距离：{:.2f} m".format(moved_m))
        print("制动后速度：{:.2f} km/h".format(speed_mps * 3.6))
        if moved_m >= 1.0:
            print("测试结果：PASS（矿卡可生成且可控制）")
        else:
            print("测试结果：FAIL（矿卡未产生有效位移）")
            return 1
    finally:
        if args.keep:
            print("已保留矿卡，actor_id={}".format(vehicle.id))
        else:
            vehicle.destroy()
            print("测试矿卡已删除。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
