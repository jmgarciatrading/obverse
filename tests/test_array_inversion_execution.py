"""Executable spec for Array_Inversion_Execution.pine.

The engine below is the trading model. The Pine script is a port of it.
Tests lock timeframe stacks, immutable inversion references, MTF candle
expiration, HTF resets, and the dashboard stage order.
"""

from __future__ import annotations

import pathlib
import unittest
from collections import deque
from dataclasses import dataclass


ROOT = pathlib.Path(__file__).resolve().parents[1]
PINE = ROOT / "Array_Inversion_Execution.pine"

ST_WAITING = 0
ST_BIAS = 1
ST_ARRAY = 2
ST_INVERSION = 3
ST_RETEST = 4
ST_MSS = 5
ST_CONFIRMED = 6

STAGE_NAMES = [
    "WAITING",
    "BIAS",
    "ARRAY",
    "INVERSION",
    "RETEST",
    "MSS/BOS",
    "CONFIRMED",
]

EXP_NONE = 0
EXP_WAITING = 1
EXP_INVERTED = 2
EXP_EXPIRED = 3

KIND_NONE = 0
KIND_OB = 1
KIND_FVG = 2

SUPPORTED_STACKS = {
    ("4H", "15M", "5M"),
    ("4H", "5M", "1M"),
    ("1H", "15M", "5M"),
    ("1H", "5M", "1M"),
}


@dataclass(frozen=True)
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class ArraySignal:
    kind: int
    direction: int
    top: float
    bottom: float
    reference: float
    source_open: int
    identified_open: int


@dataclass(frozen=True)
class Confirmation:
    time: int
    direction: int


def progress(stage: int) -> list[str]:
    return [
        "done" if index < stage else "current" if index == stage else "future"
        for index in range(len(STAGE_NAMES))
    ]


class Model:
    """One HTF period of the array-inversion sequence."""

    def __init__(
        self,
        htf_ms: int,
        mtf_ms: int,
        ltf_ms: int,
        max_candles: int = 3,
        swing: int = 2,
        target_ms: int | None = None,
    ):
        if max_candles < 1:
            raise ValueError("max_candles must be at least 1")
        if swing < 1:
            raise ValueError("swing must be at least 1")
        self.htf_ms = htf_ms
        self.mtf_ms = mtf_ms
        self.ltf_ms = ltf_ms
        self.max_candles = max_candles
        self.swing = swing
        self.stage = ST_WAITING
        self.bias = 0
        self.locked_bias = 0
        self.active_open: int | None = None
        self.kind = KIND_NONE
        self.array_dir = 0
        self.top: float | None = None
        self.bottom: float | None = None
        self.inversion_ref: float | None = None
        self.source_open: int | None = None
        self.identified_open: int | None = None
        self.inversion_mtf_open: int | None = None
        self.elapsed = 0
        self.expiration = EXP_NONE
        self.retest_time: int | None = None
        self.confirmations: list[Confirmation] = []
        self.flips: list[Confirmation] = []
        self.signals: list[Confirmation] = []
        self.mtf: deque[Candle] = deque(maxlen=3)
        self.history: deque[Candle] = deque(maxlen=50)
        self.ltf: deque[Candle] = deque(maxlen=swing * 2 + 1)
        self.swing_high: float | None = None
        self.swing_low: float | None = None
        self.stop_loss: float | None = None
        self.swing_points: list[tuple[int, int, float, int]] = []
        self.stop_time: int | None = None
        self.target_ms = target_ms
        self.target_bars: deque[Candle] = deque(maxlen=swing * 2 + 1)
        self.target_swings: list[tuple[int, float, int]] = []
        self.take_profit: float | None = None
        self.take_profit_time: int | None = None
        self.last_target: int | None = None
        self.swing_serial = 0
        self.direction = 0
        self.sweep_ref_high: float | None = None
        self.sweep_ref_low: float | None = None
        self.swept_high = False
        self.swept_low = False
        self.rejection_bias = False
        self.first_ob_bias = False
        self.last_mtf: int | None = None
        self.last_ltf: int | None = None

    def stage_name(self) -> str:
        return STAGE_NAMES[self.stage]

    def array_text(self) -> str:
        if self.kind == KIND_NONE:
            return "None"
        side = "Bearish" if self.array_dir < 0 else "Bullish"
        kind = "OB" if self.kind == KIND_OB else "FVG"
        return f"{side} {kind}"

    def countdown(self) -> str:
        if self.expiration == EXP_NONE:
            return "—"
        return f"{self.elapsed}/{self.max_candles} elapsed"

    def expiration_text(self) -> str:
        return {EXP_NONE: "None", EXP_WAITING: "Waiting", EXP_INVERTED: "Inverted", EXP_EXPIRED: "Expired"}[
            self.expiration
        ]

    def retest_text(self) -> str:
        if self.stage >= ST_MSS:
            return "Confirmed"
        if self.stage == ST_RETEST:
            return "Waiting"
        return "—"

    def mss_text(self) -> str:
        if self.stage == ST_CONFIRMED:
            return "Confirmed"
        if self.stage == ST_MSS:
            return "Waiting"
        return "—"

    def bias_text(self) -> str:
        if self.bias > 0:
            text = "Bullish"
        elif self.bias < 0:
            text = "Bearish"
        else:
            return "None"
        if self.locked_bias != 0 and self.bias != self.locked_bias:
            text += " flip"
        return text

    def direction_text(self) -> str:
        if self.direction > 0:
            return "Bullish"
        if self.direction < 0:
            return "Bearish"
        return "None"

    def start(self, new_open: int, closed: Candle | None = None, prior: Candle | None = None) -> None:
        if new_open == self.active_open:
            return
        if closed is not None and prior is not None:
            both_sides = htf_swept_both(closed, prior)
            both_bullish = both_sides and closed.close > prior.high
            both_bearish = both_sides and closed.close < prior.low
            if both_bullish:
                bias = 1
                self.rejection_bias = True
                self.first_ob_bias = False
            elif both_bearish:
                bias = -1
                self.rejection_bias = True
                self.first_ob_bias = False
            elif both_sides:
                bias = 0
                self.rejection_bias = False
                self.first_ob_bias = True
            else:
                bias = htf_bias(closed, prior)
                self.rejection_bias = htf_rejection(closed, prior)
                self.first_ob_bias = False
            self.sweep_ref_high = closed.high
            self.sweep_ref_low = closed.low
        else:
            bias = 0
            self.rejection_bias = False
            self.first_ob_bias = False
            self.sweep_ref_high = None
            self.sweep_ref_low = None
        self.bias = bias
        self.locked_bias = bias
        self.direction = 0
        self.stop_loss = None
        self.stop_time = None
        self.take_profit = None
        self.take_profit_time = None
        self.swept_high = False
        self.swept_low = False
        self.active_open = new_open
        self._clear_array()
        self.stage = ST_BIAS if bias != 0 else ST_WAITING

    def on_mtf(self, candle: Candle) -> None:
        if candle.time == self.last_mtf:
            return
        self.last_mtf = candle.time
        self._note_sweep(candle)
        through = self._close_through(candle)
        self.history.append(candle)
        self.mtf.append(candle)
        if self._apply_through(candle, through):
            return
        in_period = self.active_open is not None and candle.time >= self.active_open
        if (
            self.stage == ST_INVERSION
            and self.identified_open is not None
            and self.active_open is not None
            and candle.time > self.identified_open
            and candle.time >= self.active_open
        ):
            self.elapsed += 1
            if self._closes_through(candle):
                self.expiration = EXP_INVERTED
                self.inversion_mtf_open = candle.time
                self.stage = ST_RETEST
            elif self.elapsed >= self.max_candles:
                self._expire()
        elif self.stage == ST_ARRAY and in_period:
            found = self._detect()
            if found is not None:
                self._adopt(found)
                self.stage = ST_INVERSION

    def on_ltf(self, candle: Candle) -> None:
        if candle.time == self.last_ltf:
            return
        self.last_ltf = candle.time
        self._note_sweep(candle)
        armed_high = self.swing_high
        armed_low = self.swing_low
        self.swing_serial += 1
        self.ltf.append(candle)
        self._update_swings()
        if (
            self.stage == ST_RETEST
            and self.inversion_mtf_open is not None
            and candle.time >= self.inversion_mtf_open + self.mtf_ms
            and self.top is not None
            and self.bottom is not None
            and candle.low <= self.top
            and candle.high >= self.bottom
        ):
            self.retest_time = candle.time
            self.stage = ST_MSS
            return
        if self.stage == ST_MSS and self.retest_time is not None and candle.time > self.retest_time:
            level = armed_high if self.direction > 0 else armed_low
            broke = level is not None and (
                (self.direction > 0 and candle.close > level) or (self.direction < 0 and candle.close < level)
            )
            if broke:
                if self.stop_loss is None:
                    self.stop_loss = self._stop_before_break(level)
                self.confirmations.append(Confirmation(candle.time, self.direction))
                self.stage = ST_CONFIRMED

    def _note_sweep(self, candle: Candle) -> None:
        if self.active_open is None or candle.time < self.active_open:
            return
        if self.sweep_ref_low is not None and candle.low < self.sweep_ref_low:
            self.swept_low = True
        if self.sweep_ref_high is not None and candle.high > self.sweep_ref_high:
            self.swept_high = True

    def _close_through(self, candle: Candle) -> int:
        """1 when this close clears a bearish candle, -1 when it clears a bullish one.

        The walk stops at the first candle the close does not clear. Candles past
        that one do not raise the level and do not cancel the candles already cleared.
        """

        if self.active_open is None or candle.time < self.active_open or not self.history:
            return 0
        if candle.close > candle.open:
            found = False
            for previous in reversed(self.history):
                if candle.close <= previous.high:
                    break
                if previous.close < previous.open:
                    found = True
            if found:
                return 1
        if candle.close < candle.open:
            found = False
            for previous in reversed(self.history):
                if candle.close >= previous.low:
                    break
                if previous.close > previous.open:
                    found = True
            if found:
                return -1
        return 0

    def _apply_through(self, candle: Candle, through: int) -> bool:
        bullish = through > 0
        bearish = through < 0
        if self.first_ob_bias and self.direction == 0 and bullish:
            self.bias = 1
            self.locked_bias = 1
            self.direction = 1
            self.first_ob_bias = False
            self.rejection_bias = False
            self._clear_array()
            self.stage = ST_ARRAY
            self._capture_signal_stop(candle)
            self.signals.append(Confirmation(candle.time, 1))
            return True
        if self.first_ob_bias and self.direction == 0 and bearish:
            self.bias = -1
            self.locked_bias = -1
            self.direction = -1
            self.first_ob_bias = False
            self.rejection_bias = False
            self._clear_array()
            self.stage = ST_ARRAY
            self._capture_signal_stop(candle)
            self.signals.append(Confirmation(candle.time, -1))
            return True
        if self.bias < 0 and bullish and self.swept_low and (not self.rejection_bias or self.direction < 0):
            self.bias = 1
            self.direction = 1
            self.rejection_bias = False
            self._clear_array()
            self.stage = ST_ARRAY
            self._capture_signal_stop(candle)
            self.flips.append(Confirmation(candle.time, 1))
            self.signals.append(Confirmation(candle.time, 1))
            return True
        if self.bias > 0 and bearish and self.swept_high and (not self.rejection_bias or self.direction > 0):
            self.bias = -1
            self.direction = -1
            self.rejection_bias = False
            self._clear_array()
            self.stage = ST_ARRAY
            self._capture_signal_stop(candle)
            self.flips.append(Confirmation(candle.time, -1))
            self.signals.append(Confirmation(candle.time, -1))
            return True
        if self.bias < 0 and bearish and self.direction != -1:
            self.direction = -1
            if self.stage in (ST_WAITING, ST_BIAS):
                self.stage = ST_ARRAY
            self._capture_signal_stop(candle)
            self.signals.append(Confirmation(candle.time, -1))
            return True
        if self.bias > 0 and bullish and self.direction != 1:
            self.direction = 1
            if self.stage in (ST_WAITING, ST_BIAS):
                self.stage = ST_ARRAY
            self._capture_signal_stop(candle)
            self.signals.append(Confirmation(candle.time, 1))
            return True
        return False

    def _latest_stop_swing(self) -> float | None:
        """Latest swing high for a sell, or latest swing low for a buy."""

        kind = -1 if self.direction > 0 else 1
        self.stop_time = None
        for index in range(len(self.swing_points) - 1, -1, -1):
            _bar_id, swing_kind, price, pivot_time = self.swing_points[index]
            if swing_kind == kind:
                self.stop_time = pivot_time
                return price
        return None

    def _capture_signal_stop(self, candle: Candle) -> None:
        self.stop_loss = self._latest_stop_swing()
        self._capture_signal_target(candle)

    def on_target(self, candle: Candle) -> None:
        if self.target_ms is None or candle.time == self.last_target:
            return
        self.last_target = candle.time
        self.target_bars.append(candle)
        self._update_target_swings()

    def _update_target_swings(self) -> None:
        need = self.swing * 2 + 1
        bars = self.target_bars
        if len(bars) < need:
            return
        pivot = len(bars) - 1 - self.swing
        left = pivot - self.swing
        right = pivot + self.swing
        pivot_high = bars[pivot].high
        pivot_low = bars[pivot].low
        if all(bars[index].high < pivot_high for index in range(left, right + 1) if index != pivot):
            self.target_swings.append((1, pivot_high, bars[pivot].time))
        if all(bars[index].low > pivot_low for index in range(left, right + 1) if index != pivot):
            self.target_swings.append((-1, pivot_low, bars[pivot].time))
        if len(self.target_swings) > 50:
            del self.target_swings[:-50]

    def _capture_signal_target(self, candle: Candle) -> None:
        """Newest swing high above a buy, or newest swing low below a sell."""

        self.take_profit = None
        self.take_profit_time = None
        if self.target_ms is None:
            return
        bullish = self.direction > 0
        kind = 1 if bullish else -1
        limit = candle.high if bullish else candle.low
        for index in range(len(self.target_swings) - 1, -1, -1):
            swing_kind, price, pivot_time = self.target_swings[index]
            if swing_kind != kind:
                continue
            if bullish and price <= limit:
                continue
            if not bullish and price >= limit:
                continue
            self.take_profit = price
            self.take_profit_time = pivot_time
            return

    def _stop_before_break(self, level: float) -> float | None:
        """Opposite swing before the swing the MSS closes through.

        A buy breaks a swing high, so the stop is the prior swing low.
        A sell breaks a swing low, so the stop is the prior swing high.
        The other extreme of the same pivot bar is not that swing.
        """

        kind = 1 if self.direction > 0 else -1
        opposite = -kind
        self.stop_time = None
        match = None
        for index in range(len(self.swing_points) - 1, -1, -1):
            if self.swing_points[index][1] == kind and self.swing_points[index][2] == level:
                match = index
                break
        if match is None:
            return None
        broken_bar = self.swing_points[match][0]
        for index in range(match - 1, -1, -1):
            bar_id, swing_kind, price, pivot_time = self.swing_points[index]
            if bar_id < broken_bar and swing_kind == opposite:
                self.stop_time = pivot_time
                return price
        return None

    def _closes_through(self, candle: Candle) -> bool:
        if self.inversion_ref is None:
            return False
        if self.direction > 0:
            return candle.close > self.inversion_ref
        if self.direction < 0:
            return candle.close < self.inversion_ref
        return False

    def _detect(self) -> ArraySignal | None:
        if len(self.mtf) < 2 or self.active_open is None or self.direction == 0:
            return None
        current = self.mtf[-1]
        previous = self.mtf[-2]
        if self.direction > 0:
            bearish_ob = (
                previous.close < previous.open
                and current.close > current.open
                and current.close > previous.high
                and previous.time >= self.active_open
            )
            if bearish_ob:
                return ArraySignal(KIND_OB, -1, previous.high, previous.low, previous.open, previous.time, current.time)
        else:
            bullish_ob = (
                previous.close > previous.open
                and current.close < current.open
                and current.close < previous.low
                and previous.time >= self.active_open
            )
            if bullish_ob:
                return ArraySignal(KIND_OB, 1, previous.high, previous.low, previous.open, previous.time, current.time)
        if len(self.mtf) < 3:
            return None
        first = self.mtf[-3]
        if self.direction > 0 and first.low > current.high and current.time >= self.active_open:
            return ArraySignal(KIND_FVG, -1, first.low, current.high, current.open, current.time, current.time)
        if self.direction < 0 and first.high < current.low and current.time >= self.active_open:
            return ArraySignal(KIND_FVG, 1, current.low, first.high, current.open, current.time, current.time)
        return None

    def _adopt(self, signal: ArraySignal) -> None:
        self.kind = signal.kind
        self.array_dir = signal.direction
        self.top = signal.top
        self.bottom = signal.bottom
        self.inversion_ref = signal.reference
        self.source_open = signal.source_open
        self.identified_open = signal.identified_open
        self.inversion_mtf_open = None
        self.elapsed = 0
        self.expiration = EXP_WAITING
        self.retest_time = None

    def _expire(self) -> None:
        elapsed = self.elapsed
        self._clear_array()
        self.elapsed = elapsed
        self.expiration = EXP_EXPIRED
        if self.direction != 0:
            self.stage = ST_ARRAY
        elif self.bias != 0:
            self.stage = ST_BIAS
        else:
            self.stage = ST_WAITING

    def _clear_array(self) -> None:
        self.kind = KIND_NONE
        self.array_dir = 0
        self.top = None
        self.bottom = None
        self.inversion_ref = None
        self.source_open = None
        self.identified_open = None
        self.inversion_mtf_open = None
        self.elapsed = 0
        self.expiration = EXP_NONE
        self.retest_time = None

    def _update_swings(self) -> None:
        need = self.swing * 2 + 1
        bars = self.ltf
        if len(bars) < need:
            return
        pivot = len(bars) - 1 - self.swing
        left = pivot - self.swing
        right = pivot + self.swing
        pivot_high = bars[pivot].high
        pivot_low = bars[pivot].low
        if all(bars[index].high < pivot_high for index in range(left, right + 1) if index != pivot):
            self.swing_high = pivot_high
            self.swing_points.append((self.swing_serial, 1, pivot_high, bars[pivot].time))
        if all(bars[index].low > pivot_low for index in range(left, right + 1) if index != pivot):
            self.swing_low = pivot_low
            self.swing_points.append((self.swing_serial, -1, pivot_low, bars[pivot].time))
        if len(self.swing_points) > 50:
            del self.swing_points[:-50]


