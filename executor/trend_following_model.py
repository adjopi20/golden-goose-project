from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import pandas as pd

# Configuration constants
MIN_CONFIRMATION_BODY_RATIO = 0.6
MAX_CONFIRMATION_WICK_RATIO = 0.2
DEFAULT_TICK_SIZE = 0.1  # BTCUSDT tick size
TRAIL_OFFSET = 10.0  # Trailing stop offset

@dataclass
class SimulatedTrade:
    """Represents a single simulated trade with all lifecycle states."""
    trade_id: int
    direction: str  # "long" or "short"
    entry_timestamp: pd.Timestamp
    entry_price: float
    sl_price: float
    tp1_price: float
    risk: float
    entry_candle_index: int
    exit_candle_index: Optional[int] = None
    tp1_hit: bool = False
    tp1_timestamp: Optional[pd.Timestamp] = None
    tp1_candle_index: Optional[int] = None  # Candle index where TP1 was hit
    trail_active: bool = False
    trail_price: Optional[float] = None
    highest_price_since_tp1: Optional[float] = None  # Tracks highest price after TP1 for long positions
    lowest_price_since_tp1: Optional[float] = None   # Tracks lowest price after TP1 for short positions
    exit_timestamp: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    result: str = "open"  # "open", "closed_win", "closed_loss"

class TrendFollowingExecutor:
    """Stateful executor for trend following strategy."""
    
    STATE_WAITING = "WAITING"
    STATE_ARMED_LONG = "ARMED_LONG"
    STATE_ARMED_SHORT = "ARMED_SHORT"
    STATE_OPEN = "OPEN"

    def __init__(self, tick_size: float = DEFAULT_TICK_SIZE):
        self.tick_size = tick_size
        self.state = self.STATE_WAITING
        self.active_trade = None
        self.pending_setup = None
        self.trade_counter = 1
        
    def process_candle(
        self,
        candle: pd.Series,
        candle_index: int,
        bubbles_in_candle: pd.DataFrame,
        interpreter_state: dict
    ) -> Optional[SimulatedTrade]:
        """
        Process one candle sequentially. Returns closed trade if any.
        
        Args:
            candle: Current OHLCV candle data
            candle_index: Position in candle sequence
            bubbles_in_candle: Order bubbles that occurred during this candle
            interpreter_state: Balance/imbalance interpretation result
            
        Returns:
            Closed trade if one was exited, None otherwise
        """
        closed_trade = None
        
        # Update active trade (SL, TP1, trailing)
        if self.active_trade:
            closed_trade = self._update_active_trade(candle, candle_index)
            if closed_trade:
                self.active_trade = None
                self.state = self.STATE_WAITING
        
        # Check for pending_setup first (enter on next candle)
        if self.pending_setup and not self.active_trade:
            self._open_trade(candle, candle_index)
            self.pending_setup = None
            
        # Only look for new setups if no active trade or pending_setup
        if not self.active_trade and not self.pending_setup:
            self._evaluate_setups(candle, bubbles_in_candle, interpreter_state)
            
            # If we entered ARMED state and candle confirms, store pending setup
            if self._confirm_candle(candle) and self.state in (self.STATE_ARMED_LONG, self.STATE_ARMED_SHORT):
                self.pending_setup = {
                    "direction": "long" if self.state == self.STATE_ARMED_LONG else "short",
                    "confirmation_candle_index": candle_index,
                    "confirmation_timestamp": candle["timestamp"],
                    "confirmation_high": candle["high"],
                    "confirmation_low": candle["low"]
                }
                
            # Reset state to WAITING after recording setup
            self.state = self.STATE_WAITING
        
        return closed_trade
    
    def _evaluate_setups(self, candle: pd.Series, bubbles: pd.DataFrame, interpreter: dict):
        """Evaluate candle for potential setups based on interpreter state."""
        if interpreter["balance_state"] != "imbalance":
            return
            
        if interpreter["location"] == "above_vah":
            if self._has_aggressive_buy_bubble(bubbles):
                self.state = self.STATE_ARMED_LONG
                
        elif interpreter["location"] == "below_val":
            if self._has_aggressive_sell_bubble(bubbles):
                self.state = self.STATE_ARMED_SHORT
    
    def _has_aggressive_buy_bubble(self, bubbles: pd.DataFrame) -> bool:
        """Check for qualifying buy-side aggression bubbles."""
        return ((bubbles["aggressive_side"] == "buy") & 
                (bubbles["qty"] >= 30)).any()
    
    def _has_aggressive_sell_bubble(self, bubbles: pd.DataFrame) -> bool:
        """Check for qualifying sell-side aggression bubbles."""
        return ((bubbles["aggressive_side"] == "sell") & 
                (bubbles["qty"] >= 30)).any()
    
    def _confirm_candle(self, candle: pd.Series) -> bool:
        """Validate candle structure meets confirmation requirements."""
        range_ = candle["high"] - candle["low"]
        body = abs(candle["close"] - candle["open"])
        body_ratio = body / range_
        
        upper_wick = candle["high"] - max(candle["open"], candle["close"])
        lower_wick = min(candle["open"], candle["close"]) - candle["low"]
        upper_ratio = upper_wick / range_
        lower_ratio = lower_wick / range_
        
        if self.state == self.STATE_ARMED_LONG:
            return (candle["close"] > candle["open"] and
                    body_ratio >= MIN_CONFIRMATION_BODY_RATIO and
                    upper_ratio <= MAX_CONFIRMATION_WICK_RATIO and
                    lower_ratio <= MAX_CONFIRMATION_WICK_RATIO)
        
        elif self.state == self.STATE_ARMED_SHORT:
            return (candle["close"] < candle["open"] and
                    body_ratio >= MIN_CONFIRMATION_BODY_RATIO and
                    upper_ratio <= MAX_CONFIRMATION_WICK_RATIO and
                    lower_ratio <= MAX_CONFIRMATION_WICK_RATIO)
        
        return False
    
    def _open_trade(self, candle: pd.Series, candle_index: int):
        """Create new trade at current candle open."""
        confirmation_low = self.pending_setup["confirmation_low"]
        confirmation_high = self.pending_setup["confirmation_high"]
        direction = self.pending_setup["direction"]
        
        if direction == "long":
            sl_price = confirmation_low - self.tick_size
            entry_price = candle["open"]  # Enter at current candle open
        else:  # short
            sl_price = confirmation_high + self.tick_size
            entry_price = candle["open"]
        
        self.active_trade = SimulatedTrade(
            trade_id=self.trade_counter,
            direction=direction,
            entry_timestamp=candle["timestamp"],
            entry_price=entry_price,
            sl_price=sl_price,
            tp1_price=self._calculate_tp1(entry_price, sl_price, direction),
            risk=abs(entry_price - sl_price),
            entry_candle_index=candle_index
        )
        self.trade_counter += 1
        self.state = self.STATE_OPEN
    
    def _calculate_tp1(self, entry: float, sl: float, direction: str) -> float:
        """Calculate take profit 1 level (RR 1:2)."""
        risk = abs(entry - sl)
        return entry + (risk * 2) if direction == "long" else entry - (risk * 2)
    
    def _update_active_trade(self, candle: pd.Series, candle_idx: int) -> Optional[SimulatedTrade]:
        """Manage open trade positions and check exits."""
        trade = self.active_trade
        low, high = candle["low"], candle["high"]
        
        # Check stop loss hit
        if ((trade.direction == "long" and low <= trade.sl_price) or
            (trade.direction == "short" and high >= trade.sl_price)):
            trade.exit_price = trade.sl_price
            trade.result = "closed_loss"
            trade.exit_candle_index = candle_idx
            trade.exit_timestamp = candle["timestamp"]
            return trade
        
        # Check TP1 hit (skip same candle exits)
        if not trade.tp1_hit:
            if ((trade.direction == "long" and high >= trade.tp1_price) or
                (trade.direction == "short" and low <= trade.tp1_price)):
                trade.tp1_hit = True
                trade.tp1_timestamp = candle["timestamp"]
                trade.tp1_candle_index = candle_idx
                trade.trail_active = True
                trade.trail_price = trade.entry_price  # Breakeven SL
                
                # Initialize price tracking for trailing stop
                if trade.direction == "long":
                    trade.highest_price_since_tp1 = candle["high"]
                else:  # short
                    trade.lowest_price_since_tp1 = candle["low"]
                
                # Skip exit check on this candle
                return None
        
        # Update trailing stop (only from next candle)
        if trade.trail_active:
            # Skip trailing exit check on same candle as TP1 hit
            if candle_idx != trade.tp1_candle_index:
                # Update trailing stop price
                if trade.direction == "long":
                    # Update highest price since TP1
                    trade.highest_price_since_tp1 = max(
                        trade.highest_price_since_tp1 or candle["high"], 
                        candle["high"]
                    )
                    # Update trail price (never moves down)
                    trade.trail_price = trade.highest_price_since_tp1 - TRAIL_OFFSET
                else:  # short
                    # Update lowest price since TP1
                    trade.lowest_price_since_tp1 = min(
                        trade.lowest_price_since_tp1 or candle["low"], 
                        candle["low"]
                    )
                    # Update trail price (never moves up)
                    trade.trail_price = trade.lowest_price_since_tp1 + TRAIL_OFFSET
                
                # Check trailing stop hit
                if ((trade.direction == "long" and low <= trade.trail_price) or
                    (trade.direction == "short" and high >= trade.trail_price)):
                    trade.exit_price = trade.trail_price
                    trade.result = "closed_win" 
                    trade.exit_candle_index = candle_idx
                    trade.exit_timestamp = candle["timestamp"]
                    return trade
        
        return None
    

