"""Minimal PCA9685 driver over smbus2, replacing Adafruit Blinka.

Blinka is not usable here. Its ``board`` module pulls in a GPIO backend even
when only I2C is needed, and on this Pi that backend (``lgpio``) has no
aarch64 wheel and needs ``swig`` plus root to build. This project drives the
chip over I2C only, so the dependency bought nothing and cost a great deal:
one package instead of sixteen, no compiler, no sudo, and considerably less
resident memory on a 415 MB board.

The duty-cycle interface is deliberately 16-bit, matching Adafruit's
convention, even though the hardware registers are 12-bit. Keeping that
convention means the angle conversions in :mod:`purifier.hardware` and their
tests are unchanged by this swap.
"""

import time
from types import TracebackType
from typing import Final

from smbus2 import SMBus

#: Register addresses.
MODE1: Final = 0x00
MODE2: Final = 0x01
PRESCALE: Final = 0xFE
LED0_ON_L: Final = 0x06

#: MODE1 bits.
MODE1_ALLCALL: Final = 0x01
MODE1_SLEEP: Final = 0x10
MODE1_AUTO_INCREMENT: Final = 0x20
MODE1_RESTART: Final = 0x80

#: MODE2 totem-pole output drive, which is what a servo signal line wants.
MODE2_OUTDRV: Final = 0x04

#: The chip counts in 12 bits per channel; the public API uses 16.
COUNTS_PER_CYCLE: Final = 4096
DUTY_FULL_SCALE: Final = 0xFFFF

#: Bit 4 of a channel's ON_H/OFF_H byte forces the output fully on/off.
FULL_ON_BIT: Final = 0x10
FULL_OFF_BIT: Final = 0x10

#: Prescale register limits, per the datasheet.
MIN_PRESCALE: Final = 3
MAX_PRESCALE: Final = 255


class PCA9685:
    """A PCA9685 PWM controller on an I2C bus.

    :param bus_number: Linux I2C bus number; 1 on all modern Pis.
    :param address: 7-bit I2C address of the chip.
    :param reference_clock_hz: Internal oscillator frequency.
    """

    def __init__(
        self,
        bus_number: int = 1,
        address: int = 0x40,
        reference_clock_hz: int = 25_000_000,
    ) -> None:
        self._address = address
        self._reference_clock_hz = reference_clock_hz
        self._bus = SMBus(bus_number)

    def __enter__(self) -> "PCA9685":
        """Enter a context manager.

        :returns: This driver.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the bus on exit."""
        self.close()

    def close(self) -> None:
        """Release the I2C bus.

        Channel registers keep their values, so servos hold position and the
        next process can still read back where they are.
        """
        self._bus.close()

    def configure(self) -> None:
        """Wake the chip and set totem-pole outputs with auto-increment.

        Safe to call repeatedly. Does not touch channel registers, so calling
        it does not disturb servos that are already holding a position.
        """
        self._bus.write_byte_data(self._address, MODE2, MODE2_OUTDRV)
        mode1 = int(self._bus.read_byte_data(self._address, MODE1))
        mode1 = (mode1 | MODE1_ALLCALL | MODE1_AUTO_INCREMENT) & ~MODE1_SLEEP
        self._bus.write_byte_data(self._address, MODE1, mode1)

    @property
    def frequency(self) -> float:
        """Current PWM frequency in Hz, derived from the prescale register.

        :returns: Frequency in Hz.
        """
        prescale = int(self._bus.read_byte_data(self._address, PRESCALE))
        return self._reference_clock_hz / (COUNTS_PER_CYCLE * (prescale + 1))

    @frequency.setter
    def frequency(self, hz: float) -> None:
        """Set the PWM frequency.

        The prescale register can only be written while the chip is asleep,
        so this sleeps, writes, wakes, and issues a restart.

        :param hz: Desired frequency in Hz.
        :raises ValueError: If the frequency is unreachable for this clock.
        """
        prescale = round(self._reference_clock_hz / (COUNTS_PER_CYCLE * hz)) - 1
        if not MIN_PRESCALE <= prescale <= MAX_PRESCALE:
            raise ValueError(
                f"{hz} Hz needs prescale {prescale}, outside "
                f"{MIN_PRESCALE}-{MAX_PRESCALE}"
            )

        old_mode1 = int(self._bus.read_byte_data(self._address, MODE1))
        self._bus.write_byte_data(
            self._address, MODE1, (old_mode1 & ~MODE1_RESTART) | MODE1_SLEEP
        )
        self._bus.write_byte_data(self._address, PRESCALE, prescale)
        self._bus.write_byte_data(self._address, MODE1, old_mode1 & ~MODE1_SLEEP)
        # The oscillator needs time to stabilise before RESTART is accepted.
        time.sleep(0.005)
        self._bus.write_byte_data(self._address, MODE1, old_mode1 | MODE1_RESTART)

    def get_duty_cycle(self, channel: int) -> int:
        """Read a channel's duty cycle, scaled to 16 bits.

        :param channel: Channel index, 0-15.
        :returns: Duty cycle from 0 to 0xFFFF. Zero means the output is not
            being driven, which is how an unhomed servo is detected.
        """
        base = LED0_ON_L + 4 * channel
        registers = [
            int(value)
            for value in self._bus.read_i2c_block_data(self._address, base, 4)
        ]
        _on_low, on_high, off_low, off_high = registers
        if on_high & FULL_ON_BIT:
            return DUTY_FULL_SCALE
        if off_high & FULL_OFF_BIT:
            return 0
        return ((off_low | (off_high << 8)) & 0x0FFF) << 4

    def set_duty_cycle(self, channel: int, duty_cycle: int) -> None:
        """Set a channel's duty cycle from a 16-bit value.

        :param channel: Channel index, 0-15.
        :param duty_cycle: Duty cycle from 0 to 0xFFFF. 0 stops driving the
            output entirely; 0xFFFF drives it permanently high.
        :raises ValueError: If the channel or duty cycle is out of range.
        """
        if not 0 <= channel <= 15:
            raise ValueError(f"channel {channel} outside 0-15")
        if not 0 <= duty_cycle <= DUTY_FULL_SCALE:
            raise ValueError(f"duty cycle {duty_cycle} outside 0-0xFFFF")

        base = LED0_ON_L + 4 * channel
        if duty_cycle == DUTY_FULL_SCALE:
            payload = [0x00, FULL_ON_BIT, 0x00, 0x00]
        elif duty_cycle == 0:
            payload = [0x00, 0x00, 0x00, FULL_OFF_BIT]
        else:
            counts = duty_cycle >> 4
            payload = [0x00, 0x00, counts & 0xFF, (counts >> 8) & 0x0F]
        self._bus.write_i2c_block_data(self._address, base, payload)