def apply_chart_update(
    model: Model,
    htf_period_open: int | None,
    closed_htf: Candle | None,
    prior_htf: Candle | None,
    mtf_candle: Candle | None,
    ltf_candle: Candle | None,
    target_candle: Candle | None = None,
) -> None:
    """Port of the Pine bar driver.

    Lower-timeframe candles that opened before a new HTF period are processed
    first. The HTF reset then reads bias from the two closed candles and clears
    the direction. Candles that opened in the new period run after the reset.
    Equal close times process the target timeframe, then LTF, then MTF.
    """

    def event_rank(name: str) -> int:
        if name == "target":
            return 0
        if name == "ltf":
            return 1
        return 2

    events: list[tuple[str, Candle, int]] = []
    if target_candle is not None and model.target_ms is not None:
        events.append(("target", target_candle, target_candle.time + model.target_ms))
    if ltf_candle is not None:
        events.append(("ltf", ltf_candle, ltf_candle.time + model.ltf_ms))
    if mtf_candle is not None:
        events.append(("mtf", mtf_candle, mtf_candle.time + model.mtf_ms))
    events.sort(key=lambda event: (event[2], event_rank(event[0])))

    def dispatch(event: tuple[str, Candle, int]) -> None:
        if event[0] == "target":
            model.on_target(event[1])
        elif event[0] == "ltf":
            model.on_ltf(event[1])
        else:
            model.on_mtf(event[1])

    rolling = htf_period_open is not None and htf_period_open != model.active_open
    if rolling and model.active_open is None:
        model.start(htf_period_open, closed_htf, prior_htf)
        for event in events:
            dispatch(event)
        return
    if rolling:
        for event in events:
            if event[1].time < htf_period_open:
                dispatch(event)
        model.start(htf_period_open, closed_htf, prior_htf)
        for event in events:
            if event[1].time >= htf_period_open:
                dispatch(event)
        return
    for event in events:
        dispatch(event)