def simulate_trend_following(
    candles_df: pd.DataFrame,
    bubbles_df: pd.DataFrame,
    interpreter_df: pd.DataFrame,
    tick_size: float = DEFAULT_TICK_SIZE
) -> list[SimulatedTrade]:
    """
    Simulate trend following strategy execution on historical data.
    
    Args:
        candles_df: OHLCV candle data
        bubbles_df: Order bubble events (filtered by min_qty=30)
        interpreter_df: Balance/imbalance interpretation results
        tick_size: Minimum price movement
        
    Returns:
        List of simulated trades with full lifecycle
    """
    executor = TrendFollowingExecutor(tick_size)
    trades = []
    
    for i, candle in candles_df.iterrows():
        # Get bubbles in current candle
        candle_start = candle["timestamp"]
        if i < len(candles_df) - 1:
            candle_end = candles_df.iloc[i + 1]["timestamp"]
        else:
            candle_end = candle_start + (candles_df.iloc[i]["timestamp"] - candles_df.iloc[i - 1]["timestamp"])
        # candle_end = candle_start + pd.Timedelta(minutes=1)  # For 1m candles
        bubbles_in_candle = bubbles_df[
            (bubbles_df["timestamp"] >= candle_start) & 
            (bubbles_df["timestamp"] < candle_end)
        ]
        
        # Process candle through executor
        closed_trade = executor.process_candle(
            candle=candle,
            candle_index=i,
            bubbles_in_candle=bubbles_in_candle,
            interpreter_state=interpreter_df.iloc[i].to_dict()
        )
        
        if closed_trade:
            trades.append(closed_trade)
    
    # Handle any remaining open trade
    if executor.active_trade:
        executor.active_trade.result = "open"
        trades.append(executor.active_trade)
        
    return trades