"""DHT and OLED stand-ins. Same methods the render loop and Home Assistant use."""

from time import monotonic

from PIL import Image, ImageDraw


class SimDHT:
    """Ambient temperature and humidity from the simulated machine.

    ``read`` returns ``(humidity, temperature)`` like ``DHT22Sensor`` and
    never imports the GPIO driver.
    """

    def __init__(self, machine, name="Environment"):
        self.machine = machine
        self.name = name
        self._temperature = None
        self._humidity = None

    def read(self):
        self._temperature, self._humidity = self.machine.climate(monotonic())
        return self._humidity, self._temperature

    @property
    def temperature(self):
        return self._temperature

    @property
    def humidity(self):
        return self._humidity

    @property
    def message(self):
        label = "Room" if self.name == "Environment" else self.name
        if self._temperature is None or self._humidity is None:
            return f"{label}: No data"
        return f"{label}: {self._temperature:.1f}°C {self._humidity:.1f}%"

    def ha_states(self):
        from smart_espresso.home_assistant import State

        if self._temperature is None:
            return []
        slug = self.name.lower().replace(" ", "_")
        return [
            State(
                entity_id=f"sensor.{slug}_temperature",
                state=str(round(self._temperature, 1)),
                attributes={
                    "unit_of_measurement": "°C",
                    "friendly_name": f"{self.name} Temperature",
                    "device_class": "temperature",
                },
            ),
            State(
                entity_id=f"sensor.{slug}_humidity",
                state=str(round(self._humidity, 1)),
                attributes={
                    "unit_of_measurement": "%",
                    "friendly_name": f"{self.name} Humidity",
                    "device_class": "humidity",
                },
            ),
        ]

    def stop(self):
        return None


class SimOLED:
    """SH1106-sized framebuffer. ``display`` stores the frame; nothing is wired."""

    sim_display = True
    width = 128
    height = 64
    size = (128, 64)
    mode = "1"

    def __init__(self):
        self.frames = 0
        self.last = None

    def display(self, image):
        self.frames += 1
        self.last = image


class PillowCanvas:
    """``canvas`` stand-in used when ``luma`` is not installed, and for SimOLED."""

    def __init__(self, device, dither=False):
        self.device = device
        self.image = Image.new(getattr(device, "mode", "1"), device.size, 0)

    def __enter__(self):
        return ImageDraw.Draw(self.image)

    def __exit__(self, exc_type, exc, tb):
        self.device.display(self.image)
        return False
