"""Monthly VWAP mean reversion research; three independent SD portfolios.

No fills are inferred from candle closes. The research executor supplies an
actual eligible fill, monitors fixed brackets, then calls record_exit.
"""
from dataclasses import dataclass, replace
import math

import pandas as pd

SHARED_INDICATOR_NAMESPACE = "indicator.research"



def anchor_features(bars, anchor_period="month", sd_levels=(1, 2, 3)):
    from indicator.research.vwap import anchored_vwap, vwap_std, vwap_bands
    from utils.closed_candle_context import CandleNotReady

    # A truncated initial month must not silently become a full monthly anchor.
    first = bars.index[0]
    aligned = first.day == 1 if anchor_period == "month" else first.dayofweek == 0
    if not aligned or first != first.normalize():
        raise CandleNotReady(f"Supply history from the opening of the initial UTC {anchor_period}")
    mean = anchored_vwap(bars, source="hlc3", anchor_period=anchor_period)
    frequency = "M" if anchor_period == "month" else "W-SUN"
    group = pd.Series(bars.index.tz_convert("UTC").tz_localize(None).to_period(frequency), index=bars.index)
    std = vwap_std(bars[["high", "low", "close"]].mean(axis=1), bars.volume, mean, group)
    result = pd.DataFrame({"vwap": mean, "std": std})
    for level in sd_levels:
        bands = vwap_bands(mean, std, level)
        result[f"upper_{float(level):g}"] = bands.vwap_upper
        result[f"lower_{float(level):g}"] = bands.vwap_lower
    return result


def monthly_features(bars):
    return anchor_features(bars)


@dataclass(frozen=True)
class Config:
    base_timeframe: str = "1d"
    calculation_timeframe: str = "1d"
    evaluation_timeframe: str = "1d"
    observation_window: dict | None = None
    anchor_period: str = "month"
    sd_levels: tuple = (1, 2, 3)
    daily_er_max: float | None = None
    order_flow_entry: str = "off"
    retest_initiative: bool = False
    micro_breakout: bool = False
    micro_breakout_require_flow: bool = True


