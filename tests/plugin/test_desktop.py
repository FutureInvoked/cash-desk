"""The Python suite also executes the real Desktop ES module with Node."""
import shutil
import subprocess

from tests.plugin.support import ROOT


def test_desktop_module_loads_registers_and_renders_without_actions():
    node = shutil.which('node')
    assert node, 'Desktop checks require Node.js 22+; install Node before running the test suite'
    result = subprocess.run(
        [node, '--experimental-vm-modules', '--test', 'tests/desktop/plugin.test.mjs'],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
