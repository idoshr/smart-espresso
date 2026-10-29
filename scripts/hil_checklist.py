#!/usr/bin/env python3
"""Hardware-in-the-loop checklist.

Reports the effective sample rate, the I2C error rate and the resting
voltages. ``--sim`` runs the same report against the fake sensors, which
is what CI and a laptop can do. On a Pi without ``--sim`` this prints the
boot config to try when the measured rate is far below the requested one.

    dtparam=i2c_arm_baudrate=400000
"""

import argparse
import os
import sys

# Allow `python scripts/hil_checklist.py` from a checkout without install.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smart_espresso.sim.machine import SimulatedMachine  # noqa: E402
from smart_espresso.sim.session import build_sim_app, drive, virtual_clock  # noqa: E402


def _report(rows, note):
    print(f"{'channel':<10} {'sample_hz':>10} {'i2c_errors':>12} {'zero_V':>8}")
    for name, hz, errors, zero in rows:
        print(f"{name:<10} {hz:10.1f} {errors:12d} {zero:8.3f}")
    total = sum(row[2] for row in rows)
    samples = sum(max(row[1], 0.0) for row in rows)
    rate = 0.0 if samples <= 0 else total / samples
    print(f"i2c_error_rate {rate:.4f}")
    print(note)


def sim_report(seconds=2.0, seed=59):
    """Virtual-time sample rate. One second of script is not one second of CPU."""
    machine = SimulatedMachine(seed=seed, loop=False)
    with virtual_clock() as clock:
        app = build_sim_app(machine=machine, sample_interval=None)
        drive(app, clock, seconds, sample_dt=0.005, render_dt=0.1)
        rows = []
        for sensor in app.analog_devices:
            adc = sensor.adc
            inner = getattr(adc, "adc", adc)
            samples = getattr(inner, "samples", 0) or getattr(inner, "samples_read", 0)
            errors = getattr(inner, "errors", 0)
            zero = inner.voltage if samples else 0.0
            hz = samples / seconds if seconds else 0.0
            rows.append((sensor.name, hz, errors, zero))
    note = (
        "sim: rates are virtual. On a Pi, if sample_hz is well below the "
        "requested 200 Hz flow rate, set dtparam=i2c_arm_baudrate=400000"
    )
    _report(rows, note)
    return 0


def hardware_report():
    print("Hardware checklist needs a Raspberry Pi with the ADC connected.")
    print("Flow sampler period defaults to 0.005 s (200 Hz).")
    print("Head and boiler are read on the render loop (about 10 Hz).")
    print("If the measured flow sample_hz is well below 200, add to /boot/config.txt:")
    print("dtparam=i2c_arm_baudrate=400000")
    print("Then reboot and re-run this script on the Pi.")
    print("Zero voltages are the resting ADC volts with the machine off and")
    print("the group vented. Expect about 0.500 V. Under 0.200 V is an open wire.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sim",
        action="store_true",
        help="Report against the fake sensors instead of the Pi",
    )
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--seed", type=int, default=59)
    args = parser.parse_args(argv)
    if args.sim:
        return sim_report(seconds=args.seconds, seed=args.seed)
    return hardware_report()


if __name__ == "__main__":
    raise SystemExit(main())
