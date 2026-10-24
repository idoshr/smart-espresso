from abc import ABC, abstractmethod
from collections import deque
from statistics import median

from homeassistant_api import Client


class ADCInterface(ABC):
    """Abstract interface for Analog-to-Digital Converters."""

    @abstractmethod
    def read(self):
        """
        Read normalized value from ADC.
        Returns a float between 0.0 and 1.0.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def voltage(self):
        """Get the actual voltage reading.

        Implementations must return the voltage of the sample produced by the
        most recent ``read()`` (i.e. cache it), not trigger a second hardware
        read: callers such as ``MedianFilterADC`` pair the two and assume they
        describe the same sample.
        """
        raise NotImplementedError


class MedianFilterADC(ADCInterface):
    """ADC decorator that rejects isolated noise spikes.

    Espresso machines are electrically noisy environments (vibration pump,
    solenoid valves, heater switching all kick in at unpredictable moments,
    and that interference reaches the ADC lines). Without filtering, a single
    glitched sample is displayed and pushed to Home Assistant as a full-blown
    spike, which reads as values "blinking high at random times".

    The filter keeps a short window of recent samples and reports their
    median: an isolated spike is outvoted by its clean neighbours, while real
    pressure changes (which span many consecutive samples) pass through. It
    costs no extra hardware reads — the window is filled by the samples
    ``read()`` already performs.

    Note on ``window``: use an odd size — an even-sized window has no single
    middle sample, so its median averages a spike in instead of rejecting it.
    ``window=1`` disables filtering entirely.
    """

    def __init__(self, adc: ADCInterface, window: int = 3):
        if window < 1:
            raise ValueError(f"window must be >= 1 (got {window})")
        self.adc = adc
        self.window = window
        self._values: deque = deque(maxlen=window)
        self._voltages: deque = deque(maxlen=window)

    def read(self):
        """Take one hardware sample, add it to the window and return the median."""
        value = self.adc.read()
        self._values.append(value)
        self._voltages.append(self.adc.voltage)
        return self.filtered_value

    @property
    def filtered_value(self):
        """Median of the recent normalized readings (raw value until the window fills).

        A partially-filled window has an even sample count, whose "median"
        averages the spike in instead of rejecting it — so the raw value is
        reported until the window is full (a two-tick startup transient).
        """
        if not self._values:
            return self.adc.read()
        if len(self._values) < self.window:
            return self._values[-1]
        return median(self._values)

    @property
    def voltage(self):
        """Median voltage of the recent samples."""
        if not self._voltages:
            return self.adc.voltage
        if len(self._voltages) < self.window:
            return self._voltages[-1]
        return median(self._voltages)


class AnalogSensor(ABC):
    """Abstract base class for analog sensors (pressure, flow, temperature, etc.)."""

    def __init__(self, adc: ADCInterface, name: str):
        self.name = name
        self.adc = adc
        self._value = None

    def read(self):
        """Read the raw value from the ADC."""
        self._value = self.adc.read()
        return self._value

    @property
    def value(self):
        if self._value is not None:
            return self._value
        else:
            return self.read()

    @abstractmethod
    def update_home_assistant(self, client: Client):
        raise NotImplementedError

    @property
    @abstractmethod
    def message(self):
        raise NotImplementedError

    @property
    @abstractmethod
    def normalized_value(self):
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def unit_of_measurement():
        raise NotImplementedError
