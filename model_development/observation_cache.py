"""Source-backed OHLCV cache plus exact, indexed raw-print lookup.

Cache is independent of strategy filters, anchor and SD settings. Raw aggTrade
order is validated once when building; fills are never inferred from OHLC paths.
"""
from collections import OrderedDict
import hashlib
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024*1024), b''):
            result.update(block)
    return result.hexdigest()


def source_identity(paths):
    return [dict(path=str(p.resolve()), size=p.stat().st_size, mtime_ns=p.stat().st_mtime_ns)
            for p in paths]


LEGACY_BUILDERS = {'315bfa84cec3856c837359199634fb59052fba30204457de36fcdc3905c3d440'}
LEGACY_V2_BUILDERS = {'df01ceda77bccf4a22d3cc3828c1bf3695a1decf9d36a3a55855fc6f151a6686', '090f5612e09a54956da76ec768ad4e9ca1e171134843f4be8c5a23b855535fc9'}


def validate_batch(a, previous=None, *, policy='error', source='', row_offset=0):
    """Preserve chronological rows; record uncertain ID segments only when allowed."""
    if policy not in ('error','record'):
        raise ValueError('ID policy must be error or record')
    ts,price,qty,ids=(a[n] for n in ('timestamp','price','qty','agg_trade_id'))
    if not len(ts):
        return [],previous
    for name,values in [('timestamp',ts),('agg_trade_id',ids),('price',price),('qty',qty)]:
        bad=np.flatnonzero((~np.isfinite(values)) | (values<0 if name in ('timestamp','agg_trade_id') else values<=0))
        if len(bad):
            i=int(bad[0])
            raise ValueError(f'{source}: invalid {name}={values[i]} at row {row_offset+i}, timestamp_ms={ts[i]}')
    positions=list(np.flatnonzero((ts[1:]<ts[:-1])|(np.diff(ids)!=1))+1)
    if previous is not None and (int(ts[0])<previous['timestamp'] or int(ids[0])!=previous['agg_trade_id']+1):
        positions.insert(0,0)
    events=[]
    segment=previous.get('source_segment',0) if previous else 0
    for index in positions:
        before=previous if index==0 else {k:int(a[k][index-1]) for k in ('timestamp','agg_trade_id','last_trade_id') if k in a}
        after={k:int(a[k][index]) for k in ('timestamp','agg_trade_id','first_trade_id') if k in a}
        event=dict(source=source,row=row_offset+int(index),before=before,after=after,
                   aggregate_id_step=after['agg_trade_id']-before['agg_trade_id'])
        if after['timestamp']<before['timestamp']:
            raise ValueError(f'Backwards raw timestamp: {json.dumps(event)}')
        if after['agg_trade_id']==before['agg_trade_id']:
            raise ValueError(f'Duplicate adjacent raw ID: {json.dumps(event)}')
        if after['agg_trade_id']<before['agg_trade_id'] and after['timestamp']==before['timestamp']:
            raise ValueError(f'Ambiguous same-timestamp raw ID order: {json.dumps(event)}')
        if policy=='error':
            raise ValueError(f'Raw ID discontinuity; source continuity unverified: {json.dumps(event)}. '
                             'Use --id-policy record only for explicitly accepted exploratory use.')
        event.update(kind='forward_id_gap' if event['aggregate_id_step']>0 else 'backward_id_transition',
                     source_segment_before=segment,source_segment_after=segment+1)
        segment+=1
        events.append(event)
    last={k:int(a[k][-1]) for k in ('timestamp','agg_trade_id','last_trade_id') if k in a}
    last['source_segment']=segment
    return events,last


