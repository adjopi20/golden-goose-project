"""Small reusable exit extensions to the existing, tested print-by-print replay."""
import math

from models.btc_pny.exit_sweep import PathReplay
from models.orb.scripts.pre_ny_submodels.sweep_pre_ny_early_immediate_tail_exits_cp002 import _portfolio_net_positive_floor


class FullRunnerReplay(PathReplay):
    """No partial; causal threshold activates fee-aware floor and ATR x5 trail."""
    def __init__(self,position,fee_bps,activation_r):
        if not math.isfinite(activation_r) or activation_r<=0:
            raise ValueError('Activation must be positive and finite')
        self.activation_r=activation_r
        self.activation_timestamp_ms=None
        super().__init__(position,f'full_runner_{activation_r:g}r',fee_bps)

    def step(self,tick,atr):
        if self.closed:return
        if self.pending:
            super().step(tick,atr)  # Only full exits are queued.
            return
        price=tick['price']; move=self.sign*(price-self.entry)/self.risk
        self.mfe=max(self.mfe,move);self.mae=min(self.mae,move)
        self.best=max(self.best,price) if self.sign==1 else min(self.best,price)
        self.mark(price)
        if tick['timestamp_ms']>=self.position['force_exit_timestamp_ms']:
            self.pending={'reason':'next_day_0929',**tick};return
        if self.sign*(price-self.stop)<=0:
            self.pending={'reason':'runner_stop' if self.activation_timestamp_ms is not None else 'initial_stop',**tick};return
        if self.activation_timestamp_ms is None and move>=self.activation_r:
            self.activation_timestamp_ms=tick['timestamp_ms']
            floor=_portfolio_net_positive_floor(self.position['direction'],self.entry,self.fee_bps,self.buffer_bps,0.,price)
            self.ratchet(floor,tick,'portfolio_net_positive_floor')
        if self.activation_timestamp_ms is not None:
            if atr is None or not math.isfinite(atr) or atr<=0:
                raise ValueError('Runner has no valid completed five-minute ATR')
            self.ratchet(self.best-self.sign*atr*5,tick,'atr_trail')
            if self.sign*(price-self.stop)<=0:self.pending={'reason':'runner_stop',**tick}

    def result(self):
        row=super().result()
        row.update(partial_fraction=0.,atr_activation_r=self.activation_r,
            atr_activation_timestamp_ms=self.activation_timestamp_ms,
            activated_then_nonpositive=self.activation_timestamp_ms is not None and row['r']<=0)
        return row


class FixedTargetReplay(PathReplay):
    def __init__(self, position, target_r, fee_bps):
        if not math.isfinite(target_r) or target_r<=0:
            raise ValueError('Invalid fixed target')
        self.target_r=target_r
        super().__init__(position,f'fixed_{target_r:g}r',fee_bps)

    def step(self,tick,atr=None):
        if self.closed:return
        if self.pending:
            # A full exit uses the original fee/fill implementation and closes.
            super().step(tick,atr)
            return
        price=tick['price']; move=self.sign*(price-self.entry)/self.risk
        self.mfe=max(self.mfe,move);self.mae=min(self.mae,move)
        self.best=max(self.best,price) if self.sign==1 else min(self.best,price)
        self.mark(price)
        if tick['timestamp_ms']>=self.position['force_exit_timestamp_ms']:
            reason='next_day_0929'
        elif self.sign*(price-self.stop)<=0:
            reason='initial_stop'
        elif move>=self.target_r:
            reason=f'fixed_tp{self.target_r:g}r'
        else:reason=None
        if reason:self.pending={'reason':reason,**tick}


class DelayedTrailReplay(PathReplay):
    """Only defer ATR ratchets. Partial and portfolio-net-positive floor unchanged."""
    def __init__(self,position,fee_bps,activation_r=2.):
        if not math.isfinite(activation_r) or activation_r<1:
            raise ValueError('Trail activation must be >=1R')
        self.activation_r=activation_r
        self.trail_active=False
        self.activation_timestamp_ms=None
        super().__init__(position,'half1r_atr5',fee_bps)

    def step(self,tick,atr):
        if not self.closed and self.sign*(tick['price']-self.entry)/self.risk>=self.activation_r:
            if not self.trail_active:self.activation_timestamp_ms=tick['timestamp_ms']
            self.trail_active=True
        super().step(tick,atr)

    def ratchet(self,level,tick,reason):
        if reason=='atr_trail' and not self.trail_active:return
        super().ratchet(level,tick,reason)

    def result(self):
        row=super().result()
        row.update(policy=f'half1r_atr5_activate{self.activation_r:g}r',
                   atr_activation_timestamp_ms=self.activation_timestamp_ms)
        return row
