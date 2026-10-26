"""Sensor drivers.

Import a driver from its module (``dht22_sensor``, ``water_flow_sensor``, …).
This package does not import them: the DHT22 driver pulls in an optional GPIO
library, and importing the package must stay possible with no extra hardware
dependencies installed.
"""
