import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class DockerModuleDependencyTests(unittest.TestCase):
    def run_step(self, modules, fail_on=''):
        dockerfile = (Path(__file__).resolve().parents[1] / 'Dockerfile').read_text()
        step = dockerfile.split('RUN set -eu;', 1)[1].split('\n\n', 1)[0]
        step = 'set -eu;' + step.replace('\\\n', '\n')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for module in modules:
                target = root / 'modules' / module / 'requirements.txt'
                target.parent.mkdir(parents=True)
                target.write_text('example>=1\n')
            binary = root / 'bin/python'
            binary.parent.mkdir()
            binary.write_text(f'#!{sys.executable}\n' +
                              'import json, os, pathlib, sys\n' +
                              'with pathlib.Path(os.environ["CYBERHUB_QA_LOG"]).open("a") as f:\n' +
                              '    f.write(json.dumps(sys.argv[1:])+"\\n")\n' +
                              'if os.environ.get("CYBERHUB_QA_FAIL") and os.environ["CYBERHUB_QA_FAIL"] in sys.argv[-1]:\n' +
                              '    sys.exit(9)\n')
            binary.chmod(0o755)
            log = root / 'calls.jsonl'
            env = {**os.environ, 'PATH': str(binary.parent) + os.pathsep + os.environ['PATH'],
                   'CYBERHUB_QA_LOG': str(log), 'CYBERHUB_QA_FAIL': fail_on}
            result = subprocess.run(['sh', '-c', step], cwd=root, env=env, capture_output=True, text=True)
            calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            return result, calls

    def test_actual_docker_shell_step_handles_all_module_requirements_and_empty_starter(self):
        result, calls = self.run_step(['alpha', 'beta module'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[-1] for call in calls],
                         ['modules/alpha/requirements.txt', 'modules/beta module/requirements.txt'])
        result, calls = self.run_step([])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])

    def test_requirement_failure_aborts_the_build_step(self):
        result, calls = self.run_step(['alpha', 'beta'], fail_on='alpha')
        self.assertEqual(result.returncode, 9)
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
