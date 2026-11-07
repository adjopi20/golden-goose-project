"""Dispatch existing evaluators without changing their input/output contracts.

Use --task TASK -- <native calculator flags>, or explicit --run mode for
Kelly/Monte Carlo or statistical-jump on a selected completed research account. No phase transition.
"""
import argparse
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from model_development._legacy import launch

TASKS = {
    'kelly': ('model_development.phase_3.kelly', True),
    'monte-carlo': ('model_development.phase_3.monte_carlo', True),
    'volatility-sizing': ('model_development.phase_3.volatility_sizing', True),
    'indicator-sizing': ('model_development.phase_3.indicator_sizing', True),
    'statistical-jump': ('model_development/phase_3/statistical_jump.py', False),
}


def main(argv=None):
    values=list(sys.argv[1:] if argv is None else argv)
    head=values[:values.index('--')] if '--' in values else values
    if any(v=='--run' or v.startswith('--run=') for v in head):
        task_parser=argparse.ArgumentParser(add_help=False)
        task_parser.add_argument('--task',nargs='+',required=True)
        tasks,remaining=task_parser.parse_known_args(values)
        if 'statistical-jump' in tasks.task:
            if tasks.task!=['statistical-jump']:
                task_parser.error('Statistical jump uses its own explicit market/window configuration; invoke separately')
            return launch(TASKS['statistical-jump'][0],remaining,module=False)
        from model_development.phase_3.outcomes import parser as run_parser,run
        result=run(run_parser().parse_args(values))
        print(f"Evaluation complete: {result['selected_account']}")
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', choices=tuple(TASKS), required=True)
    parser.add_argument('arguments', nargs=argparse.REMAINDER,
                        help='Existing task flags after --; no implicit data conversion')
    args = parser.parse_args(argv)
    forwarded = args.arguments
    if forwarded[:1] == ['--']:
        forwarded = forwarded[1:]
    target, module = TASKS[args.task]
    return launch(target, forwarded, module=module)


if __name__ == '__main__':
    raise SystemExit(main())
