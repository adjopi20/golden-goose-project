"""Merge existing one-minute candle caches without rescanning raw aggTrades."""
from __future__ import annotations
import argparse, hashlib, json, tempfile
from pathlib import Path
import pandas as pd

COLUMNS=['timestamp_ms','open','high','low','close','volume','delta','trade_count']

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def run(inputs,output):
    if output.exists():raise FileExistsError(f'Use a new output directory: {output}')
    frames=[]; sources=[]
    for cache in inputs:
        path=cache/'candles_1m.parquet'
        if not path.exists():raise FileNotFoundError(path)
        frame=pd.read_parquet(path,columns=COLUMNS).sort_values('timestamp_ms',kind='mergesort')
        if frame.empty or frame['timestamp_ms'].duplicated().any():raise ValueError(f'Invalid source candle cache: {cache}')
        frames.append(frame);sources.append(dict(cache=str(cache.resolve()),rows=len(frame),sha256=sha(path)))
    result=pd.concat(frames,ignore_index=True).sort_values('timestamp_ms',kind='mergesort')
    duplicated=result[result['timestamp_ms'].duplicated(False)]
    for _,group in duplicated.groupby('timestamp_ms',sort=False):
        if not all(group.iloc[0].equals(row) for _,row in group.iloc[1:].iterrows()):
            raise ValueError(f"Conflicting candles at {int(group.iloc[0]['timestamp_ms'])}")
    result=result.drop_duplicates('timestamp_ms',keep='first').reset_index(drop=True)
    if not result['timestamp_ms'].is_monotonic_increasing:raise ValueError('Merged candles are not ordered')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.merge-candles-',dir=output.parent) as tmp:
        bundle=Path(tmp)/'bundle';bundle.mkdir()
        result.to_parquet(bundle/'candles_1m.parquet',index=False,compression='zstd')
        manifest=dict(schema_version=1,kind='merged_candle_cache',rows=len(result),start_timestamp_ms=int(result.iloc[0].timestamp_ms),end_timestamp_ms=int(result.iloc[-1].timestamp_ms),duplicate_rows_removed=sum(len(f) for f in frames)-len(result),sources=sources)
        (bundle/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        bundle.rename(output)
    return manifest

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input-cache',type=Path,action='append',required=True);p.add_argument('--output-dir',type=Path,required=True);a=p.parse_args();print(json.dumps(run(a.input_cache,a.output_dir),indent=2))
if __name__=='__main__':main()
