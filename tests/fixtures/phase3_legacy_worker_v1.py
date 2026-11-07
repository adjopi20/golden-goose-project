# Frozen pre-Phase3 class, retained only for migration parity tests.
class Worker:
    def __init__(self, store, manager, *, paper=False, calibration=None, now_ms=None,
                 strict_coverage=False, min_native_reference_sessions=0):
        self.store, self.manager = store, manager
        self.paper, self.calibration = paper, calibration
        self.now_ms = now_ms or (lambda:int(time.time()*1000))
        self.strict_coverage = strict_coverage
        self.min_native_reference_sessions = min_native_reference_sessions

    def boundary(self, symbol, asof, state):
        local = datetime.fromtimestamp(asof/1000, NY)
        day = local.date()
        start, cutoff = clock_ms(day, 9), clock_ms(day, 12)
        if not start <= asof <= cutoff:
            return
        db = self.store.db
        if db.execute("SELECT 1 FROM evaluations WHERE symbol=? AND asof=?", (symbol,asof)).fetchone():
            return
        result = dict(state="NOT_READY", session_day=str(day), as_of_ms=asof, candidate=False)
        try:
            if self.strict_coverage and state["coverage_start_ms"] > clock_ms(day,1):
                raise NotReady("Collector did not cover full 01:00-09:00 profile; wait for next session")
            if asof > start and self.min_native_reference_sessions and symbol != "HYPEUSDT":
                prior_minutes = db.execute(
                    "SELECT timestamp_ms FROM minutes WHERE symbol=? AND source='aggregate_trades' AND timestamp_ms>=? AND timestamp_ms<?",
                    (symbol, asof-25*86_400_000, start)).fetchall()
                counts = Counter()
                for row in prior_minutes:
                    local_minute = datetime.fromtimestamp(row[0]/1000, NY)
                    if local_minute.date() < day and 9 <= local_minute.hour < 12:
                        counts[local_minute.date()] += 1
                if sum(count >= 60 for count in counts.values()) < self.min_native_reference_sessions:
                    raise NotReady("Need earlier native 09:00-12:00 sessions before relative order-flow evaluation")
            stored = db.execute("SELECT payload FROM profiles WHERE symbol=? AND session=?", (symbol,str(day))).fetchone()
            if stored:
                profile = json.loads(stored[0])
            else:
                if state["coverage_start_ms"] > clock_ms(day,1):
                    raise NotReady("Collector did not cover full 01:00-09:00 profile; wait for next session")
                prices = {r[0]:r[1] for r in db.execute(
                    "SELECT price,quantity FROM profile_prices WHERE symbol=? AND session=? ORDER BY price", (symbol,str(day)))}
                profile = make_profile(day, prices)
                db.execute("INSERT INTO profiles VALUES (?,?,?)", (symbol,str(day),encode(profile)))
                # Frozen profiles remain; old per-price buckets are no longer required.
                db.execute("DELETE FROM profile_prices WHERE symbol=? AND session<?", (symbol,str(day)))
            if asof == start:
                result.update(state="PROFILE_READY", profile=profile)
            else:
                prior = db.execute("SELECT payload FROM evaluations WHERE symbol=? AND asof>=? AND asof<?",
                                   (symbol,start,asof)).fetchall()
                if any(json.loads(r[0]).get("candidate") for r in prior):
                    result["state"] = "SESSION_LOCKED"
                else:
                    rows = [json.loads(r[0]) for r in db.execute(
                        "SELECT payload FROM minutes WHERE symbol=? AND timestamp_ms<? ORDER BY timestamp_ms", (symbol,asof))]
                    if not rows:
                        raise NotReady("No completed history")
                    context = prepare_context(pd.DataFrame(rows), day, asof)
                    signals, _ = evaluate_session(profile, context["bars"], symbol=symbol)
                    result["candidate"] = bool(signals)
                    if signals:
                        signal = signals[0]
                        if signal["feature_as_of_ms"] != asof:
                            raise NotReady("First candidate was missed; no replacement entry")
                        if symbol == "HYPEUSDT":
                            result["c1_candidate_features"] = hype_candidate_features(
                                context["bars"], signal["direction"],
                                context["atr_before"], context["signal_atr"])
                        trend = context["trend"]
                        structure = trend.get("ma_structure")
                        if structure in ("bullish_stack", "bearish_stack"):
                            trend["trade_alignment"] = "aligned" if (structure == "bullish_stack") == (signal["direction"] == "long") else "opposed"
                    evaluated = evaluate_completed_bar(symbol=symbol, profile=profile, as_of_ms=asof,
                        c1_calibration=self.calibration, **context)
                    result.update(evaluated)
                    if evaluated["signal"] is not None:
                        ready_ms = self.now_ms()
                        result["decision_ready_ms"] = ready_ms
                        result["decision_delay_ms"] = ready_ms-asof
                        if self.paper:
                            # The evaluator can take seconds. Paper entry becomes eligible only
                            # when the decision is actually ready, never at a past candle close.
                            paper_signal = paper_activation_signal(evaluated["signal"], ready_ms)
                            result["paper_signal"] = paper_signal
                            result["paper_result"] = self.manager.submit(ACCOUNT_IDS[symbol], paper_signal,
                                snapshot=evaluated["snapshot"], risk_fraction=evaluated["risk_fraction"], selected=evaluated["selected"])
                        else:
                            result["paper_result"] = "collection_only_no_order"
        except NotReady as exc:
            result.update(state="NOT_READY", reason=str(exc))
        db.execute("INSERT INTO evaluations VALUES (?,?,?)", (symbol,asof,encode(result)))
        print(encode(dict(symbol=symbol, session=str(day), asof=asof, state=result["state"],
                          reason=result.get("reason"), paper=self.paper)), flush=True)


