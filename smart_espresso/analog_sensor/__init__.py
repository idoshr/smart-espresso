"""Sensor drivers.

Import a driver from its module (``dht22_sensor``, ``water_flow_sensor``, …).
This package does not import them. The DHT22 module loads its GPIO library
only when a sensor is constructed, so importing the package stays possible
with no extra hardware dependencies installed.
"""