def candle_cache(paths, directory, id_policy='error'):
    if id_policy not in ('error','record'):
        raise ValueError('ID policy must be error or record')
    identity = dict(version=2, builder=digest(Path(__file__)), sources=source_identity(paths), id_policy=id_policy)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    folder = directory / key
    data, meta = folder/'candles_1m.parquet', folder/'cache.json'
    # Reuse known, stricter legacy caches: diagnostics/resume must not invalidate LTC.
    for candidate in [meta]+list(directory.glob('*/cache.json')):
        if not candidate.exists():
            continue
        saved=json.loads(candidate.read_text())
        prior=saved['identity']
        known=(prior.get('version')==1 and prior.get('builder') in LEGACY_BUILDERS)
        known |= prior.get('version')==2 and prior.get('builder') in LEGACY_V2_BUILDERS|{identity['builder']}
        compatible=(prior.get('id_policy','error')=='error' or id_policy=='record'
                    or (saved.get('source_continuity')=='contiguous_ids_observed' and not saved.get('discontinuities')))
        if not known or not compatible or prior.get('sources')!=identity['sources']:
            continue
        cached=candidate.with_name('candles_1m.parquet')
        if not cached.exists() or digest(cached) != saved['sha256']:
            raise ValueError('Cache identity/checksum mismatch; cache cannot be used')
        return pd.read_parquet(cached), dict(saved, reused=True, path=str(candidate.parent),
            source_continuity=saved.get('source_continuity','contiguous_ids_observed'),
            discontinuities=saved.get('discontinuities',[]))
    folder.mkdir(parents=True, exist_ok=True)
    frames, carry, previous = [], None, None
    minimum = maximum = None
    rows = 0
    last_progress=time.monotonic()
    sides_known = True
    discontinuities=[]
    for path in paths:
        source = pq.ParquetFile(path)
        columns = ['timestamp', 'price', 'qty', 'agg_trade_id']
        if not set(columns).issubset(source.schema_arrow.names):
            raise ValueError(f'{path}: missing raw aggTrade columns')
        if any(not pa.types.is_integer(source.schema_arrow.field(n).type)
               for n in ('timestamp','agg_trade_id')):
            raise ValueError('Raw timestamps/IDs must be integer milliseconds/IDs')
        side = 'is_buyer_maker' in source.schema_arrow.names
        sides_known &= side
        if side:
            columns.append('is_buyer_maker')
        for name in ('first_trade_id','last_trade_id'):
            if name in source.schema_arrow.names:
                if not pa.types.is_integer(source.schema_arrow.field(name).type):
                    raise ValueError(f'{name} must be integer')
                columns.append(name)
        file_rows=0
        for batch in source.iter_batches(columns=columns, batch_size=262144):
            if any(batch.column(i).null_count for i in range(len(columns))):
                raise ValueError('Null raw trade fields')
            a = {name: batch.column(i).to_numpy(zero_copy_only=False) for i,name in enumerate(columns)}
            ts, price, qty, ids = (a[n] for n in columns[:4])
            if not len(ts):
                continue
            events,previous=validate_batch(a,previous,policy=id_policy,source=str(path),row_offset=file_rows)
            for event in events:
                print(f'WARNING: source continuity unverified: {json.dumps(event)}',flush=True)
            discontinuities.extend(events)
            file_rows+=len(ts)
            minimum = int(ts[0]) if minimum is None else minimum
            maximum = int(ts[-1]); rows += len(ts)
            frame = pd.DataFrame(dict(timestamp_ms=(ts//60000)*60000, price=price, volume=qty))
            aggregates = dict(open=('price','first'), high=('price','max'), low=('price','min'),
                              close=('price','last'), volume=('volume','sum'))
            if side:
                if a['is_buyer_maker'].dtype.kind != 'b':
                    raise ValueError('is_buyer_maker must be boolean')
                frame['buy_volume'] = np.where(a['is_buyer_maker'],0.,qty)
                frame['sell_volume'] = np.where(a['is_buyer_maker'],qty,0.)
                aggregates.update(buy_volume=('buy_volume','sum'),sell_volume=('sell_volume','sum'))
            if time.monotonic()-last_progress>=20:
                print(f'Cache validated {rows:,} trades through {pd.to_datetime(maximum,unit="ms",utc=True)}',flush=True)
                last_progress=time.monotonic()
            grouped = frame.groupby('timestamp_ms',sort=False).agg(**aggregates).reset_index()
            if carry is not None:
                if carry['timestamp_ms'] == grouped.iloc[0]['timestamp_ms']:
                    grouped.loc[0,'open'] = carry['open']
                    grouped.loc[0,'high'] = max(carry['high'],grouped.loc[0,'high'])
                    grouped.loc[0,'low'] = min(carry['low'],grouped.loc[0,'low'])
                    for name in ('volume','buy_volume','sell_volume'):
                        if name in grouped and name in carry:
                            grouped.loc[0,name] += carry[name]
                else:
                    frames.append(pd.DataFrame([carry]))
            carry = grouped.iloc[-1].to_dict()
            if len(grouped)>1:
                frames.append(grouped.iloc[:-1])
        print(f'Cache scanned: {path.name}; {rows:,} trades',flush=True)
    if carry is None:
        raise ValueError('Empty raw source')
    frames.append(pd.DataFrame([carry]))
    result = pd.concat(frames,ignore_index=True)
    result.timestamp_ms = result.timestamp_ms.astype('int64')
    if not sides_known:
        result = result.drop(columns=['buy_volume','sell_volume'],errors='ignore')
    if 'buy_volume' in result:
        result['delta'] = result.buy_volume-result.sell_volume
    if source_identity(paths) != identity['sources']:
        raise ValueError('Source changed during cache build')
    partial = folder/'candles_1m.partial'
    result.to_parquet(partial,index=False,compression='zstd')
    partial.replace(data)
    saved = dict(identity=identity, sha256=digest(data), rows=rows, source_start_ms=minimum,
                 source_end_ms=maximum, validation='chronological timestamps; no adjacent duplicate IDs; positive finite price/qty; explicit source-ID-discontinuity policy',
                 source_continuity='unverified_discontinuities' if discontinuities else 'contiguous_ids_observed',
                 discontinuities=discontinuities)
    meta.write_text(json.dumps(saved,indent=2),encoding='utf-8')
    return result, dict(saved,reused=False,path=str(folder))


def aggregate_candles(minutes, timeframe, start, end):
    from utils.closed_candle_context import timeframe_ms
    duration = timeframe_ms(timeframe)
    if duration < 60000 or duration % 60000:
        raise ValueError('Cached candle timeframe must be a fixed multiple of 1m')
    frame = minutes[(minutes.timestamp_ms>=start)&(minutes.timestamp_ms<end)].copy()
    frame.index = pd.to_datetime(frame.timestamp_ms,unit='ms',utc=True)
    aggregate = dict(open=('open','first'), high=('high','max'), low=('low','min'),
                     close=('close','last'), volume=('volume','sum'))
    for name in ('buy_volume','sell_volume','delta'):
        if name in frame:
            aggregate[name] = (name,'sum')
    bars = frame.resample(f'{duration}ms',origin='epoch').agg(**aggregate)
    # Sparse minutes are fine: a nonempty larger bar is still measured OHLCV.
    # An entirely empty evaluation bar has no observed price; do not invent it.
    if bars.empty or bars[['open','high','low','close']].isna().any().any():
        raise ValueError(f'No observed prices for a complete {timeframe} candle; evaluation blocked')
    if int(bars.index[0].timestamp()*1000)!=start or int(bars.index[-1].timestamp()*1000)+duration!=end:
        raise ValueError('Incomplete cached timeframe coverage')
    return bars


class RawPrints:
    """Bounded row-group LRU; reuse compressed source instead of duplicating ticks."""
    def __init__(self, paths):
        self.files = [pq.ParquetFile(path) for path in paths]
        self.groups = []
        self.cache = OrderedDict()
        for source in self.files:
            col = source.schema_arrow.get_field_index('timestamp')
            for i in range(source.num_row_groups):
                stats = source.metadata.row_group(i).column(col).statistics
                if stats is None or not stats.has_min_max:
                    raise ValueError('Raw timestamp row-group statistics required')
                self.groups.append((source,i,int(stats.min),int(stats.max)))
        self.ends = np.array([g[3] for g in self.groups],dtype='int64')

    def close(self):
        self.cache.clear()
        for source in self.files:
            source.close()

    def arrays(self, group):
        if group not in self.cache:
            source,index,_,_ = self.groups[group]
            table = source.read_row_group(index,columns=['timestamp','price','agg_trade_id'])
            self.cache[group] = tuple(table[n].combine_chunks().to_numpy() for n in table.column_names)
            if len(self.cache)>8:
                self.cache.popitem(last=False)
        self.cache.move_to_end(group)
        return self.cache[group]

    def tick(self, ref):
        ts,price,ids = self.arrays(ref[0])
        i=ref[1]
        return dict(timestamp_ms=int(ts[i]),price=float(price[i]),agg_trade_id=int(ids[i]))

    def first(self, timestamp):
        group = int(np.searchsorted(self.ends,timestamp,side='left'))
        while group < len(self.groups):
            ts,_,_ = self.arrays(group)
            offset = int(np.searchsorted(ts,timestamp,side='left'))
            if offset < len(ts):
                return (group,offset)
            group += 1
        return (len(self.groups),0)

    def next(self, ref):
        if ref[0]>=len(self.groups):
            return ref
        ts,_,_ = self.arrays(ref[0])
        return (ref[0],ref[1]+1) if ref[1]+1<len(ts) else (ref[0]+1,0)

    def crossing(self, begin, limit, stop, target, sign, force_exit=None):
        group, offset = begin
        while (group,offset) < limit and group < len(self.groups):
            source,index,_,latest = self.groups[group]
            price_col = source.schema_arrow.get_field_index('price')
            stats = source.metadata.row_group(index).column(price_col).statistics
            if stats is not None and stats.has_min_max and (force_exit is None or latest<force_exit):
                if (sign==1 and stats.min>stop and stats.max<target) or (sign==-1 and stats.max<stop and stats.min>target):
                    group+=1; offset=0; continue
            ts,prices,_ = self.arrays(group)
            end = limit[1] if group==limit[0] else len(ts)
            selected=prices[offset:end]
            hits=(sign*(selected-stop)<=0)|(sign*(selected-target)>=0)
            if force_exit is not None:
                hits |= ts[offset:end]>=force_exit
            found=np.flatnonzero(hits)
            if len(found):
                return (group,offset+int(found[0]))
            group+=1; offset=0
        return None
