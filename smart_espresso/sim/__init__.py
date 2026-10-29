"""Laptop-side stand-in for the Pi sensors.

``ADC_TYPE=SIM`` (or ``python main.py --sim``) builds these fakes instead of
an ADC, a DHT22 or an OLED. Nothing in this package imports GPIO, I2C or
``luma``.
"""

from smart_espresso.sim.adc import ReplayADC, SignalADC
from smart_espresso.sim.machine import SimulatedMachine, reference_program
from smart_espresso.sim.sensors import SimDHT, SimOLED

__all__ = [
    "ReplayADC",
    "SignalADC",
    "SimDHT",
    "SimOLED",
    "SimulatedMachine",
    "reference_program",
]