# At 10:00, BULLISH_CLOSED is the 9:00 candle and BULLISH_PRIOR is the 8:00 candle.
BULLISH_PRIOR = Candle(-60, 90, 100, 80, 95)
BULLISH_CLOSED = Candle(0, 100, 120, 90, 110)
BEARISH_PRIOR = Candle(-60, 110, 110, 90, 100)
# High equals the prior high, so this is a one-sided close below the prior low.
BEARISH_CLOSED = Candle(0, 100, 110, 85, 80)


def in_signal_window(minute_of_hour: int, start: int, end: int) -> bool:
    """BUY and SELL labels print only inside this minute range of the exchange hour."""

    return start <= minute_of_hour <= end


def htf_rejection(closed: Candle, prior: Candle) -> bool:
    """True when the bias is a sweep that closed back inside the prior candle."""

    bearish_close = closed.close < prior.low
    bearish_reject = closed.high > prior.high and prior.low <= closed.close < prior.high
    bullish_close = closed.close > prior.high
    bullish_reject = closed.low < prior.low and prior.low < closed.close <= prior.high
    bearish = bearish_close or bearish_reject
    bullish = bullish_close or bullish_reject
    return (bearish and not bullish and bearish_reject) or (bullish and not bearish and bullish_reject)


def htf_swept_both(closed: Candle, prior: Candle) -> bool:
    """True when the closed candle traded strictly beyond both sides of the prior candle."""

    return closed.high > prior.high and closed.low < prior.low


def htf_bias(closed: Candle, prior: Candle) -> int:
    """How the closed HTF candle finished against the candle before it."""

    bearish_close = closed.close < prior.low
    bearish_reject = closed.high > prior.high and prior.low <= closed.close < prior.high
    bullish_close = closed.close > prior.high
    bullish_reject = closed.low < prior.low and prior.low < closed.close <= prior.high
    bearish = bearish_close or bearish_reject
    bullish = bullish_close or bullish_reject
    if bearish and not bullish:
        return -1
    if bullish and not bearish:
        return 1
    return 0


def feed_bullish_through(model: Model, t0: int) -> None:
    """Close through one bearish candle. Prices stay inside the standard sweep refs."""

    step = model.mtf_ms
    model.on_mtf(Candle(t0, 100, 110, 95, 98))
    model.on_mtf(Candle(t0 + step, 98, 116, 96, 112))


def feed_bearish_through(model: Model, t0: int) -> None:
    """Close through one bullish candle. Prices stay inside the standard sweep refs."""

    step = model.mtf_ms
    model.on_mtf(Candle(t0, 100, 110, 95, 108))
    model.on_mtf(Candle(t0 + step, 106, 109, 90, 94))


def bullish_ob_ready(max_candles: int = 3, swing: int = 1) -> Model:
    model = Model(60, 15, 5, max_candles=max_candles, swing=swing)
    model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
    feed_bullish_through(model, 0)
    model.on_mtf(Candle(60, 108, 109, 104, 106))
    model.on_mtf(Candle(75, 107, 112, 105, 111))
    return model


def arm_bullish_swing(model: Model) -> None:
    model.on_ltf(Candle(60, 100, 100, 99, 100))
    model.on_ltf(Candle(65, 100, 114, 99, 113))
    model.on_ltf(Candle(70, 100, 90, 89, 90))


