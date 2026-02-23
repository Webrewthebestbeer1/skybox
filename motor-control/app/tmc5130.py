"""
TMC5130A stepper motor driver over SPI.

SPI Mode 3, 1 MHz. 5-byte datagrams: address byte (bit 7 = write) + 4 data bytes MSB-first.
Reads are delayed by one SPI transaction (must read twice to get the value).
"""

import logging
import struct
import time

import spidev

log = logging.getLogger(__name__)

# --- Register addresses ---
REG_GCONF = 0x00
REG_GSTAT = 0x01
REG_IOIN = 0x04
REG_IHOLD_IRUN = 0x10
REG_TPOWERDOWN = 0x11
REG_RAMPMODE = 0x20
REG_XACTUAL = 0x21
REG_VACTUAL = 0x22
REG_VSTART = 0x23
REG_A1 = 0x24
REG_V1 = 0x25
REG_AMAX = 0x26
REG_VMAX = 0x27
REG_DMAX = 0x28
REG_D1 = 0x2A
REG_VSTOP = 0x2B
REG_XTARGET = 0x2D
REG_CHOPCONF = 0x6C
REG_DRVSTATUS = 0x6F

# Ramp modes
RAMP_POSITION = 0


class TMC5130Error(Exception):
    pass


class TMC5130:
    """Driver for the TMC5130A stepper motor controller via SPI."""

    def __init__(self, bus: int = 0, device: int = 0):
        self.bus = bus
        self.device = device
        self.spi = None

    def open(self) -> None:
        """Open SPI connection."""
        self.spi = spidev.SpiDev()
        self.spi.open(self.bus, self.device)
        self.spi.max_speed_hz = 1_000_000
        self.spi.mode = 3
        self.spi.bits_per_word = 8
        log.info("SPI opened: bus=%d device=%d", self.bus, self.device)

    def close(self) -> None:
        """Close SPI connection."""
        if self.spi:
            self.spi.close()
            self.spi = None
            log.info("SPI closed")

    def _transfer(self, address: int, data: int, write: bool = False) -> int:
        """
        Perform a single 5-byte SPI transaction.

        Returns the 4-byte response from the previous transaction (SPI pipeline).
        """
        if self.spi is None:
            raise TMC5130Error("SPI not open")

        addr_byte = (address | 0x80) if write else (address & 0x7F)
        # Pack data as unsigned 32-bit big-endian
        data_bytes = struct.pack(">I", data & 0xFFFFFFFF)
        tx = [addr_byte] + list(data_bytes)
        rx = self.spi.xfer2(tx)

        # Response: status byte + 4 data bytes
        result = struct.unpack(">I", bytes(rx[1:5]))[0]
        return result

    def write_reg(self, address: int, value: int) -> None:
        """Write a 32-bit value to a register."""
        # Handle signed values by masking to 32 bits
        self._transfer(address, value & 0xFFFFFFFF, write=True)
        log.debug("WRITE reg 0x%02X = 0x%08X", address, value & 0xFFFFFFFF)

    def read_reg(self, address: int) -> int:
        """
        Read a 32-bit value from a register.

        Requires two SPI transactions due to the TMC5130 read pipeline.
        """
        self._transfer(address, 0, write=False)  # send read request
        value = self._transfer(address, 0, write=False)  # get the result
        log.debug("READ  reg 0x%02X = 0x%08X", address, value)
        return value

    def read_reg_signed(self, address: int) -> int:
        """Read a register and interpret as signed 32-bit integer."""
        raw = self.read_reg(address)
        if raw >= 0x80000000:
            return raw - 0x100000000
        return raw

    def init(
        self,
        current_run: int = 16,
        current_hold: int = 8,
        vmax: int = 100000,
        amax: int = 500,
    ) -> None:
        """
        Initialize the TMC5130A with stealthChop and S-curve ramp profile.

        Args:
            current_run: Run current 0-31
            current_hold: Hold current 0-31
            vmax: Maximum velocity in microsteps/t
            amax: Maximum acceleration
        """
        if self.spi is None:
            raise TMC5130Error("SPI not open — call open() first")

        # Clear GSTAT (write 1s to clear latched flags)
        self.write_reg(REG_GSTAT, 0x07)
        gstat = self.read_reg(REG_GSTAT)
        log.info("GSTAT after clear: 0x%08X", gstat)

        # Read IOIN for chip version
        ioin = self.read_reg(REG_IOIN)
        version = (ioin >> 24) & 0xFF
        log.info("TMC5130 version: 0x%02X", version)
        if version not in (0x11, 0x30):
            log.warning(
                "Unexpected chip version 0x%02X — expected 0x11 (TMC5130) or 0x30 (TMC5130A)",
                version,
            )

        # GCONF: enable stealthChop (en_pwm_mode = bit 2)
        self.write_reg(REG_GCONF, 0x00000004)

        # CHOPCONF: TOFF=5, HSTRT=4, HEND=1, TBL=2, MRES=0 (256 microsteps)
        # TBL(bits 16:15)=2, HEND(bits 10:7)=1, HSTRT(bits 6:4)=4, TOFF(bits 3:0)=5
        chopconf = (2 << 15) | (1 << 7) | (4 << 4) | 5
        self.write_reg(REG_CHOPCONF, chopconf)

        # IHOLD_IRUN: IRUN(bits 12:8), IHOLD(bits 4:0), IHOLDDELAY(bits 19:16)=6
        current_run = max(0, min(31, current_run))
        current_hold = max(0, min(31, current_hold))
        ihold_irun = (6 << 16) | (current_run << 8) | current_hold
        self.write_reg(REG_IHOLD_IRUN, ihold_irun)

        # TPOWERDOWN: reduce current after ~2 seconds of standstill
        self.write_reg(REG_TPOWERDOWN, 128)

        # S-curve ramp profile
        self.write_reg(REG_VSTART, 1)
        self.write_reg(REG_A1, amax * 2)
        self.write_reg(REG_V1, vmax // 2)
        self.write_reg(REG_AMAX, amax)
        self.write_reg(REG_VMAX, vmax)
        self.write_reg(REG_DMAX, amax)
        self.write_reg(REG_D1, amax * 2)
        self.write_reg(REG_VSTOP, 10)

        # Position mode
        self.write_reg(REG_RAMPMODE, RAMP_POSITION)

        log.info(
            "TMC5130 initialized: run_current=%d hold_current=%d vmax=%d amax=%d",
            current_run,
            current_hold,
            vmax,
            amax,
        )

    def set_position(self, position: int) -> None:
        """Set both XACTUAL and XTARGET to a position (no movement)."""
        self.write_reg(REG_XACTUAL, position)
        self.write_reg(REG_XTARGET, position)
        log.info("Position set to %d (no movement)", position)

    def move_to(self, target: int) -> None:
        """Command a move to an absolute target position."""
        self.write_reg(REG_XTARGET, target)
        log.info("Moving to target %d", target)

    def get_position(self) -> int:
        """Read current XACTUAL as signed 32-bit."""
        return self.read_reg_signed(REG_XACTUAL)

    def get_target(self) -> int:
        """Read current XTARGET as signed 32-bit."""
        return self.read_reg_signed(REG_XTARGET)

    def get_velocity(self) -> int:
        """Read current VACTUAL as signed 32-bit."""
        return self.read_reg_signed(REG_VACTUAL)

    def is_moving(self) -> bool:
        """Check if motor is currently moving (VACTUAL != 0)."""
        return self.get_velocity() != 0

    def set_speed(self, vmax: int, amax: int) -> None:
        """Update velocity and acceleration without full reinit."""
        self.write_reg(REG_A1, amax * 2)
        self.write_reg(REG_V1, vmax // 2)
        self.write_reg(REG_AMAX, amax)
        self.write_reg(REG_VMAX, vmax)
        self.write_reg(REG_DMAX, amax)
        self.write_reg(REG_D1, amax * 2)
        log.info("Speed updated: vmax=%d amax=%d", vmax, amax)

    def stop(self) -> None:
        """Emergency stop: set XTARGET = XACTUAL."""
        pos = self.get_position()
        self.write_reg(REG_XTARGET, pos)
        log.info("Emergency stop at position %d", pos)
