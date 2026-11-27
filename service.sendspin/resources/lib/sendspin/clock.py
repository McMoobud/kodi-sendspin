"""Clock synchronisation between the Sendspin server and this client.

Implements the Sendspin spec's time filter: a 2-D Kalman filter tracking
``offset = server_clock - client_clock`` and its drift. The client sends
``client/time`` in bursts and feeds the filter only the burst's best sample
(lowest ``max_error``, i.e. half the round trip).
"""

import math
import threading
import time


def now_us():
    """Monotonic local clock in microseconds."""
    return time.monotonic_ns() // 1000


def _round(x):
    """Nearest integer, halves away from zero (as the spec asks)."""
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def _trunc_half(x):
    return int(x / 2) if x >= 0 else -int(-x / 2)


def measure(client_tx, server_rx, server_tx, client_rx):
    """(measurement, max_error) for one server/time reply."""
    return (_trunc_half((server_rx - client_tx) + (server_tx - client_rx)),
            _trunc_half((client_rx - client_tx) - (server_tx - server_rx)))


class ClockSync:
    PROCESS_STD_DEV = 0.0
    DRIFT_PROCESS_STD_DEV = 1e-11
    FORGET_FACTOR = 2.0
    ADAPTIVE_CUTOFF = 3.0
    MIN_SAMPLES = 100
    DRIFT_SIGNIFICANCE = 2.0
    MAX_ERROR_SCALE = 0.5

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.offset = 0.0
            self.drift = 0.0
            self._p_oo, self._p_od, self._p_dd = math.inf, 0.0, 0.0
            self._last = 0
            self.count = 0
            self._use_drift = False
            self.rtt_us = None

    @property
    def synced(self):
        return self.count >= 1

    def add_sample(self, client_tx, server_rx, server_tx, client_rx):
        """Feed one reply straight in (use ``add_measurement`` with a burst's best)."""
        m, err = measure(client_tx, server_rx, server_tx, client_rx)
        self.add_measurement(m, err, client_rx)

    def add_measurement(self, measurement, max_error, time_added):
        with self._lock:
            if time_added <= self._last or max_error <= 0:
                return
            dt = float(time_added - self._last)
            self._last = time_added
            self.rtt_us = 2 * max_error
            r = (self.MAX_ERROR_SCALE * max_error) ** 2
            if self.count == 0:
                self.offset, self._p_oo, self.drift, self.count = float(measurement), r, 0.0, 1
                return
            if self.count == 1:
                self.drift = (measurement - self.offset) / dt
                self._p_dd = (self._p_oo + r) / (dt * dt)
                self.offset, self._p_oo, self.count = float(measurement), r, 2
                return
            offset_p = self.offset + self.drift * dt
            p_oo = self._p_oo + 2 * self._p_od * dt + self._p_dd * dt * dt + self.PROCESS_STD_DEV ** 2 * dt
            p_od = self._p_od + self._p_dd * dt
            p_dd = self._p_dd + self.DRIFT_PROCESS_STD_DEV ** 2 * dt
            y = measurement - offset_p
            if self.count < self.MIN_SAMPLES:
                self.count += 1
            elif abs(y) > self.ADAPTIVE_CUTOFF * max_error:
                f = self.FORGET_FACTOR ** 2
                p_oo, p_od, p_dd = p_oo * f, p_od * f, p_dd * f
            s = p_oo + r
            k_o, k_d = p_oo / s, p_od / s
            self.offset = offset_p + k_o * y
            self.drift = self.drift + k_d * y
            self._p_oo = p_oo - k_o * p_oo
            self._p_od = p_od - k_d * p_oo
            self._p_dd = p_dd - k_d * p_od
            self._use_drift = self.drift ** 2 > self.DRIFT_SIGNIFICANCE ** 2 * self._p_dd

    def _d(self):
        return self.drift if self._use_drift else 0.0

    def server_to_local(self, server_us):
        """Convert a server timestamp to the local monotonic clock (µs), or None before sync."""
        if self.count < 1:
            return None
        d = self._d()
        return _round((server_us - self.offset + d * self._last) / (1 + d))

    def server_now(self):
        if self.count < 1:
            return None
        t = now_us()
        return t + _round(self.offset + self._d() * (t - self._last))
