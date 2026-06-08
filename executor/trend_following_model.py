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
    bubble_timestamp: pd.Timestamp  # Timestamp of triggering bubble
    bubble_price: float  # Price of triggering bubble
    trigger_mfe: float = 0.0  # MFE at entry trigger
    trigger_mae: float = 0.0  # MAE at entry trigger
    continuation_condition: str = ""  # Condition that triggered entry
    min_mfe_usd: float = 0.0  # Minimum MFE threshold used
    exit_candle_index: Optional[int] = None
    tp1_hit: bool = False
    tp1_timestamp: Optional[pd.Timestamp] = None
    tp1_candle_index: Optional[int] = None
    tp1_r: Optional[float] = None
    trail_active: bool = False
    trail_price: Optional[float] = None
    highest_price_since_tp1: Optional[float] = None
    lowest_price_since_tp1: Optional[float] = None
    exit_timestamp: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    result: str = "open"  # "open", "closed_win", "closed_loss"

class BubbleTracker:
    """Tracks MFE/MAE for a bubble in real-time."""
    def __init__(self, bubble: dict, condition: str, min_mfe: float):
        self.bubble_price = bubble['price']
        self.bubble_timestamp = bubble['timestamp']
        self.direction = 'sell' if bubble['aggressive_side'] == 'sell' else 'buy'
        self.condition = condition
        self.min_mfe = min_mfe
        self.mfe = 0.0
        self.mae = 0.0
        self.expiry = self.bubble_timestamp + pd.Timedelta(seconds=5)
    
    def update(self, trade: pd.Series):
        price = trade['price']
        if self.direction == 'sell':
            favorable = self.bubble_price - price
            adverse = price - self.bubble_price
        else:  # buy
            favorable = price - self.bubble_price
            adverse = self.bubble_price - price
            
        self.mfe = max(self.mfe, favorable)
        self.mae = max(self.mae, adverse)
    
    def check_condition(self) -> bool:
        if self.condition == "mfe_gt_mae":
            return self.mfe > self.mae and self.mfe >= self.min_mfe
        elif self.condition == "mfe_gt_2x_mae":
            return self.mfe > 2 * self.mae and self.mfe >= self.min_mfe
        return False

class TrendFollowingExecutor:
    """Stateful executor for trend following strategy."""
    
    STATE_WAITING = "WAITING"
    STATE_OPEN = "OPEN"
    STATE_TRACKING_BUBBLE = "TRACKING_BUBBLE"

    def __init__(self, 
                 tick_size: float = DEFAULT_TICK_SIZE,
                 min_bubble_tier: str = "medium",
                 continuation_condition: str = "mfe_gt_mae",
                 min_mfe_usd: float = 10.0,
                 bubble_sl_offset_usd: float = 10.0,
                 tp1_r: float = 4.0):
        self.tick_size = tick_size
        self.min_bubble_tier = min_bubble_tier
        self.continuation_condition = continuation_condition
        self.min_mfe_usd = min_mfe_usd
        self.bubble_sl_offset_usd = bubble_sl_offset_usd
        self.tp1_r = tp1_r
        self.state = self.STATE_WAITING
        self.active_trade = None
        self.active_bubble_trackers = []
        self.trade_counter = 1
        
        
    def process_candle(
        self,
        candle: pd.Series,
        candle_index: int,
        bubbles_in_candle: pd.DataFrame,
        trades_in_candle: pd.DataFrame,
        interpreter_state: dict
    ) -> Optional[SimulatedTrade]:
        """
        Process one candle sequentially. Returns closed trade if any.
        
        Args:
            candle: Current OHLCV candle data
            candle_index: Position in candle sequence
            bubbles_in_candle: Order bubbles that occurred during this candle
            trades_in_candle: Raw trades that occurred during this candle
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
        
        # Process trades within candle to update bubble trackers
        if not self.active_trade:
            self._process_trades_in_candle(trades_in_candle, interpreter_state)
        
        # Register new bubble trackers and process trades for entry
        if not self.active_trade:
            self._register_bubble_trackers(bubbles_in_candle, interpreter_state)
            self._process_trades_in_candle(trades_in_candle, interpreter_state)
        
        # Reset state to WAITING if no active trade and not tracking
        if not self.active_trade and not self.active_bubble_trackers:
            self.state = self.STATE_WAITING
        
        return closed_trade
    
    def _register_bubble_trackers(self, bubbles: pd.DataFrame, interpreter_state: dict):
        """Register new bubble trackers based on market conditions"""
        # Clear existing trackers at start of new candle
        self.active_bubble_trackers = []
        
        if interpreter_state["balance_state"] != "imbalance":
            return
            
        direction = None
        if interpreter_state["location"] == "above_vah":
            direction = "buy"
        elif interpreter_state["location"] == "below_val":
            direction = "sell"
        else:
            return
            
        # Process each bubble in candle
        for _, bubble in bubbles.iterrows():
            # Filter by bubble tier
            if bubble['bubble_tier'] < self.min_bubble_tier:
                continue
                
            # Filter by directional bias
            if direction == "buy" and bubble['aggressive_side'] != 'buy':
                continue
            if direction == "sell" and bubble['aggressive_side'] != 'sell':
                continue
                
            # Create tracker
            tracker = BubbleTracker(
                bubble,
                self.continuation_condition,
                self.min_mfe_usd
            )
            self.active_bubble_trackers.append(tracker)
            self.state = self.STATE_TRACKING_BUBBLE
            
    def _process_trades_in_candle(self, trades: pd.DataFrame, interpreter_state: dict):
        """Process raw trades within candle to update bubble trackers"""
        if trades.empty:
            return
            
        # Process each trade chronologically
        for _, trade in trades.sort_values("timestamp").iterrows():
            # Update existing trackers
            for tracker in self.active_bubble_trackers[:]:
                tracker.update(trade)
                if tracker.check_condition():
                    self._open_trade_at_price(tracker, trade)
                    self.active_bubble_trackers.remove(tracker)
                    return  # Only one trade per session
            
            # Check if trade is expired
            self.active_bubble_trackers = [
                t for t in self.active_bubble_trackers 
                if trade["timestamp"] < t.expiry
            ]
    
    def _open_trade_at_price(self, tracker: BubbleTracker, trade: pd.Series):
        """Create new trade at specific trade price"""
        direction = tracker.direction
        bubble_price = tracker.bubble_price
        
        # Calculate stop loss
        if direction == "long":
            sl_price = bubble_price - self.bubble_sl_offset_usd
            entry_price = trade["price"]
        else:  # short
            sl_price = bubble_price + self.bubble_sl_offset_usd
            entry_price = trade["price"]
        
        risk = abs(entry_price - sl_price)
        
        self.active_trade = SimulatedTrade(
            trade_id=self.trade_counter,
            direction=direction,
            entry_timestamp=trade["timestamp"],
            entry_price=entry_price,
            sl_price=sl_price,
            tp1_price=entry_price + (risk * self.tp1_r) if direction == "long" else entry_price - (risk * self.tp1_r),
            tp1_r=self.tp1_r,
            risk=risk,
            entry_candle_index=-1,  # Will be set later
            bubble_timestamp=tracker.bubble_timestamp,
            bubble_price=bubble_price,
            trigger_mfe=tracker.mfe,
            trigger_mae=tracker.mae,
            continuation_condition=tracker.condition,
            min_mfe_usd=tracker.min_mfe
        )
        self.trade_counter += 1
        self.state = self.STATE_OPEN
    
    
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