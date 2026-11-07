"""Monthly VWAP mean reversion research; three independent SD portfolios.

No fills are inferred from candle closes. The research executor supplies an
actual eligible fill, monitors fixed brackets, then calls record_exit.
"""
from dataclasses import dataclass, replace
import math

import pandas as pd



def anchor_features(bars, anchor_period="month", sd_levels=(1, 2, 3)):
    from indicator.vwap import anchored_vwap, vwap_std, vwap_bands
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
        if config.calculation_timeframe != config.evaluation_timeframe:
            raise ValueError("VWAP calculation and evaluation timeframes must match")
        if config.anchor_period not in ("month", "week"):
            raise ValueError("VWAP anchor_period must be month or week")
        if (not config.sd_levels or len(set(config.sd_levels)) != len(config.sd_levels)
                or any(isinstance(x,bool) or not math.isfinite(x) or x<=0 for x in config.sd_levels)):
            raise ValueError("SD levels must be unique positive finite numbers")
        self.config = config
        self.submodels = tuple(config.sd_levels)
        self.pending = {}
        self.positions = {}
        self.records = {level: [] for level in self.submodels}
        self.last_boundary = None
        self.position_blocks = []

    def evaluate(self, bars, *, decision_timestamp_ms):
        from utils.closed_candle_context import CandleContext, IndicatorSpec

        context = CandleContext(bars, as_of_ms=decision_timestamp_ms,
                                base_timeframe=self.config.base_timeframe)
        result = context.evaluate(self._decide, evaluation_timeframe=self.config.evaluation_timeframe,
            indicators={"monthly": IndicatorSpec(self.config.calculation_timeframe, anchor_features,
                dict(anchor_period=self.config.anchor_period, sd_levels=self.submodels))})
        return result

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
        if self.config.base_timeframe != self.config.calculation_timeframe:
            raise ValueError("Prepared VWAP research uses matching base/calculation/evaluation timeframes")
        context=CandleContext(bars,as_of_ms=as_of_ms,base_timeframe=self.config.base_timeframe)
        self._prepared_bars=context.candles(self.config.calculation_timeframe)
        # Shared anchored calculations are prefix-causal, never fitted on outcomes.
        self._prepared_features=anchor_features(self._prepared_bars,self.config.anchor_period,self.submodels)
        return [(int(row.timestamp_ms),int(row.end_ms)) for row in self._prepared_bars.itertuples()]

    def evaluate_prepared(self, opening_ms, *, decision_timestamp_ms):
        from utils.closed_candle_context import CandleContext, IndicatorSpec
        stamp=pd.to_datetime(opening_ms,unit="ms",utc=True)
        bar=self._prepared_bars.loc[[stamp]]
        context=CandleContext(bar,as_of_ms=decision_timestamp_ms,base_timeframe=self.config.base_timeframe)
        return context.evaluate(self._decide,evaluation_timeframe=self.config.evaluation_timeframe,
            indicators={"monthly":IndicatorSpec(self.config.calculation_timeframe,
                lambda closed: self._prepared_features.loc[closed.index])})

    def _decide(self, snapshot):
        from utils.closed_candle_context import timeframe_ms
        boundary = snapshot["feature_as_of_ms"]
        if self.last_boundary is not None and boundary < self.last_boundary:
            raise ValueError("Research decisions must be chronological")
        if boundary == self.last_boundary:
            return []
        self.last_boundary = boundary
        self.pending.clear()
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
                continue
            signal = dict(setup_id=setup_id, sd=level, direction=direction, target=features["vwap"],
                signal_close=close, feature_as_of_ms=boundary,
                feature_available_at_ms=snapshot["feature_available_at_ms"],
                decision_timestamp_ms=snapshot["decision_timestamp_ms"],
                entry_eligible_timestamp_ms=snapshot["entry_eligible_timestamp_ms"],
                expires_at_ms=boundary + timeframe_ms(self.config.evaluation_timeframe))
            self.pending[level] = signal
            decisions.append(signal.copy())
        return decisions

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
    import indicator.vwap
    config = dict(config)
    if "timeframe" in config:
        timeframe = config.pop("timeframe")
        config.update(base_timeframe=timeframe, calculation_timeframe=timeframe, evaluation_timeframe=timeframe)
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
    sys.path.insert(0, str(model_path.parents[4]))
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