class MonthlySDReversion:
    submodels = (1, 2, 3)

    """One position/pending entry per SD; portfolios never block each other.

    Touch means close at/beyond a band, not an intrabar high/low touch.
    Repeated outside closes may signal again after the previous trade exits.
    TP stays at the latest completed decision-boundary VWAP known at fill.
    Pending signals expire at the next daily boundary; open brackets survive
    month resets. No maximum holding period or forced monthly exit is imposed.
    """

    def __init__(self, config=Config()):
        from utils.closed_candle_context import timeframe_ms
        for timeframe in (config.base_timeframe, config.calculation_timeframe, config.evaluation_timeframe):
            timeframe_ms(timeframe)
        if type(config.retest_initiative) is not bool:
            raise ValueError("retest_initiative must be a boolean")
        if type(config.micro_breakout) is not bool or type(config.micro_breakout_require_flow) is not bool:
            raise ValueError("Micro breakout options must be booleans")
        if not config.micro_breakout and not config.micro_breakout_require_flow:
            raise ValueError("Price-only breakout requires micro_breakout")
        if config.micro_breakout:
            if (config.base_timeframe, config.calculation_timeframe, config.evaluation_timeframe) != ("5m", "8h", "5m"):
                raise ValueError("Micro breakout requires 5m base/entry and 8h setup candles")
            if config.retest_initiative or config.daily_er_max is not None or config.order_flow_entry != "off":
                raise ValueError("Run micro breakout without other entry filters")
        elif config.retest_initiative:
            if (config.base_timeframe, config.calculation_timeframe, config.evaluation_timeframe) != ("15m", "8h", "15m"):
                raise ValueError("Retest initiative requires 15m base/entry and 8h setup candles")
            if config.daily_er_max is not None or config.order_flow_entry != "off":
                raise ValueError("Run retest initiative without other entry filters")
        elif config.calculation_timeframe != config.evaluation_timeframe:
            raise ValueError("VWAP calculation and evaluation timeframes must match")
        if config.anchor_period not in ("month", "week"):
            raise ValueError("VWAP anchor_period must be month or week")
        if (not config.sd_levels or len(set(config.sd_levels)) != len(config.sd_levels)
                or any(isinstance(x,bool) or not math.isfinite(x) or x<=0 for x in config.sd_levels)):
            raise ValueError("SD levels must be unique positive finite numbers")
        limit = config.daily_er_max
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, (int, float))
                                  or not 0 <= limit <= 1):
            raise ValueError("daily_er_max must be a number in [0, 1] or null")
        if config.order_flow_entry not in ("off", "ready", "initiative"):
            raise ValueError("order_flow_entry must be off, ready or initiative")
        self.config = config
        self.needs_observation_prints = config.micro_breakout
        self.submodels = tuple(config.sd_levels)
        self.pending = {}
        self.positions = {}
        self.records = {level: [] for level in self.submodels}
        self.last_boundary = None
        self.position_blocks = []
        self._used_retests = set()

    def evaluate(self, bars, *, decision_timestamp_ms):
        from utils.closed_candle_context import CandleContext, IndicatorSpec

        if self.config.retest_initiative or self.config.micro_breakout:
            raise ValueError("Use the cached batch observer for multi-timeframe VWAP timing")

        context = CandleContext(bars, as_of_ms=decision_timestamp_ms,
                                base_timeframe=self.config.base_timeframe)
        daily_er = self._daily_er(context)
        order_flow = self._order_flow(context.candles(self.config.evaluation_timeframe)) if (
            self.config.order_flow_entry != "off") else None
        result = context.evaluate(lambda snapshot: self._decide(snapshot, daily_er, order_flow),
            evaluation_timeframe=self.config.evaluation_timeframe,
            indicators={"monthly": IndicatorSpec(self.config.calculation_timeframe, anchor_features,
                dict(anchor_period=self.config.anchor_period, sd_levels=self.submodels))})
        return result

    def _daily_er(self, context):
        if self.config.daily_er_max is None:
            return None
        daily = context.candles("1d").set_index("end_ms")
        path = daily.close.diff().abs().rolling(20).sum()
        ratio = daily.close.diff(20).abs().div(path).mask(path.eq(0), 0).clip(upper=1)
        return pd.DataFrame({"value": ratio, "available_at_ms": daily.available_at_ms.rolling(21).max()})

    def _order_flow(self, candles):
        if self.config.order_flow_entry == "off":
            return None
        from indicator.research.order_flow import order_flow_features

        if not {"buy_volume", "sell_volume"}.issubset(candles.columns):
            raise ValueError("Initiative needs measured aggressor-side volume. Use the cached batch observer.")
        features = order_flow_features(candles)
        return features.set_index("end_ms")[["candle_label", "available_at_ms"]]

    def prepare_observation_window(self, start, end, source_start_ms, source_end_ms):
        start = pd.to_datetime(source_start_ms,unit="ms",utc=True) if start is None else start
        end = pd.to_datetime(source_end_ms,unit="ms",utc=True) if end is None else end
        if self.config.anchor_period == "month":
            window = complete_month_window(start,end,source_start_ms,source_end_ms)
        else:
            def utc(value):
                value=pd.Timestamp(value)
                return value.tz_localize("UTC") if value.tz is None else value.tz_convert("UTC")
            a,b=utc(start),utc(end)
            lower=max(a,pd.to_datetime(source_start_ms,unit="ms",utc=True))
            upper=min(b,pd.to_datetime(source_end_ms,unit="ms",utc=True))
            left=lower.normalize()-pd.Timedelta(days=lower.dayofweek)
            if left<lower:
                left+=pd.Timedelta(days=7)
            right=upper.normalize()-pd.Timedelta(days=upper.dayofweek)
            if right<=left:
                raise ValueError("No complete UTC week within requested/source coverage")
            excluded=[dict(start_utc=x.isoformat(),end_exclusive_utc=y.isoformat(),
                reason="incomplete_week_or_outside_confirmed_source_coverage")
                for x,y in ((a,left),(right,b)) if y>x]
            window=dict(policy="complete_utc_weeks_only",requested_start_utc=a.isoformat(),
                requested_end_exclusive_utc=b.isoformat(),effective_start_utc=left.isoformat(),
                effective_end_exclusive_utc=right.isoformat(),source_start_ms=int(source_start_ms),
                source_end_ms=int(source_end_ms),excluded_intervals=excluded)
        self.config=replace(self.config,observation_window=window)
        return window

    def prepare_observation(self, bars, *, as_of_ms):
        from utils.closed_candle_context import CandleContext, timeframe_ms
        if self.config.micro_breakout:
            raise ValueError("Micro breakout requires the cached batch observer with raw prints")
        if self.config.retest_initiative:
            return self._prepare_retest(bars, as_of_ms)
        if self.config.base_timeframe != self.config.calculation_timeframe:
            raise ValueError("Prepared VWAP research uses matching base/calculation/evaluation timeframes")
        context=CandleContext(bars,as_of_ms=as_of_ms,base_timeframe=self.config.base_timeframe)
        self._prepared_bars=context.candles(self.config.calculation_timeframe)
        self._prepared_daily_er = self._daily_er(context)
        self._prepared_order_flow = self._order_flow(self._prepared_bars)
        # Shared anchored calculations are prefix-causal, never fitted on outcomes.
        self._prepared_features=anchor_features(self._prepared_bars,self.config.anchor_period,self.submodels)
        return [(int(row.timestamp_ms),int(row.end_ms)) for row in self._prepared_bars.itertuples()]

    def _prepare_retest(self, bars, as_of_ms):
        from indicator.research.order_flow import order_flow_features
        from utils.closed_candle_context import CandleContext, timeframe_ms

        context = CandleContext(bars, as_of_ms=as_of_ms, base_timeframe="15m")
        lower = context.candles("15m")
        higher = context.candles("8h")
        features = anchor_features(higher, self.config.anchor_period, self.submodels)
        flow = order_flow_features(lower, include_temporal=False)
        initiative = flow.candle_label.eq("initiative")
        higher_available = higher.available_at_ms.cummax()
        duration = timeframe_ms("8h")
        self._retest_candidates = {}
        self._used_retests.clear()
        funnel = {str(level): dict(setups=0, retested=0, confirmed=0) for level in self.submodels}
        for stamp, setup in higher.iterrows():
            end = int(setup.end_ms)
            expiry = end + duration
            if expiry > as_of_ms:
                continue
            known = int(higher_available.at[stamp])
            bands = features.loc[stamp]
            if bands["std"] <= 0:
                continue
            left, right = lower.index.searchsorted(pd.to_datetime([known, expiry], unit="ms", utc=True))
            window = lower.iloc[left:right]
            window = window[window.end_ms.lt(expiry)]
            for level in self.submodels:
                upper = bands[f"upper_{float(level):g}"]
                lower_band = bands[f"lower_{float(level):g}"]
                if setup.close >= upper:
                    direction, band, price_direction = "short", upper, "down"
                elif setup.close <= lower_band:
                    direction, band, price_direction = "long", lower_band, "up"
                else:
                    continue
                counts = funnel[str(level)]
                counts["setups"] += 1
                touch = window.low.le(band) & window.high.ge(band)
                if not touch.any():
                    continue
                counts["retested"] += 1
                # Shift so the confirmation candle starts after an observed retest candle.
                retest_available = window.available_at_ms.where(touch).cummin().ffill().shift(1)
                aligned = initiative.loc[window.index] & flow.loc[window.index, "price_direction"].eq(price_direction)
                candidates = window.loc[aligned & retest_available.notna()]
                confirmed = False
                for t, bar in candidates.iterrows():
                    available = max(known, int(retest_available.at[t]), int(flow.at[t, "available_at_ms"]))
                    if available >= expiry:
                        continue
                    confirmed = True
                    candidate = (level, direction, float(bands.vwap), float(bar.close), end, expiry, available)
                    self._retest_candidates.setdefault(int(bar.timestamp_ms), []).append(candidate)
                counts["confirmed"] += int(confirmed)
        print(f"VWAP retest funnel (setup episodes per SD): {funnel}", flush=True)
        step = timeframe_ms("15m")
        return [(opening, opening + step) for opening in sorted(self._retest_candidates)]

    def prepare_observation_from_cache(self, minutes, *, start_ms, as_of_ms, prints):
        from indicator.research.order_flow import order_flow_features
        from model_development.observation_cache import aggregate_candles
        from utils.closed_candle_context import CandleContext, timeframe_ms

        bars8 = aggregate_candles(minutes, "8h", start_ms, as_of_ms)
        higher = CandleContext(bars8, as_of_ms=as_of_ms, base_timeframe="8h").candles("8h")
        bands = anchor_features(higher, self.config.anchor_period, self.submodels)
        step = timeframe_ms("5m")
        observed = minutes.loc[minutes.timestamp_ms.ge(start_ms) & minutes.timestamp_ms.lt(as_of_ms)]
        buckets = observed.timestamp_ms // step
        segments = buckets.diff().gt(1).cumsum()
        frames = []
        for _, segment in observed.groupby(segments, sort=False):
            left = int(segment.timestamp_ms.iloc[0]) // step * step
            right = (int(segment.timestamp_ms.iloc[-1]) // step + 1) * step
            candles = aggregate_candles(segment, "5m", left, right)
            closed = CandleContext(candles, as_of_ms=as_of_ms, base_timeframe="5m").candles("5m")
            frame = order_flow_features(closed, include_temporal=False)
            # Rolling references never bridge an interval with no observed prints.
            frame["range_low"] = closed.low.shift(1).rolling(3).min()
            frame["range_high"] = closed.high.shift(1).rolling(3).max()
            frame["range_ready"] = closed.available_at_ms.shift(1).rolling(3).max()
            frames.append(frame)
        flow = pd.concat(frames)
        lower = flow
        range_low, range_high, range_ready = flow.range_low, flow.range_high, flow.range_ready
        strong = flow.volume_class.eq("high") & flow.delta_class.eq("high")
        setup_ready = higher.available_at_ms.cummax()
        anchor = "W-SUN" if self.config.anchor_period == "week" else "M"
        duration = timeframe_ms("8h")
        self._retest_candidates = {}
        self._used_retests.clear()
        funnel = {str(level): dict(touches=0, price_breakouts=0, confirmed=0,
                                  first_breakout_supported=0, first_breakout_flow_warmup=0)
                  for level in self.submodels}
        for stamp, setup in higher.iterrows():
            end = int(setup.end_ms)
            boundary = min(end + duration, as_of_ms)
            if (end >= boundary or bands.at[stamp, "std"] <= 0
                    or stamp.tz_localize(None).to_period(anchor)
                    != pd.Timestamp(end, unit="ms").to_period(anchor)):
                continue
            known = int(setup_ready.at[stamp])
            if known >= boundary:
                continue
            start_ref, end_ref = prints.first(known), prints.first(boundary)
            for level in self.submodels:
                for direction, side, sign, reference in (
                        ("short", "upper", -1, range_low), ("long", "lower", 1, range_high)):
                    band = float(bands.at[stamp, f"{side}_{float(level):g}"])
                    stop, target = (float("-inf"), band) if sign == -1 else (band, float("inf"))
                    touch = prints.crossing(start_ref, end_ref, stop, target, 1)
                    if touch is None:
                        continue
                    tick = prints.tick(touch)
                    touch_ms = tick["timestamp_ms"]
                    expiry = min(touch_ms + timeframe_ms("1h"), boundary)
                    counts = funnel[str(level)]
                    counts["touches"] += 1
                    left, right = lower.end_ms.searchsorted([touch_ms, expiry], side="right")
                    window = lower.iloc[left:right]
                    window = window[window.end_ms.lt(expiry)]
                    levels = reference.loc[window.index]
                    # For a candle containing the touch, the crossing must occur after that print.
                    starts_inside = window.timestamp_ms.ge(touch_ms) | (sign * (tick["price"] - levels) <= 0)
                    broke = starts_inside & (sign * (window.close - levels) > 0)
                    available = flow.available_at_ms.loc[window.index].clip(lower=known)
                    available = available.combine(range_ready.loc[window.index], max)
                    broke &= available.lt(expiry)
                    counts["price_breakouts"] += int(broke.any())
                    confirmed = broke & strong.loc[window.index] & (sign * flow.delta.loc[window.index] > 0)
                    counts["confirmed"] += int(confirmed.any())
                    if broke.any():
                        first = broke.idxmax()
                        counts["first_breakout_supported"] += int(confirmed.at[first])
                        counts["first_breakout_flow_warmup"] += int(flow.at[first, "base_status"] == "warmup")
                    eligible = confirmed if self.config.micro_breakout_require_flow else broke
                    for t, bar in window.loc[eligible].iterrows():
                        candidate = (level, direction, float(bands.at[stamp, "vwap"]), float(bar.close),
                                     end, expiry, int(available.at[t]))
                        self._retest_candidates.setdefault(int(bar.timestamp_ms), []).append(candidate)
        self.observation_diagnostics = {
            "per_sd": funnel, "empty_5m_intervals": (as_of_ms - start_ms) // step - len(lower),
        }
        print(f"VWAP micro-breakout funnel per SD: {funnel}", flush=True)
        step = timeframe_ms("5m")
        return [(opening, opening + step) for opening in sorted(self._retest_candidates)]

    def _evaluate_retest(self, opening_ms, decision_timestamp_ms):
        from utils.closed_candle_context import timeframe_ms
        boundary = opening_ms + timeframe_ms(self.config.evaluation_timeframe)
        self.pending.clear()
        decisions = []
        for level, direction, target, close, setup_end, expiry, available in self._retest_candidates[opening_ms]:
            key = (level, setup_end, direction) if self.config.micro_breakout else (level, setup_end)
            if (key in self._used_retests or decision_timestamp_ms < max(boundary, available)
                    or decision_timestamp_ms >= expiry):
                continue
            self._used_retests.add(key)
            snapshot = dict(feature_as_of_ms=boundary, feature_available_at_ms=available,
                decision_timestamp_ms=decision_timestamp_ms, entry_eligible_timestamp_ms=decision_timestamp_ms)
            signal = self._queue_entry(level, direction, target, close, expiry, snapshot)
            if signal is not None:
                decisions.append(signal)
        return {"decision": decisions}

    def evaluate_prepared(self, opening_ms, *, decision_timestamp_ms):
        if self.config.retest_initiative or self.config.micro_breakout:
            return self._evaluate_retest(opening_ms, decision_timestamp_ms)
        from utils.closed_candle_context import CandleContext, IndicatorSpec
        stamp=pd.to_datetime(opening_ms,unit="ms",utc=True)
        bar=self._prepared_bars.loc[[stamp]]
        context=CandleContext(bar,as_of_ms=decision_timestamp_ms,base_timeframe=self.config.base_timeframe)
        return context.evaluate(
            lambda snapshot: self._decide(snapshot, self._prepared_daily_er, self._prepared_order_flow),
            evaluation_timeframe=self.config.evaluation_timeframe,
            indicators={"monthly":IndicatorSpec(self.config.calculation_timeframe,
                lambda closed: self._prepared_features.loc[closed.index])})

    def _decide(self, snapshot, daily_er=None, order_flow=None):
        from utils.closed_candle_context import timeframe_ms
        boundary = snapshot["feature_as_of_ms"]
        if self.last_boundary is not None and boundary < self.last_boundary:
            raise ValueError("Research decisions must be chronological")
        if boundary == self.last_boundary:
            return []
        self.last_boundary = boundary
        self.pending.clear()
        if self.config.daily_er_max is not None:
            day = timeframe_ms("1d")
            daily_boundary = boundary // day * day
            if daily_er is None or daily_boundary not in daily_er.index:
                return []
            er = daily_er.loc[daily_boundary]
            if (not math.isfinite(er.value) or not math.isfinite(er.available_at_ms)
                    or er.available_at_ms > snapshot["decision_timestamp_ms"]
                    or er.value > self.config.daily_er_max):
                return []
            snapshot["feature_available_at_ms"] = max(
                snapshot["feature_available_at_ms"], int(er.available_at_ms))
        if self.config.order_flow_entry != "off":
            flow = order_flow.loc[boundary]
            if flow.candle_label == "warmup" or flow.available_at_ms > snapshot["decision_timestamp_ms"]:
                return []
            if self.config.order_flow_entry == "initiative" and flow.candle_label != "initiative":
                return []
            snapshot["feature_available_at_ms"] = max(
                snapshot["feature_available_at_ms"], int(flow.available_at_ms))
        features = snapshot["indicators"]["monthly"]
        close = snapshot["bar"]["close"]
        decisions = []
        if features["std"] <= 0:
            return decisions
        for level in self.submodels:
            direction = ("long" if close <= features[f"lower_{float(level):g}"] else
                         "short" if close >= features[f"upper_{float(level):g}"] else None)
            if direction is None:
                continue
            expiry = boundary + timeframe_ms(self.config.evaluation_timeframe)
            signal = self._queue_entry(level, direction, features["vwap"], close, expiry, snapshot)
            if signal is not None:
                decisions.append(signal)
        return decisions

    def _queue_entry(self, level, direction, target, close, expiry, snapshot):
        boundary = snapshot["feature_as_of_ms"]
        setup_id = f"vwap:{self.config.anchor_period}:{self.config.evaluation_timeframe}:sd{float(level):g}:{boundary}:{direction}"
        if level in self.positions:
            blocking = self.positions[level]
            self.position_blocks.append(dict(
                record_type="position_block", setup_id=setup_id, sd=level, submodel_key=level,
                direction=direction, feature_as_of_ms=boundary,
                feature_available_at_ms=snapshot["feature_available_at_ms"],
                decision_timestamp_ms=snapshot["decision_timestamp_ms"],
                blocking_setup_id=blocking["setup_id"],
                blocking_entry_timestamp_ms=blocking["entry_timestamp_ms"]))
            return None
        signal = dict(setup_id=setup_id, sd=level, direction=direction, target=target,
            signal_close=close, feature_as_of_ms=boundary,
            feature_available_at_ms=snapshot["feature_available_at_ms"],
            decision_timestamp_ms=snapshot["decision_timestamp_ms"],
            entry_eligible_timestamp_ms=snapshot["entry_eligible_timestamp_ms"],
            expires_at_ms=expiry)
        self.pending[level] = signal
        return signal.copy()

    def accept_fill(self, level, *, price, timestamp_ms):
        """Executor calls for its next available price, after processing decision.

        An entry that gaps through VWAP is rejected. RR is measured from the
        supplied executed price, before fees/funding and later exit slippage.
        """
        if type(timestamp_ms) is not int or not math.isfinite(price) or price <= 0:
            raise ValueError("Fill requires positive finite price and integer timestamp")
        signal = self.pending[level]
        if not signal["entry_eligible_timestamp_ms"] <= timestamp_ms < signal["expires_at_ms"]:
            raise ValueError("Fill is before eligibility or signal has expired")
        sign = 1 if signal["direction"] == "long" else -1
        risk = sign * (signal["target"] - price)
        del self.pending[level]
        if risk <= 0 or price - sign * risk <= 0:
            return None
        position = dict(signal, entry=price, entry_timestamp_ms=timestamp_ms,
                        stop=price - sign * risk, risk=risk)
        self.positions[level] = position
        return position.copy()

    def record_exit(self, level, *, price, timestamp_ms, reason):
        """Record an exit provided by the shared research execution method."""
        position = self.positions[level]
        if (type(timestamp_ms) is not int or timestamp_ms < position["entry_timestamp_ms"]
                or not math.isfinite(price) or price <= 0 or not reason):
            raise ValueError("Invalid exit price, time or reason")
        sign = 1 if position["direction"] == "long" else -1
        record = dict(position, exit=price, exit_timestamp_ms=timestamp_ms,
                      exit_reason=reason, gross_R=sign * (price-position["entry"])/position["risk"])
        self.records[level].append(record)
        del self.positions[level]
        return record.copy()


def create_observer(config):
    """Shared observer factory; strategy rules stay in this script."""
    # Load the model dependency before the global runner fingerprints imported code.
    import indicator.research.vwap
    config = dict(config)
    if (config.get("order_flow_entry", "off") != "off" or config.get("retest_initiative", False)
            or config.get("micro_breakout", False)):
        import indicator.research.order_flow
    if "timeframe" in config:
        timeframe = config.pop("timeframe")
        if config.get("micro_breakout", False):
            entry_timeframe = "5m"
        else:
            entry_timeframe = "15m" if config.get("retest_initiative", False) else timeframe
        config.update(base_timeframe=entry_timeframe, calculation_timeframe=timeframe, evaluation_timeframe=entry_timeframe)
    if "sd_levels" in config:
        config["sd_levels"] = tuple(config["sd_levels"])
    return MonthlySDReversion(Config(**config))


def complete_month_window(start, end, source_start_ms, source_end_ms):
    """Intersect requested/source coverage, then retain complete UTC months only."""
    def utc(value):
        value = pd.Timestamp(value)
        if pd.isna(value):
            raise ValueError("Observation dates must be valid")
        return value.tz_localize("UTC") if value.tz is None else value.tz_convert("UTC")

    requested_start, requested_end = utc(start), utc(end)
    if requested_end <= requested_start:
        raise ValueError("Observation end must be after start (end is exclusive)")
    source_start = pd.to_datetime(source_start_ms, unit="ms", utc=True)
    source_end = pd.to_datetime(source_end_ms, unit="ms", utc=True)
    lower, upper = max(requested_start, source_start), min(requested_end, source_end)
    effective_start = lower.normalize().replace(day=1)
    if effective_start < lower:
        effective_start += pd.DateOffset(months=1)
    effective_end = upper.normalize().replace(day=1)
    if effective_end <= effective_start:
        raise ValueError("No complete UTC month inside requested dates and confirmed source coverage")
    excluded = []
    for left, right in ((requested_start, effective_start), (effective_end, requested_end)):
        if right > left:
            excluded.append(dict(start_utc=left.isoformat(), end_exclusive_utc=right.isoformat(),
                                 reason="incomplete_month_or_outside_confirmed_source_coverage"))
    return dict(policy="complete_utc_months_only", requested_start_utc=requested_start.isoformat(),
        requested_end_exclusive_utc=requested_end.isoformat(),
        effective_start_utc=effective_start.isoformat(), effective_end_exclusive_utc=effective_end.isoformat(),
        source_start_ms=int(source_start_ms), source_end_ms=int(source_end_ms), excluded_intervals=excluded)


def observe_main(argv=None):
    """Model-specific date preparation; delegate execution to unchanged global observe."""
    import json
    from pathlib import Path
    import sys
    import pyarrow.parquet as pq

    model_path = Path(__file__).resolve()
    sys.path.insert(0, str(model_path.parents[3]))
    from model_development.phase_1 import observe

    parser = observe.parser()
    parser.description = "Observe monthly VWAP; skip and record incomplete UTC months. End is exclusive."
    for action in parser._actions:
        if action.dest in ("model", "iteration"):
            action.required = False
        if action.dest == "start":
            action.help = "Requested start (inclusive); incomplete initial UTC month is skipped"
        if action.dest == "end":
            action.help = "Requested end (exclusive); incomplete final UTC month is skipped"
    parser.set_defaults(model=model_path, iteration="ITER-004")
    args = parser.parse_args(argv)
    if args.model.resolve() != model_path:
        parser.error("This model launcher runs its own script; select other models through global observe")
    args.model = model_path
    ranges = []
    for path in args.input:
        source = pq.ParquetFile(path)
        column = source.schema_arrow.get_field_index("timestamp")
        if column < 0:
            raise ValueError(f"{path}: missing timestamp column")
        stats = [source.metadata.row_group(i).column(column).statistics
                 for i in range(source.num_row_groups)]
        if not stats or any(s is None or not s.has_min_max for s in stats):
            raise ValueError("Timestamp row-group statistics required for monthly coverage")
        ranges.append((min(s.min for s in stats), max(s.max for s in stats)))
    if args.batch:
        parser.error("Use the global observer entrypoint for --batch")
    config = json.loads(args.model_config)
    if not isinstance(config, dict):
        raise ValueError("model-config must be a JSON object")
    sys.path.insert(0, str(args.shared_core.resolve()))
    model = create_observer(config)
    window = model.prepare_observation_window(args.start, args.end,
                    min(r[0] for r in ranges), max(r[1] for r in ranges))
    config["observation_window"] = window
    args.model_config = json.dumps(config)
    args.start, args.end = window["effective_start_utc"], window["effective_end_exclusive_utc"]
    print(f"Requested UTC: [{window['requested_start_utc']}, {window['requested_end_exclusive_utc']})", flush=True)
    print(f"Effective UTC: [{args.start}, {args.end}); excluded intervals: {len(window['excluded_intervals'])}", flush=True)
    return observe.run(args)


if __name__ == "__main__":
    observe_main()