class SequenceTests(unittest.TestCase):
    def test_hourly_bias_matrix(self):
        prior = Candle(-60, 100, 100, 80, 90)
        self.assertEqual(htf_bias(Candle(0, 90, 95, 70, 75), prior), -1)
        self.assertEqual(htf_bias(Candle(0, 90, 110, 85, 105), prior), 1)
        self.assertEqual(htf_bias(Candle(0, 90, 110, 85, 90), prior), -1)
        self.assertEqual(htf_bias(Candle(0, 90, 95, 70, 90), prior), 1)
        self.assertEqual(htf_bias(Candle(0, 90, 110, 70, 90), prior), 0)
        self.assertEqual(htf_bias(Candle(0, 90, 95, 85, 92), prior), 0)
        self.assertEqual(htf_bias(Candle(0, 90, 110, 85, 100), prior), 0)
        self.assertEqual(htf_bias(Candle(0, 90, 95, 70, 80), prior), 0)
        self.assertEqual(htf_bias(Candle(0, 70, 90, 65, 75), prior), -1)
        self.assertEqual(htf_bias(BULLISH_CLOSED, BULLISH_PRIOR), 1)
        self.assertEqual(htf_bias(BEARISH_CLOSED, BEARISH_PRIOR), -1)

    def test_htf_open_sets_bias_and_leaves_direction_empty(self):
        model = Model(60, 15, 5)
        self.assertEqual(model.stage_name(), "WAITING")
        self.assertEqual(progress(model.stage), ["current", "future", "future", "future", "future", "future", "future"])
        model.start(60, BULLISH_CLOSED, BULLISH_PRIOR)
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.direction_text(), "None")
        self.assertEqual(model.locked_bias, 1)
        self.assertEqual(model.stage_name(), "BIAS")
        self.assertEqual(progress(model.stage), ["done", "current", "future", "future", "future", "future", "future"])
        model.start(120, BEARISH_CLOSED, BEARISH_PRIOR)
        self.assertEqual(model.bias_text(), "Bearish")
        self.assertEqual(model.direction_text(), "None")
        self.assertEqual(model.locked_bias, -1)
        self.assertEqual(model.stage_name(), "BIAS")
        self.assertEqual(model.sweep_ref_high, 110)
        self.assertEqual(model.sweep_ref_low, 85)
        self.assertFalse(model.first_ob_bias)

    def test_a_close_through_does_nothing_while_bias_is_empty(self):
        model = Model(60, 15, 5, swing=1)
        model.start(0)
        feed_bullish_through(model, 0)
        feed_bearish_through(model, 60)
        self.assertEqual(model.bias_text(), "None")
        self.assertEqual(model.direction_text(), "None")
        self.assertEqual(model.stage_name(), "WAITING")

    def test_bullish_bias_and_a_bullish_close_through_set_direction(self):
        model = Model(60, 15, 5, swing=1)
        model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
        feed_bullish_through(model, 0)
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.direction_text(), "Bullish")
        self.assertEqual(model.locked_bias, 1)
        self.assertEqual(model.direction, 1)
        self.assertEqual(model.stage_name(), "ARRAY")
        self.assertIsNone(model.inversion_ref)
        self.assertFalse(model.swept_low)
        self.assertEqual(model.flips, [])
        self.assertEqual(model.signals, [Confirmation(15, 1)])
        self.assertEqual(
            progress(model.stage),
            ["done", "done", "current", "future", "future", "future", "future"],
        )

    def test_bearish_bias_and_a_bearish_close_through_set_direction(self):
        model = Model(60, 15, 5, swing=1)
        model.start(0, BEARISH_CLOSED, BEARISH_PRIOR)
        feed_bearish_through(model, 0)
        self.assertEqual(model.bias_text(), "Bearish")
        self.assertEqual(model.direction_text(), "Bearish")
        self.assertEqual(model.locked_bias, -1)
        self.assertEqual(model.direction, -1)
        self.assertEqual(model.stage_name(), "ARRAY")
        self.assertFalse(model.swept_low)
        self.assertFalse(model.swept_high)
        self.assertEqual(model.flips, [])
        self.assertEqual(model.signals, [Confirmation(15, -1)])

    def test_a_wick_and_an_equal_close_do_not_set_direction(self):
        model = Model(60, 15, 5, swing=1)
        model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
        model.on_mtf(Candle(0, 100, 110, 95, 98))
        model.on_mtf(Candle(15, 99, 118, 96, 100))
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.direction_text(), "None")
        self.assertEqual(model.stage_name(), "BIAS")
        model.on_mtf(Candle(30, 100, 112, 96, 110))
        self.assertEqual(model.direction_text(), "None")
        model.on_mtf(Candle(45, 112, 116, 100, 101))
        model.on_mtf(Candle(60, 101, 118, 99, 117))
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.direction_text(), "Bullish")
        self.assertEqual(model.stage_name(), "ARRAY")

    def test_a_close_through_the_nearest_candle_does_not_need_the_older_high(self):
        model = Model(60, 15, 5, swing=1)
        model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
        model.on_mtf(Candle(0, 100, 118, 95, 98))
        model.on_mtf(Candle(15, 98, 108, 96, 97))
        model.on_mtf(Candle(30, 97, 116, 96, 112))
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.direction_text(), "Bullish")
        self.assertEqual(model.stage_name(), "ARRAY")
        self.assertEqual(model.array_text(), "None")

    def test_a_close_must_clear_the_nearest_opposing_candle(self):
        model = Model(60, 15, 5, swing=1)
        model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
        model.on_mtf(Candle(0, 100, 104, 95, 98))
        model.on_mtf(Candle(15, 98, 112, 96, 97))
        model.on_mtf(Candle(30, 97, 116, 96, 108))
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.direction_text(), "None")
        self.assertEqual(model.stage_name(), "BIAS")

    def test_a_close_through_the_base_ignores_the_selloff_above_it(self):
        model = Model(60, 15, 5, swing=1)
        model.start(0, BEARISH_CLOSED, BEARISH_PRIOR)
        model.on_mtf(Candle(0, 130, 140, 118, 120))
        model.on_mtf(Candle(15, 120, 124, 100, 104))
        model.on_mtf(Candle(30, 104, 108, 96, 100))
        model.on_ltf(Candle(35, 100, 101, 80, 90))
        self.assertTrue(model.swept_low)
        model.on_mtf(Candle(45, 100, 116, 95, 114))
        self.assertEqual(model.bias_text(), "Bullish flip")
        self.assertEqual(model.direction_text(), "Bullish")
        self.assertEqual(model.stage_name(), "ARRAY")
        self.assertEqual(model.array_text(), "None")
        self.assertEqual(model.flips, [Confirmation(45, 1)])

    def test_a_candle_that_is_not_cleared_hides_the_order_block_behind_it(self):
        model = Model(60, 15, 5, swing=1)
        model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
        model.on_mtf(Candle(0, 100, 108, 95, 98))
        model.on_mtf(Candle(15, 99, 120, 97, 105))
        model.on_mtf(Candle(30, 104, 116, 100, 114))
        self.assertEqual(model.direction_text(), "None")
        self.assertEqual(model.stage_name(), "BIAS")

    def test_a_close_through_a_run_of_candles_sets_direction(self):
        model = Model(60, 15, 5, swing=1)
        model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
        model.on_mtf(Candle(0, 100, 118, 95, 98))
        model.on_mtf(Candle(15, 98, 108, 96, 97))
        model.on_mtf(Candle(30, 97, 120, 96, 119))
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.direction_text(), "Bullish")
        self.assertEqual(model.array_text(), "None")
        self.assertEqual(model.stage_name(), "ARRAY")

    def test_a_candle_from_before_the_hour_can_be_closed_through(self):
        model = Model(60, 15, 5, swing=1)
        model.start(60, BULLISH_CLOSED, BULLISH_PRIOR)
        model.on_mtf(Candle(45, 100, 110, 95, 98))
        self.assertEqual(model.direction, 0)
        model.on_mtf(Candle(60, 98, 112, 96, 111))
        self.assertEqual(model.direction_text(), "Bullish")
        self.assertEqual(model.array_text(), "None")
        self.assertEqual(model.stage_name(), "ARRAY")

    def test_the_close_through_candle_does_not_adopt_an_array(self):
        model = Model(60, 15, 5, swing=1)
        model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
        model.on_mtf(Candle(0, 108, 109, 104, 106))
        model.on_mtf(Candle(15, 107, 112, 105, 111))
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.direction_text(), "Bullish")
        self.assertEqual(model.array_text(), "None")
        self.assertEqual(model.stage_name(), "ARRAY")

    def test_no_array_is_stored_on_the_candle_that_sets_direction(self):
        model = Model(60, 15, 5, swing=1)
        model.start(60, BULLISH_CLOSED, BULLISH_PRIOR)
        model.on_mtf(Candle(60, 108, 109, 104, 106))
        model.on_mtf(Candle(75, 107, 112, 105, 111))
        self.assertEqual(model.stage_name(), "ARRAY")
        self.assertEqual(model.direction, 1)
        self.assertIsNone(model.inversion_ref)

    def test_opposite_order_block_without_a_sweep_does_not_flip(self):
        model = Model(60, 15, 5, swing=1)
        model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
        feed_bullish_through(model, 0)
        model.on_mtf(Candle(60, 100, 110, 95, 104))
        model.on_mtf(Candle(75, 102, 115, 90, 94))
        self.assertFalse(model.swept_high)
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.direction_text(), "Bullish")
        self.assertEqual(model.locked_bias, 1)
        self.assertEqual(model.flips, [])

    def test_an_opposing_order_block_after_a_high_sweep_flips_the_bias(self):
        model = Model(60, 15, 5, swing=1)
        model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
        feed_bullish_through(model, 0)
        model.on_mtf(Candle(60, 100, 110, 95, 104))
        model.on_mtf(Candle(75, 102, 125, 90, 94))
        self.assertEqual(model.bias_text(), "Bearish flip")
        self.assertEqual(model.direction_text(), "Bearish")
        self.assertEqual(model.locked_bias, 1)
        self.assertEqual(model.stage_name(), "ARRAY")
        self.assertIsNone(model.inversion_ref)
        self.assertEqual(model.flips, [Confirmation(75, -1)])

    def test_an_opposing_order_block_after_a_low_sweep_flips_the_bias(self):
        model = Model(60, 15, 5, swing=1)
        model.start(0, BEARISH_CLOSED, BEARISH_PRIOR)
        feed_bearish_through(model, 0)
        self.assertFalse(model.swept_low)
        model.on_mtf(Candle(60, 100, 108, 96, 98))
        model.on_mtf(Candle(75, 99, 112, 70, 110))
        self.assertTrue(model.swept_low)
        self.assertEqual(model.bias_text(), "Bullish flip")
        self.assertEqual(model.direction_text(), "Bullish")
        self.assertEqual(model.locked_bias, -1)
        self.assertEqual(model.stage_name(), "ARRAY")
        self.assertEqual(model.array_text(), "None")
        self.assertEqual(model.flips, [Confirmation(75, 1)])

    def test_signal_window_includes_the_start_and_end_minutes(self):
        self.assertTrue(in_signal_window(10, 10, 50))
        self.assertTrue(in_signal_window(50, 10, 50))
        self.assertTrue(in_signal_window(30, 10, 50))
        self.assertFalse(in_signal_window(9, 10, 50))
        self.assertFalse(in_signal_window(51, 10, 50))
        self.assertFalse(in_signal_window(0, 10, 50))
        self.assertFalse(in_signal_window(59, 10, 50))

    def test_signal_window_with_start_after_end_prints_nothing(self):
        self.assertFalse(in_signal_window(30, 50, 10))

    def test_a_bearish_rejection_ignores_a_bullish_order_block(self):
        prior = Candle(-60, 90, 100, 80, 90)
        closed = Candle(0, 95, 110, 85, 92)
        model = Model(60, 15, 5, swing=1)
        model.start(0, closed, prior)
        self.assertEqual(model.bias_text(), "Bearish")
        self.assertTrue(model.rejection_bias)
        model.on_mtf(Candle(0, 100, 108, 96, 98))
        model.on_ltf(Candle(10, 100, 101, 70, 90))
        self.assertTrue(model.swept_low)
        model.on_mtf(Candle(15, 99, 112, 97, 110))
        self.assertEqual(model.bias_text(), "Bearish")
        self.assertEqual(model.direction_text(), "None")
        self.assertEqual(model.flips, [])
        self.assertEqual(model.signals, [])
        model.on_mtf(Candle(30, 100, 110, 95, 108))
        model.on_mtf(Candle(45, 106, 109, 90, 94))
        self.assertEqual(model.bias_text(), "Bearish")
        self.assertEqual(model.direction_text(), "Bearish")
        self.assertEqual(model.signals, [Confirmation(45, -1)])
        self.assertEqual(model.flips, [])

    def test_a_bullish_rejection_ignores_a_bearish_order_block(self):
        prior = Candle(-60, 90, 100, 80, 90)
        closed = Candle(0, 90, 98, 70, 88)
        model = Model(60, 15, 5, swing=1)
        model.start(0, closed, prior)
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertTrue(model.rejection_bias)
        model.on_mtf(Candle(0, 100, 110, 95, 108))
        model.on_ltf(Candle(10, 100, 120, 99, 110))
        self.assertTrue(model.swept_high)
        model.on_mtf(Candle(15, 106, 109, 90, 94))
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.direction_text(), "None")
        self.assertEqual(model.signals, [])
        model.on_mtf(Candle(30, 100, 108, 96, 98))
        model.on_mtf(Candle(45, 98, 112, 97, 110))
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.direction_text(), "Bullish")
        self.assertEqual(model.signals, [Confirmation(45, 1)])
        self.assertEqual(model.flips, [])

    def test_a_bearish_rejection_flips_after_the_sell_once_the_low_is_swept(self):
        prior = Candle(-60, 90, 100, 80, 90)
        closed = Candle(0, 95, 110, 85, 92)
        model = Model(60, 15, 5, swing=1)
        model.start(0, closed, prior)
        self.assertTrue(model.rejection_bias)
        model.on_mtf(Candle(0, 100, 110, 95, 108))
        model.on_mtf(Candle(15, 106, 109, 90, 94))
        self.assertEqual(model.direction, -1)
        self.assertEqual(model.signals, [Confirmation(15, -1)])
        self.assertEqual(model.flips, [])
        feed_bullish_through(model, 30)
        self.assertFalse(model.swept_low)
        self.assertEqual(model.bias, -1)
        self.assertEqual(model.direction, -1)
        self.assertEqual(model.flips, [])
        model.on_ltf(Candle(70, 100, 101, 70, 90))
        self.assertTrue(model.swept_low)
        feed_bullish_through(model, 75)
        self.assertEqual(model.bias_text(), "Bullish flip")
        self.assertEqual(model.direction, 1)
        self.assertEqual(model.locked_bias, -1)
        self.assertFalse(model.rejection_bias)
        self.assertEqual(model.stage_name(), "ARRAY")
        self.assertEqual(model.array_text(), "None")
        self.assertEqual(model.flips, [Confirmation(90, 1)])
        self.assertEqual(model.signals, [Confirmation(15, -1), Confirmation(90, 1)])

    def test_a_bullish_rejection_flips_after_the_buy_once_the_high_is_swept(self):
        prior = Candle(-60, 90, 100, 80, 90)
        closed = Candle(0, 90, 98, 70, 88)
        model = Model(60, 15, 5, swing=1)
        model.start(0, closed, prior)
        self.assertTrue(model.rejection_bias)
        model.on_mtf(Candle(0, 94, 95, 88, 90))
        model.on_mtf(Candle(15, 90, 98, 89, 96))
        self.assertEqual(model.direction, 1)
        self.assertEqual(model.flips, [])
        model.on_mtf(Candle(30, 90, 96, 88, 94))
        model.on_mtf(Candle(45, 94, 95, 80, 84))
        self.assertFalse(model.swept_high)
        self.assertEqual(model.bias, 1)
        self.assertEqual(model.direction, 1)
        model.on_ltf(Candle(70, 100, 120, 99, 110))
        self.assertTrue(model.swept_high)
        feed_bearish_through(model, 75)
        self.assertEqual(model.bias_text(), "Bearish flip")
        self.assertEqual(model.direction, -1)
        self.assertEqual(model.locked_bias, 1)
        self.assertFalse(model.rejection_bias)
        self.assertEqual(model.flips, [Confirmation(90, -1)])
        self.assertEqual(model.signals, [Confirmation(15, 1), Confirmation(90, -1)])

    def test_a_both_sides_bearish_close_flips_after_the_sell_once_the_low_is_swept(self):
        prior = Candle(-60, 90, 100, 80, 90)
        closed = Candle(0, 90, 110, 70, 75)
        model = Model(60, 15, 5, swing=1)
        model.start(0, closed, prior)
        self.assertEqual(model.bias, -1)
        self.assertTrue(model.rejection_bias)
        feed_bullish_through(model, 0)
        model.on_ltf(Candle(25, 100, 101, 60, 90))
        self.assertTrue(model.swept_low)
        self.assertEqual(model.direction, 0)
        self.assertEqual(model.flips, [])
        feed_bearish_through(model, 30)
        self.assertEqual(model.direction, -1)
        self.assertEqual(model.bias, -1)
        self.assertEqual(model.flips, [])
        feed_bullish_through(model, 60)
        self.assertEqual(model.bias_text(), "Bullish flip")
        self.assertEqual(model.direction, 1)
        self.assertEqual(model.locked_bias, -1)
        self.assertEqual(model.stage_name(), "ARRAY")
        self.assertIsNone(model.inversion_ref)
        self.assertEqual(model.flips, [Confirmation(75, 1)])
        self.assertEqual(model.signals, [Confirmation(45, -1), Confirmation(75, 1)])

    def test_a_both_sides_sweep_stays_neutral_unless_the_close_leaves_the_prior_candle(self):
        prior = Candle(-60, 90, 100, 80, 90)
        inside = Candle(0, 90, 110, 70, 90)
        self.assertEqual(htf_bias(inside, prior), 0)
        self.assertTrue(htf_swept_both(inside, prior))
        model = Model(60, 15, 5, swing=1)
        model.start(0, inside, prior)
        self.assertEqual(model.bias_text(), "None")
        self.assertEqual(model.stage_name(), "WAITING")
        self.assertTrue(model.first_ob_bias)
        self.assertFalse(model.rejection_bias)
        above_open = Candle(0, 95, 110, 70, 95)
        self.assertGreater(above_open.close, prior.open)
        self.assertEqual(htf_bias(above_open, prior), 0)
        model.start(60, above_open, prior)
        self.assertEqual(model.bias, 0)
        self.assertTrue(model.first_ob_bias)
        self.assertFalse(model.rejection_bias)
        below_open = Candle(0, 85, 110, 70, 85)
        self.assertLess(below_open.close, prior.open)
        model.start(120, below_open, prior)
        self.assertEqual(model.bias, 0)
        self.assertTrue(model.first_ob_bias)
        self.assertFalse(model.rejection_bias)
        on_high = Candle(0, 95, 110, 70, 100)
        self.assertEqual(htf_bias(on_high, prior), 1)
        self.assertTrue(htf_rejection(on_high, prior))
        model.start(180, on_high, prior)
        self.assertEqual(model.bias, 0)
        self.assertEqual(model.stage_name(), "WAITING")
        self.assertTrue(model.first_ob_bias)
        self.assertFalse(model.rejection_bias)
        on_low = Candle(0, 85, 110, 70, 80)
        self.assertEqual(htf_bias(on_low, prior), -1)
        model.start(240, on_low, prior)
        self.assertEqual(model.bias, 0)
        self.assertTrue(model.first_ob_bias)
        self.assertFalse(model.rejection_bias)
        below = Candle(0, 90, 110, 70, 75)
        self.assertEqual(htf_bias(below, prior), -1)
        model.start(300, below, prior)
        self.assertEqual(model.bias, -1)
        self.assertEqual(model.locked_bias, -1)
        self.assertEqual(model.stage_name(), "BIAS")
        self.assertFalse(model.first_ob_bias)
        self.assertTrue(model.rejection_bias)
        above = Candle(0, 90, 110, 70, 105)
        self.assertEqual(htf_bias(above, prior), 1)
        model.start(360, above, prior)
        self.assertEqual(model.bias, 1)
        self.assertEqual(model.locked_bias, 1)
        self.assertFalse(model.first_ob_bias)
        self.assertTrue(model.rejection_bias)

    def test_a_both_sides_close_inside_but_above_the_prior_open_takes_either_order_block(self):
        prior = Candle(-60, 90, 100, 80, 90)
        closed = Candle(0, 95, 110, 70, 95)
        model = Model(60, 15, 5, swing=1)
        model.start(0, closed, prior)
        self.assertEqual(model.bias, 0)
        self.assertTrue(model.first_ob_bias)
        feed_bearish_through(model, 0)
        self.assertEqual(model.bias, -1)
        self.assertEqual(model.locked_bias, -1)
        self.assertEqual(model.direction, -1)
        self.assertEqual(model.signals, [Confirmation(15, -1)])
        self.assertEqual(model.flips, [])
        self.assertFalse(model.first_ob_bias)

    def test_the_first_bullish_order_block_after_a_both_sides_sweep_sets_the_signal(self):
        prior = Candle(-60, 90, 100, 80, 90)
        closed = Candle(0, 90, 110, 70, 90)
        model = Model(60, 15, 5, swing=1)
        model.start(0, closed, prior)
        feed_bullish_through(model, 0)
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.locked_bias, 1)
        self.assertEqual(model.direction_text(), "Bullish")
        self.assertEqual(model.direction, 1)
        self.assertEqual(model.stage_name(), "ARRAY")
        self.assertEqual(model.array_text(), "None")
        self.assertIsNone(model.inversion_ref)
        self.assertEqual(model.signals, [Confirmation(15, 1)])
        self.assertEqual(model.flips, [])
        self.assertFalse(model.first_ob_bias)
        self.assertFalse(model.rejection_bias)

    def test_the_first_bearish_order_block_after_a_both_sides_sweep_sets_the_signal(self):
        prior = Candle(-60, 90, 100, 80, 90)
        closed = Candle(0, 90, 110, 70, 75)
        model = Model(60, 15, 5, swing=1)
        model.start(0, closed, prior)
        self.assertEqual(htf_bias(closed, prior), -1)
        self.assertEqual(model.bias, -1)
        self.assertTrue(model.rejection_bias)
        self.assertFalse(model.first_ob_bias)
        feed_bullish_through(model, 0)
        self.assertEqual(model.direction, 0)
        self.assertEqual(model.signals, [])
        feed_bearish_through(model, 30)
        self.assertEqual(model.bias_text(), "Bearish")
        self.assertEqual(model.locked_bias, -1)
        self.assertEqual(model.direction_text(), "Bearish")
        self.assertEqual(model.stage_name(), "ARRAY")
        self.assertEqual(model.array_text(), "None")
        self.assertEqual(model.signals, [Confirmation(45, -1)])
        self.assertEqual(model.flips, [])
        self.assertFalse(model.first_ob_bias)

    def test_an_empty_bias_without_a_both_sides_sweep_ignores_order_blocks(self):
        prior = Candle(-60, 100, 100, 80, 90)
        inside = Candle(0, 90, 95, 85, 92)
        self.assertEqual(htf_bias(inside, prior), 0)
        self.assertFalse(htf_swept_both(inside, prior))
        model = Model(60, 15, 5, swing=1)
        model.start(0, inside, prior)
        self.assertFalse(model.first_ob_bias)
        feed_bullish_through(model, 0)
        feed_bearish_through(model, 30)
        self.assertEqual(model.bias_text(), "None")
        self.assertEqual(model.direction_text(), "None")
        self.assertEqual(model.signals, [])
        self.assertEqual(model.stage_name(), "WAITING")
        touched_high = Candle(0, 90, 110, 85, 100)
        self.assertEqual(htf_bias(touched_high, prior), 0)
        self.assertFalse(htf_swept_both(touched_high, prior))
        model.start(60, touched_high, prior)
        self.assertFalse(model.first_ob_bias)
        feed_bullish_through(model, 60)
        self.assertEqual(model.bias, 0)
        self.assertEqual(model.direction, 0)
        self.assertEqual(model.signals, [])

    def test_after_the_first_order_block_the_normal_flip_rules_return(self):
        prior = Candle(-60, 90, 100, 80, 90)
        closed = Candle(0, 90, 120, 70, 90)
        model = Model(60, 15, 5, swing=1)
        model.start(0, closed, prior)
        feed_bullish_through(model, 0)
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertFalse(model.swept_high)
        model.on_mtf(Candle(60, 100, 110, 95, 104))
        model.on_mtf(Candle(75, 102, 115, 90, 94))
        self.assertFalse(model.swept_high)
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.direction, 1)
        self.assertEqual(model.flips, [])
        model.on_mtf(Candle(90, 100, 110, 95, 104))
        model.on_mtf(Candle(105, 102, 125, 90, 94))
        self.assertEqual(model.bias_text(), "Bearish flip")
        self.assertEqual(model.direction_text(), "Bearish")
        self.assertEqual(model.stage_name(), "ARRAY")
        self.assertIsNone(model.inversion_ref)
        self.assertEqual(model.flips, [Confirmation(105, -1)])
        self.assertEqual(model.signals, [Confirmation(15, 1), Confirmation(105, -1)])

    def test_an_ltf_sweep_lets_the_later_order_block_flip(self):
        model = Model(60, 15, 5, swing=1)
        model.start(60, BEARISH_CLOSED, BEARISH_PRIOR)
        model.on_mtf(Candle(60, 100, 110, 95, 108))
        model.on_mtf(Candle(75, 106, 109, 90, 94))
        model.on_ltf(Candle(90, 100, 101, 70, 90))
        self.assertTrue(model.swept_low)
        self.assertEqual(model.bias_text(), "Bearish")
        self.assertEqual(model.direction_text(), "Bearish")
        model.on_mtf(Candle(90, 100, 108, 96, 98))
        model.on_mtf(Candle(105, 99, 112, 97, 110))
        self.assertEqual(model.bias_text(), "Bullish flip")
        self.assertEqual(model.direction_text(), "Bullish")
        self.assertEqual(model.stage_name(), "ARRAY")
        self.assertEqual(model.flips, [Confirmation(105, 1)])

    def test_an_ltf_close_does_not_change_bias_or_direction(self):
        model = Model(60, 15, 5, swing=1)
        model.start(60, BEARISH_CLOSED, BEARISH_PRIOR)
        feed_bearish_through(model, 60)
        model.on_ltf(Candle(90, 100, 110, 99, 105))
        model.on_ltf(Candle(95, 105, 124, 100, 108))
        model.on_ltf(Candle(100, 108, 116, 104, 110))
        self.assertEqual(model.swing_high, 124)
        model.on_ltf(Candle(105, 110, 130, 70, 125))
        self.assertTrue(model.swept_low)
        self.assertEqual(model.bias_text(), "Bearish")
        self.assertEqual(model.direction_text(), "Bearish")
        self.assertEqual(model.locked_bias, -1)
        self.assertEqual(model.stage_name(), "ARRAY")

    def test_a_new_htf_candle_recomputes_bias_and_clears_direction(self):
        model = Model(60, 15, 5, swing=1)
        model.start(60, BEARISH_CLOSED, BEARISH_PRIOR)
        feed_bearish_through(model, 60)
        self.assertEqual(model.direction, -1)
        model.start(120, BULLISH_CLOSED, BULLISH_PRIOR)
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.direction_text(), "None")
        self.assertEqual(model.locked_bias, 1)
        self.assertEqual(model.stage_name(), "BIAS")
        self.assertIsNone(model.inversion_ref)
        self.assertFalse(model.swept_high)
        self.assertFalse(model.swept_low)

    def test_a_fair_value_gap_does_not_flip_the_bias(self):
        model = Model(60, 15, 5, swing=1)
        model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
        feed_bullish_through(model, 0)
        model.on_mtf(Candle(60, 125, 130, 120, 122))
        model.on_mtf(Candle(75, 110, 112, 100, 101))
        model.on_mtf(Candle(90, 112, 113, 108, 109))
        self.assertTrue(model.swept_high)
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.direction_text(), "Bullish")
        self.assertEqual(model.locked_bias, 1)
        self.assertEqual(model.array_text(), "Bearish FVG")

    def test_an_opposite_order_block_without_the_sweep_keeps_a_pending_inversion(self):
        model = bullish_ob_ready(max_candles=20, swing=1)
        self.assertEqual(model.stage_name(), "INVERSION")
        self.assertEqual(model.inversion_ref, 108)
        model.on_mtf(Candle(90, 100, 110, 95, 104))
        model.on_mtf(Candle(105, 102, 115, 90, 94))
        self.assertFalse(model.swept_high)
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.direction_text(), "Bullish")
        self.assertEqual(model.stage_name(), "INVERSION")
        self.assertEqual(model.inversion_ref, 108)

    def test_an_opposing_order_block_after_the_sweep_cancels_a_pending_inversion(self):
        model = bullish_ob_ready(max_candles=20, swing=1)
        model.on_mtf(Candle(90, 100, 110, 95, 104))
        model.on_mtf(Candle(105, 102, 125, 90, 94))
        self.assertEqual(model.bias_text(), "Bearish flip")
        self.assertEqual(model.direction_text(), "Bearish")
        self.assertEqual(model.stage_name(), "ARRAY")
        self.assertEqual(model.elapsed, 0)
        self.assertIsNone(model.inversion_ref)
        self.assertEqual(model.flips, [Confirmation(105, -1)])

    def test_a_same_side_close_through_does_not_reset_direction(self):
        model = bullish_ob_ready(max_candles=20, swing=1)
        model.on_mtf(Candle(90, 106, 107, 100, 101))
        model.on_mtf(Candle(105, 101, 112, 100, 108))
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.direction_text(), "Bullish")
        self.assertEqual(model.stage_name(), "INVERSION")
        self.assertEqual(model.inversion_ref, 108)
        self.assertEqual(model.elapsed, 2)

    def test_direction_stays_empty_until_the_candles_are_closed_through(self):
        model = Model(60, 15, 5, swing=1)
        model.start(60, BULLISH_CLOSED, BULLISH_PRIOR)
        self.assertEqual(model.stage_name(), "BIAS")
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.direction_text(), "None")
        model.on_mtf(Candle(60, 100, 110, 95, 98))
        model.on_mtf(Candle(75, 98, 116, 96, 97))
        self.assertEqual(model.direction_text(), "None")
        self.assertEqual(model.stage_name(), "BIAS")
        model.on_mtf(Candle(90, 97, 118, 95, 117))
        self.assertEqual(model.bias_text(), "Bullish")
        self.assertEqual(model.direction_text(), "Bullish")
        self.assertEqual(model.stage_name(), "ARRAY")
        self.assertEqual(model.array_text(), "None")
        self.assertEqual(progress(model.stage)[1], "done")
        self.assertEqual(progress(model.stage)[2], "current")

    def test_ob_reference_is_the_anchor_open_and_stays_immutable(self):
        model = bullish_ob_ready()
        self.assertEqual(model.stage_name(), "INVERSION")
        self.assertEqual(model.array_text(), "Bearish OB")
        self.assertEqual(model.inversion_ref, 108)
        self.assertEqual(model.top, 109)
        self.assertEqual(model.bottom, 104)
        self.assertEqual(model.countdown(), "0/3 elapsed")
        self.assertEqual(model.source_open, 60)
        model.on_mtf(Candle(90, 200, 201, 100, 107))
        self.assertEqual(model.inversion_ref, 108)
        self.assertEqual(model.elapsed, 1)
        self.assertEqual(model.stage_name(), "INVERSION")
        self.assertEqual(model.countdown(), "1/3 elapsed")

    def test_equal_close_and_wick_do_not_invert(self):
        model = bullish_ob_ready()
        model.on_mtf(Candle(90, 107, 130, 100, 108))
        self.assertEqual(model.stage_name(), "INVERSION")
        self.assertEqual(model.elapsed, 1)

    def test_inversion_on_a_later_mtf_close_advances_to_retest(self):
        model = bullish_ob_ready()
        model.on_mtf(Candle(90, 107, 112, 106, 109))
        self.assertEqual(model.stage_name(), "RETEST")
        self.assertEqual(model.expiration_text(), "Inverted")
        self.assertEqual(model.countdown(), "1/3 elapsed")
        self.assertEqual(model.inversion_ref, 108)
        self.assertEqual(model.retest_text(), "Waiting")
        self.assertEqual(model.mss_text(), "—")

    def test_identification_candle_is_not_an_inversion_candle(self):
        model = bullish_ob_ready()
        self.assertEqual(model.elapsed, 0)
        self.assertEqual(model.stage_name(), "INVERSION")

    def test_ltf_candles_do_not_advance_the_mtf_countdown(self):
        model = bullish_ob_ready()
        for offset in range(60, 90, 5):
            model.on_ltf(Candle(offset, 100, 101, 99, 100))
        self.assertEqual(model.elapsed, 0)
        self.assertEqual(model.stage_name(), "INVERSION")

    def test_third_completed_mtf_candle_expires_and_returns_to_array_search(self):
        model = bullish_ob_ready(max_candles=3)
        model.on_mtf(Candle(90, 107, 112, 100, 107))
        model.on_mtf(Candle(105, 107, 112, 100, 107))
        self.assertEqual(model.stage_name(), "INVERSION")
        self.assertEqual(model.elapsed, 2)
        model.on_mtf(Candle(120, 107, 130, 100, 108))
        self.assertEqual(model.stage_name(), "ARRAY")
        self.assertEqual(model.bias, 1)
        self.assertEqual(model.expiration_text(), "Expired")
        self.assertEqual(model.countdown(), "3/3 elapsed")
        self.assertIsNone(model.inversion_ref)
        self.assertEqual(model.array_text(), "None")
        self.assertEqual(
            progress(model.stage),
            ["done", "done", "current", "future", "future", "future", "future"],
        )

    def test_inverting_on_the_final_allowed_candle_does_not_expire(self):
        model = bullish_ob_ready(max_candles=3)
        model.on_mtf(Candle(90, 108, 109, 107, 107))
        model.on_mtf(Candle(105, 108, 109, 107, 107))
        model.on_mtf(Candle(120, 108, 130, 107, 109))
        self.assertEqual(model.stage_name(), "RETEST")
        self.assertEqual(model.expiration_text(), "Inverted")
        self.assertEqual(model.elapsed, 3)

    def test_expired_array_cannot_complete_a_retest_or_setup(self):
        model = bullish_ob_ready(max_candles=1)
        arm_bullish_swing(model)
        model.on_mtf(Candle(90, 108, 130, 107, 107))
        self.assertEqual(model.expiration_text(), "Expired")
        model.on_ltf(Candle(105, 108, 110, 104, 109))
        model.on_ltf(Candle(110, 109, 120, 108, 115))
        self.assertEqual(model.confirmations, [])
        self.assertEqual(model.stage_name(), "ARRAY")

    def test_new_array_after_expiration_uses_its_own_reference(self):
        model = bullish_ob_ready(max_candles=1)
        model.on_mtf(Candle(90, 108, 109, 100, 107))
        self.assertEqual(model.expiration_text(), "Expired")
        model.on_mtf(Candle(105, 106, 112, 105, 110))
        self.assertEqual(model.stage_name(), "INVERSION")
        self.assertEqual(model.array_text(), "Bearish OB")
        self.assertEqual(model.inversion_ref, 108)
        self.assertEqual(model.countdown(), "0/1 elapsed")
        self.assertEqual(model.expiration_text(), "Waiting")

    def test_a_second_array_is_ignored_while_inversion_is_pending(self):
        model = bullish_ob_ready()
        model.on_mtf(Candle(90, 108, 109, 100, 107))
        self.assertEqual(model.inversion_ref, 108)
        self.assertEqual(model.elapsed, 1)
        self.assertEqual(model.stage_name(), "INVERSION")

    def test_fvg_reference_is_the_third_candle_open(self):
        model = Model(60, 15, 5)
        model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
        feed_bullish_through(model, 0)
        model.on_mtf(Candle(60, 125, 130, 120, 122))
        model.on_mtf(Candle(75, 110, 112, 100, 101))
        model.on_mtf(Candle(90, 112, 113, 108, 109))
        self.assertEqual(model.stage_name(), "INVERSION")
        self.assertEqual(model.array_text(), "Bearish FVG")
        self.assertEqual(model.inversion_ref, 112)
        self.assertEqual(model.top, 120)
        self.assertEqual(model.bottom, 113)
        self.assertEqual(model.source_open, 90)

    def test_ob_is_preferred_when_ob_and_fvg_confirm_together(self):
        model = Model(60, 15, 5)
        model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
        feed_bullish_through(model, 0)
        model.on_mtf(Candle(60, 113, 114, 110, 111))
        model.on_mtf(Candle(75, 104, 105, 100, 101))
        model.on_mtf(Candle(90, 102, 109, 101, 108))
        self.assertEqual(model.array_text(), "Bearish OB")
        self.assertEqual(model.inversion_ref, 104)
        self.assertEqual(model.top, 105)
        self.assertEqual(model.bottom, 100)

    def test_anchor_from_the_previous_htf_period_is_rejected(self):
        model = Model(60, 15, 5)
        model.start(60, BULLISH_CLOSED, BULLISH_PRIOR)
        feed_bullish_through(model, -200)
        model.on_mtf(Candle(45, 108, 109, 104, 106))
        model.on_mtf(Candle(60, 107, 112, 105, 111))
        self.assertEqual(model.stage_name(), "ARRAY")
        self.assertIsNone(model.inversion_ref)

    def test_retest_then_later_mss_confirms_once(self):
        model = bullish_ob_ready(swing=1)
        arm_bullish_swing(model)
        model.on_mtf(Candle(90, 107, 112, 106, 109))
        self.assertEqual(model.stage_name(), "RETEST")
        model.on_ltf(Candle(100, 108, 112, 107, 111))
        self.assertEqual(model.stage_name(), "RETEST")
        model.on_ltf(Candle(105, 108, 110, 107, 115))
        self.assertEqual(model.stage_name(), "MSS/BOS")
        self.assertEqual(model.retest_text(), "Confirmed")
        self.assertEqual(model.mss_text(), "Waiting")
        self.assertEqual(model.confirmations, [])
        model.on_ltf(Candle(110, 108, 116, 107, 115))
        self.assertEqual(model.stage_name(), "CONFIRMED")
        self.assertEqual(model.mss_text(), "Confirmed")
        self.assertEqual(model.confirmations, [Confirmation(110, 1)])
        self.assertEqual(progress(model.stage), ["done"] * 6 + ["current"])
        model.on_mtf(Candle(120, 108, 109, 100, 107))
        model.on_ltf(Candle(115, 108, 120, 100, 119))
        self.assertEqual(model.confirmations, [Confirmation(110, 1)])
        self.assertEqual(model.stage_name(), "CONFIRMED")

    def test_bearish_sequence_inverts_below_the_original_open(self):
        model = Model(60, 15, 5, swing=1)
        model.start(0, BEARISH_CLOSED, BEARISH_PRIOR)
        feed_bearish_through(model, 0)
        model.on_mtf(Candle(60, 100, 106, 99, 104))
        model.on_mtf(Candle(75, 103, 104, 97, 98))
        self.assertEqual(model.array_text(), "Bullish OB")
        self.assertEqual(model.inversion_ref, 100)
        model.on_mtf(Candle(90, 101, 102, 98, 100))
        self.assertEqual(model.stage_name(), "INVERSION")
        model.on_mtf(Candle(105, 100, 101, 97, 99))
        self.assertEqual(model.stage_name(), "RETEST")
        model.on_ltf(Candle(60, 100, 101, 100, 100))
        model.on_ltf(Candle(65, 100, 101, 80, 81))
        model.on_ltf(Candle(70, 100, 110, 90, 100))
        model.on_ltf(Candle(120, 100, 105, 99, 104))
        self.assertEqual(model.stage_name(), "MSS/BOS")
        model.on_ltf(Candle(125, 100, 101, 79, 79))
        self.assertEqual(model.stage_name(), "CONFIRMED")
        self.assertEqual(model.confirmations, [Confirmation(125, -1)])

    def test_duplicate_mtf_candle_does_not_double_count(self):
        model = bullish_ob_ready()
        candle = Candle(90, 108, 109, 100, 107)
        model.on_mtf(candle)
        model.on_mtf(candle)
        self.assertEqual(model.elapsed, 1)

    def test_htf_reset_drops_a_pending_array(self):
        model = bullish_ob_ready()
        model.start(120, Candle(60, 130, 131, 95, 100), BULLISH_CLOSED)
        self.assertEqual(model.stage_name(), "BIAS")
        self.assertEqual(model.bias_text(), "Bearish")
        self.assertEqual(model.direction_text(), "None")
        self.assertEqual(model.locked_bias, -1)
        self.assertIsNone(model.inversion_ref)
        self.assertEqual(model.expiration_text(), "None")
        self.assertEqual(model.countdown(), "—")

    def test_boundary_mss_is_kept_and_then_the_new_htf_period_starts(self):
        model = bullish_ob_ready(swing=1)
        arm_bullish_swing(model)
        model.on_mtf(Candle(90, 107, 112, 106, 109))
        model.on_ltf(Candle(105, 108, 110, 107, 109))
        self.assertEqual(model.stage_name(), "MSS/BOS")
        apply_chart_update(
            model,
            120,
            Candle(60, 130, 140, 95, 100),
            BULLISH_CLOSED,
            Candle(105, 109, 111, 107, 108),
            Candle(115, 108, 116, 107, 115),
        )
        self.assertEqual(model.confirmations, [Confirmation(115, 1)])
        self.assertEqual(model.stage_name(), "BIAS")
        self.assertEqual(model.bias, -1)
        self.assertEqual(model.direction, 0)
        self.assertIsNone(model.inversion_ref)

    def test_new_period_ltf_cannot_retest_the_previous_array(self):
        model = bullish_ob_ready(swing=1)
        model.on_mtf(Candle(90, 107, 112, 106, 109))
        self.assertEqual(model.stage_name(), "RETEST")
        apply_chart_update(
            model,
            120,
            Candle(60, 121, 130, 118, 125),
            BULLISH_CLOSED,
            Candle(105, 108, 110, 106, 107),
            Candle(120, 108, 110, 104, 109),
        )
        self.assertEqual(model.confirmations, [])
        self.assertEqual(model.stage_name(), "BIAS")
        self.assertEqual(model.bias, 1)
        self.assertEqual(model.direction, 0)
        self.assertIsNone(model.top)

    def test_a_buy_stop_is_the_swing_before_the_broken_high(self):
        model = bullish_ob_ready(swing=1)
        self.assertIsNone(model.stop_loss)
        model.on_ltf(Candle(60, 100, 100, 90, 96))
        model.on_ltf(Candle(65, 100, 108, 80, 100))
        model.on_ltf(Candle(70, 100, 112, 96, 108))
        self.assertEqual(model.swing_low, 80)
        model.on_ltf(Candle(75, 100, 120, 110, 118))
        model.on_ltf(Candle(80, 100, 115, 112, 114))
        self.assertEqual(model.swing_high, 120)
        model.on_ltf(Candle(85, 100, 114, 105, 110))
        model.on_ltf(Candle(88, 100, 112, 108, 110))
        self.assertEqual(model.swing_low, 105)
        model.on_mtf(Candle(90, 107, 112, 106, 109))
        self.assertEqual(model.stage_name(), "RETEST")
        model.on_ltf(Candle(110, 108, 109, 104, 108))
        self.assertEqual(model.stage_name(), "MSS/BOS")
        self.assertEqual(model.swing_high, 120)
        model.on_ltf(Candle(115, 120, 130, 110, 125))
        self.assertEqual(model.confirmations, [Confirmation(115, 1)])
        self.assertEqual(model.stop_loss, 80)
        model.on_ltf(Candle(120, 126, 128, 112, 127))
        self.assertEqual(model.stop_loss, 80)
        model.on_mtf(Candle(120, 100, 110, 95, 104))
        model.on_mtf(Candle(135, 102, 115, 90, 94))
        self.assertEqual(model.direction, -1)
        self.assertEqual(model.stop_loss, 130)
        self.assertEqual(model.stop_time, 115)

    def test_a_sell_stop_is_the_swing_before_the_broken_low(self):
        model = Model(60, 15, 5, swing=1)
        model.start(0, BEARISH_CLOSED, BEARISH_PRIOR)
        feed_bearish_through(model, 0)
        self.assertIsNone(model.stop_loss)
        model.on_mtf(Candle(60, 100, 112, 104, 108))
        model.on_mtf(Candle(75, 106, 108, 98, 103))
        self.assertEqual(model.stage_name(), "INVERSION")
        model.on_ltf(Candle(60, 100, 100, 90, 96))
        model.on_ltf(Candle(65, 100, 140, 100, 130))
        model.on_ltf(Candle(70, 100, 110, 95, 100))
        self.assertEqual(model.swing_high, 140)
        model.on_ltf(Candle(75, 100, 112, 70, 100))
        model.on_ltf(Candle(80, 100, 108, 90, 96))
        self.assertEqual(model.swing_low, 70)
        model.on_ltf(Candle(85, 100, 130, 100, 120))
        model.on_ltf(Candle(90, 100, 120, 105, 112))
        self.assertEqual(model.swing_high, 130)
        model.on_mtf(Candle(90, 104, 107, 96, 99))
        self.assertEqual(model.stage_name(), "RETEST")
        model.on_ltf(Candle(110, 108, 110, 104, 106))
        self.assertEqual(model.stage_name(), "MSS/BOS")
        model.on_ltf(Candle(115, 100, 108, 60, 65))
        self.assertEqual(model.confirmations, [Confirmation(115, -1)])
        self.assertEqual(model.stop_loss, 140)

    def test_a_sell_signal_marks_the_stop_before_the_latest_swing_low(self):
        model = Model(60, 15, 5, swing=1)
        model.on_ltf(Candle(0, 100, 100, 90, 96))
        model.on_ltf(Candle(5, 100, 140, 100, 130))
        model.on_ltf(Candle(10, 100, 110, 95, 100))
        self.assertEqual(model.swing_high, 140)
        model.on_ltf(Candle(15, 100, 112, 70, 100))
        model.on_ltf(Candle(20, 100, 108, 90, 96))
        self.assertEqual(model.swing_low, 70)
        model.on_ltf(Candle(25, 100, 130, 100, 120))
        model.on_ltf(Candle(30, 100, 120, 105, 112))
        self.assertEqual(model.swing_high, 130)
        model.start(60, BEARISH_CLOSED, BEARISH_PRIOR)
        feed_bearish_through(model, 60)
        self.assertEqual(model.direction, -1)
        self.assertEqual(model.stage_name(), "ARRAY")
        self.assertEqual(model.confirmations, [])
        self.assertEqual(model.stop_loss, 130)
        self.assertEqual(model.stop_time, 25)

    def test_a_break_with_no_earlier_swing_has_no_stop(self):
        model = bullish_ob_ready(swing=1)
        model.on_ltf(Candle(60, 100, 100, 90, 96))
        model.on_ltf(Candle(65, 100, 130, 95, 120))
        model.on_ltf(Candle(70, 100, 110, 92, 100))
        self.assertEqual(model.swing_high, 130)
        self.assertIsNone(model.swing_low)
        model.on_mtf(Candle(90, 107, 112, 106, 109))
        model.on_ltf(Candle(110, 108, 109, 104, 108))
        model.on_ltf(Candle(115, 120, 140, 110, 135))
        self.assertEqual(model.confirmations, [Confirmation(115, 1)])
        self.assertIsNone(model.stop_loss)

    def _arm_target_swings(self, model: Model) -> None:
        model.on_target(Candle(0, 100, 100, 90, 96))
        model.on_target(Candle(15, 100, 150, 100, 140))
        model.on_target(Candle(30, 100, 110, 95, 105))
        model.on_target(Candle(45, 100, 120, 80, 90))
        model.on_target(Candle(60, 100, 125, 100, 110))

    def test_a_bullish_signal_targets_the_previous_swing_high(self):
        model = Model(60, 15, 5, swing=1, target_ms=15)
        self._arm_target_swings(model)
        self.assertEqual(model.target_swings, [(1, 150, 15), (-1, 80, 45)])
        model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
        feed_bullish_through(model, 0)
        self.assertEqual(model.direction, 1)
        self.assertEqual(model.take_profit, 150)
        self.assertEqual(model.take_profit_time, 15)
        model.on_target(Candle(75, 100, 180, 90, 170))
        model.on_target(Candle(90, 100, 100, 90, 95))
        self.assertEqual(model.take_profit, 150)
        self.assertEqual(model.take_profit_time, 15)

    def test_a_target_skips_the_swing_already_traded_by_the_signal(self):
        model = Model(60, 15, 5, swing=1, target_ms=15)
        model.on_target(Candle(0, 100, 100, 90, 96))
        model.on_target(Candle(15, 100, 160, 100, 150))
        model.on_target(Candle(30, 100, 110, 95, 105))
        model.on_target(Candle(45, 100, 100, 90, 95))
        model.on_target(Candle(60, 100, 120, 100, 110))
        model.on_target(Candle(75, 100, 105, 95, 100))
        model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
        model.on_mtf(Candle(0, 100, 130, 95, 98))
        model.on_mtf(Candle(15, 98, 140, 96, 135))
        self.assertEqual(model.direction, 1)
        self.assertEqual(model.take_profit, 160)
        self.assertEqual(model.take_profit_time, 15)
        model = Model(60, 15, 5, swing=1, target_ms=15)
        model.on_target(Candle(0, 100, 110, 100, 105))
        model.on_target(Candle(15, 100, 110, 70, 100))
        model.on_target(Candle(30, 100, 110, 100, 105))
        model.on_target(Candle(45, 100, 110, 100, 105))
        model.on_target(Candle(60, 100, 110, 96, 100))
        model.on_target(Candle(75, 100, 110, 100, 105))
        model.start(0, BEARISH_CLOSED, BEARISH_PRIOR)
        feed_bearish_through(model, 0)
        self.assertEqual(model.direction, -1)
        self.assertEqual(model.take_profit, 70)
        self.assertEqual(model.take_profit_time, 15)

    def test_a_bearish_signal_targets_the_previous_swing_low(self):
        model = Model(60, 15, 5, swing=1, target_ms=15)
        self._arm_target_swings(model)
        model.start(0, BEARISH_CLOSED, BEARISH_PRIOR)
        feed_bearish_through(model, 0)
        self.assertEqual(model.direction, -1)
        self.assertEqual(model.take_profit, 80)
        self.assertEqual(model.take_profit_time, 45)

    def test_a_signal_without_a_confirmed_target_swing_has_no_take_profit(self):
        model = Model(60, 15, 5, swing=1, target_ms=15)
        model.on_target(Candle(0, 100, 150, 90, 140))
        model.on_target(Candle(15, 100, 110, 95, 100))
        model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
        feed_bullish_through(model, 0)
        self.assertEqual(model.direction, 1)
        self.assertIsNone(model.take_profit)
        self.assertIsNone(model.take_profit_time)

    def test_timeframe_target_off_leaves_the_take_profit_empty(self):
        model = Model(60, 15, 5, swing=1)
        model.on_target(Candle(0, 100, 100, 90, 96))
        model.on_target(Candle(15, 100, 150, 100, 140))
        model.on_target(Candle(30, 100, 110, 95, 105))
        self.assertEqual(model.target_swings, [])
        model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
        feed_bullish_through(model, 0)
        self.assertIsNone(model.take_profit)

    def test_a_new_period_clears_the_take_profit_until_the_next_signal(self):
        model = Model(60, 15, 5, swing=1, target_ms=15)
        self._arm_target_swings(model)
        model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
        feed_bullish_through(model, 0)
        self.assertEqual(model.take_profit, 150)
        model.start(60, BULLISH_CLOSED, BULLISH_PRIOR)
        self.assertIsNone(model.take_profit)
        self.assertIsNone(model.take_profit_time)
        self.assertEqual(model.target_swings, [(1, 150, 15), (-1, 80, 45)])
        feed_bullish_through(model, 60)
        self.assertEqual(model.take_profit, 150)
        self.assertEqual(model.take_profit_time, 15)

    def test_a_flip_replaces_the_take_profit_with_the_other_swing(self):
        model = Model(60, 15, 5, swing=1, target_ms=15)
        self._arm_target_swings(model)
        model.start(0, BULLISH_CLOSED, BULLISH_PRIOR)
        feed_bullish_through(model, 0)
        self.assertEqual(model.take_profit, 150)
        model.on_mtf(Candle(60, 100, 110, 95, 104))
        model.on_mtf(Candle(75, 102, 125, 90, 94))
        self.assertEqual(model.bias_text(), "Bearish flip")
        self.assertEqual(model.direction, -1)
        self.assertEqual(model.take_profit, 80)
        self.assertEqual(model.take_profit_time, 45)

    def test_progress_tracks_every_stage_index(self):
        for stage in range(len(STAGE_NAMES)):
            colors = progress(stage)
            self.assertEqual(colors[stage], "current")
            self.assertTrue(all(color == "done" for color in colors[:stage]))
            self.assertTrue(all(color == "future" for color in colors[stage + 1 :]))


