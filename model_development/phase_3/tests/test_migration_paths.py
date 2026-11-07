"""Relocated presets must retain the same input root and local context bytes."""
import json
from pathlib import Path
from model_development.phase_3.ledger_io import config_load

ROOT=Path(__file__).resolve().parents[3]

def test_preset_roots_and_internal_input_paths():
    folder=ROOT/'model_development/phase_3/configs'
    configs=list(folder.glob('*.json'))
    assert len(configs)==4
    for p in configs:
        cfg=json.loads(p.read_text(encoding='utf-8'))
        if 'datasets' in cfg:
            cfg,root=config_load(p)
        else:
            root=(p.parent/cfg['path_root']).resolve()
        assert root==ROOT
        if 'ledger_config' in cfg:
            assert (root/cfg['ledger_config']).is_file()
            assert cfg['ledger_config'].startswith('model_development/phase_3/configs/')
        for entry in cfg.get('context_files',[]):
            if entry.startswith('model_development/'):
                assert (root/entry).is_file()
