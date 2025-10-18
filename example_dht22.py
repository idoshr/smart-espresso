#!/usr/bin/env python3
"""
Example: Using DHT22 Temperature and Humidity Sensor with Smart Espresso System

This example demonstrates how to integrate the DHT22 (AM2302) sensor with your
espresso machine monitoring setup.
"""

import os
from smart_espresso.analog_sensor.dht22_sensor import DHT22Sensor
from smart_espresso.analog_sensor.ads1115_analog_sensor import ADS1115ADC
from smart_espresso.analog_sensor.pressure_analog_sensor import PressureAnalogSensor
from smart_espresso.smart_espresso import SmartEspresso
from smart_espresso.utils import strtobool

# Optional: Setup display
try:
    from luma.core.interface.serial import i2c
    from luma.oled.device import sh1106
    display = sh1106(i2c(port=1, address=0x3C), width=128, height=64, rotate=0)
    print("Display initialized")
except Exception as e:
    print(f"Display not available: {e}")
    display = None

# Optional: Setup Home Assistant
HA_ENABLE = strtobool(os.environ.get("HA_ENABLE", "False"))
client_ha = None

if HA_ENABLE:
    from homeassistant_api import Client
    HA_URL = os.environ.get("HA_URL")
    HA_TOKEN = os.environ.get("HA_TOKEN")
    HA_VERIFY_SSL = strtobool(os.environ.get("HA_VERIFY_SSL", "True"))

    if HA_URL and HA_TOKEN:
        print("Connecting to Home Assistant...")
        client_ha = Client(f"{HA_URL}/api", HA_TOKEN, verify_ssl=HA_VERIFY_SSL)
        print("Connected to Home Assistant")

# Create pressure sensors (existing analog sensors)
analog_devices = [
    PressureAnalogSensor(
        adc=ADS1115ADC(pin=0, gain=2/3),
        name="Head"
    ),
    PressureAnalogSensor(
        adc=ADS1115ADC(pin=1, gain=2/3),
        name="Boiler"
    ),
]

# Create DHT22 temperature and humidity sensor
# GPIO4 (Pin 7) is a good choice that doesn't conflict with SPI or I2C
digital_sensors = [
    DHT22Sensor(
        pin=4,  # GPIO4 (Physical Pin 7)
        name="Environment",
        use_fahrenheit=False  # Set to True for Fahrenheit
    )
]

print("Starting Smart Espresso with DHT22 sensor...")
print("Monitoring:")
print("  - Brew head pressure (ADS1115 CH0)")
print("  - Boiler pressure (ADS1115 CH1)")
print("  - Temperature & Humidity (DHT22 on GPIO4)")

# Run the monitoring system
se = SmartEspresso(
    analog_devices=analog_devices,
    digital_sensors=digital_sensors,
    client_ha=client_ha,
    display=display,
    render_interval=0.1
)

try:
    se.run()
except KeyboardInterrupt:
    print("\nStopping Smart Espresso...")