class SourceContractTests(unittest.TestCase):
    def test_pine_indicator_matches_the_execution_contract(self):
        source = PINE.read_text(encoding="utf-8")
        self.assertIn("//@version=6", source)
        self.assertIn('indicator("HTF Array Inversion"', source)
        self.assertNotIn("strategy(", source)
        self.assertNotIn("box.new", source)
        self.assertEqual(source.count("line.new"), 2)
        self.assertNotIn('label.new(candleTime, this.stopLoss, "SL"', source)
        self.assertNotIn("extend.right", source)
        self.assertIn('input.color(color.red, "Stop Line Color"', source)
        self.assertIn('input.string("Solid", "Stop Line Type"', source)
        self.assertIn("line.style_solid", source)
        self.assertIn("line.style_dashed", source)
        self.assertIn("line.style_dotted", source)
        self.assertIn("color = stopColor", source)
        self.assertIn("style = stopStyle", source)
        self.assertIn("anchor + ltfMs * 2", source)
        self.assertIn("this.stopTime", source)
        self.assertIn('input.string("15M", "Timeframe Target"', source)
        self.assertIn('input.color(color.green, "Take Profit Line Color"', source)
        self.assertIn('input.string("Solid", "Take Profit Line Type"', source)
        self.assertIn("color = targetColor", source)
        self.assertIn("style = targetStyle", source)
        self.assertIn("anchor + targetMs * 2", source)
        self.assertIn("this.takeProfitTime", source)
        self.assertNotIn("plotshape", source)
        self.assertNotIn("plot(", source)
        self.assertIn("Maximum Candles to Wait for Inversion", source)
        self.assertIn("HTF (Directional Bias)", source)
        self.assertIn("MTF (Opposing Array and Inversion)", source)
        self.assertIn("LTF (Retest and MSS/BOS)", source)
        self.assertIn("Show Confirmed Setup Marker", source)
        self.assertIn("Signal Window Start", source)
        self.assertIn("Signal Window End", source)
        self.assertIn("minute(candleTime, syminfo.timezone)", source)
        self.assertEqual(source.count("f_inSignalWindow(candleTime)"), 4)
        self.assertIn("lookahead = barmerge.lookahead_on", source)
        self.assertIn("time[1]", source)
        self.assertIn("label.new", source)
        self.assertIn("this.f_markFlip(t, l, h)", source)
        self.assertIn("not this.rejectionBias", source)
        self.assertIn("bool firstObBias = false", source)
        self.assertIn("this.firstObBias", source)
        self.assertIn("closedHigh > priorHigh and closedLow < priorLow", source)
        self.assertNotIn("open[2]", source)
        self.assertNotIn("priorOpen", source)
        self.assertIn("bothSides and not na(closedClose) and closedClose > priorHigh", source)
        self.assertIn("bothSides and not na(closedClose) and closedClose < priorLow", source)
        self.assertIn("bullish ? lowPrice : highPrice", source)
        self.assertIn("showConfirmedMarker", source)
        for name in STAGE_NAMES:
            self.assertIn(f'"{name}"', source)
        for stack in ("4H / 15M / 5M", "4H / 5M / 1M", "1H / 15M / 5M", "1H / 5M / 1M"):
            self.assertIn(stack, source)
        self.assertIn("Chart timeframe must be the selected LTF or lower.", source)
        reference_assignments = [
            line.strip() for line in source.splitlines() if "inversionRef :=" in line
        ]
        self.assertEqual(reference_assignments, ["this.inversionRef := na", "this.inversionRef := reference"])
        mtf_body = source.split("f_onMtf(")[1].split("f_onLtf(")[0]
        self.assertIn("this.candlesElapsed := this.candlesElapsed + 1", mtf_body)
        ltf_body = source.split("f_onLtf(")[1].split("f_process(")[0]
        self.assertNotIn("candlesElapsed", ltf_body)
        self.assertIn("this.direction", source)
        self.assertIn("high[2]", source)
        self.assertIn("f_noteSweep", source)
        self.assertNotIn("flipHigh", source)
        self.assertIn('f_row(3, "Direction"', source)
        self.assertIn("Stop loss", source)
        self.assertIn("Take profit", source)
        self.assertIn("this.stopLoss := this.f_stopBeforeBreak(level)", source)
        self.assertNotIn("Stop Swing Length", source)
        self.assertNotIn("f_stopSwing", source)


if __name__ == "__main__":
    unittest.main()
