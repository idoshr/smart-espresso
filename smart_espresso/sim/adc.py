"""ADCs that follow a script or a recording. No hardware behind them."""

from time import monotonic

from smart_espresso.analog_sensor.analog_sensor import ADCInterface


class SignalADC(ADCInterface):
    """Voltage from a :class:`SimulatedMachine` channel.

    ``read`` caches the voltage, same contract as the ADS1115 and MCP3008
    drivers. A fault in the script raises ``OSError`` so the render loop's
    health path sees a failed sensor.
    """

    def __init__(self, machine, channel, recorder=None, max_voltage=5.0):
        self.machine = machine
        self.channel = channel
        self.recorder = recorder
        self._max_voltage = max_voltage
        self._voltage = 0.0
        self.samples = 0
        self.errors = 0

    def read(self):
        self.samples += 1
        now = monotonic()
        try:
            self._voltage = self.machine.voltage(self.channel, now)
        except OSError:
            self.errors += 1
            raise
        if self.recorder is not None:
            self.recorder.add(now, self.channel, self._voltage)
        return min(self._voltage / self._max_voltage, 1.0)

    @property
    def voltage(self):
        return self._voltage


class ReplayADC(ADCInterface):
    """Play back ``(t, volts)`` samples. Holds the latest sample at or before now."""

    def __init__(self, samples, max_voltage=5.0):
        self.samples = list(samples)
        self._index = 0
        self._max_voltage = max_voltage
        self._voltage = self.samples[0][1] if self.samples else 0.0
        self.samples_read = 0
        self.errors = 0

    def read(self):
        self.samples_read += 1
        now = monotonic()
        while (
            self._index + 1 < len(self.samples)
            and self.samples[self._index + 1][0] <= now + 1e-9
        ):
            self._index += 1
        if self.samples:
            self._voltage = self.samples[self._index][1]
        return min(self._voltage / self._max_voltage, 1.0)

    @property
    def voltage(self):
        return self._voltage


class CsvRecorder:
    """Raw ``t,ch,volts`` rows, one per ADC read."""

    def __init__(self, path):
        self.path = path
        self._file = open(path, "w", encoding="utf-8")
        self._file.write("t,ch,volts\n")
        self.rows = 0

    def add(self, t, channel, volts):
        self._file.write(f"{t:.6f},{int(channel)},{volts:.6f}\n")
        self.rows += 1

    def close(self):
        self._file.close()


def load_recording(path):
    """Channel number → list of ``(t, volts)``, in time order."""
    channels = {0: [], 1: [], 2: []}
    with open(path, encoding="utf-8") as handle:
        header = handle.readline()
        if "t" not in header or "ch" not in header:
            raise ValueError(f"{path} is not a t,ch,volts recording")
        for line in handle:
            line = line.strip()
            if not line:
                continue
            raw_t, raw_ch, raw_v = line.split(",")
            channels[int(raw_ch)].append((float(raw_t), float(raw_v)))
    return channels
