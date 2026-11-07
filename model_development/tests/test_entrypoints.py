from pathlib import Path
import importlib.util
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from model_development.phase_3 import evaluate
from unittest.mock import patch


class EntryPoints(unittest.TestCase):
    def test_task_arguments_forwarded_without_interpretation(self):
        for task, (target, module) in evaluate.TASKS.items():
            with self.subTest(task=task), patch.object(evaluate, 'launch', return_value=7) as launch:
                self.assertEqual(evaluate.main(['--task', task, '--', '--output', 'folder with spaces']), 7)
                launch.assert_called_once_with(target, ['--output', 'folder with spaces'], module=module)

    def test_unknown_task_and_unimplemented_run_fail(self):
        for args in (['--task', 'unknown'], ['--task', 'kelly', '--run', 'a']):
            with self.assertRaises(SystemExit) as error:
                evaluate.main(args)
            self.assertEqual(error.exception.code, 2)

    def test_calculators_reuse_exact_existing_functions(self):
        from model_development.phase_3.kelly import empirical_kelly
        from model_development.phase_3.kelly import empirical_kelly as original
        from model_development.phase_3.monte_carlo import block_indices, path_stats
        from model_development.phase_3.monte_carlo import block_indices as blocks
        from model_development.phase_3.monte_carlo import path_stats as paths
        self.assertIs(empirical_kelly, original)
        self.assertIs(block_indices, blocks)
        self.assertIs(path_stats, paths)

    def test_real_help_from_neutral_directory_creates_no_outputs(self):
        commands = [['phase_1/observe.py', '--help'], ['phase_2/backtest.py', '--help']]
        commands += [['phase_3/evaluate.py', '--task', task, '--', '--help'] for task in evaluate.TASKS]
        with tempfile.TemporaryDirectory() as temp:
            for command in commands:
                args = [sys.executable, '-B', str(ROOT/'model_development'/command[0]), *command[1:]]
                result = subprocess.run(args, cwd=temp, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('usage:', result.stdout.lower())
            self.assertEqual(list(Path(temp).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
