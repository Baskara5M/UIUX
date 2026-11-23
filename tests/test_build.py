"""
The data model in neo4j-datamodel/ can be rebuilt, byte for byte, from the
sources in data/ by tools/datamodel/build.py. If this fails, the committed
CSVs no longer match what the build makes: rebuild them, or find out why.

    python -m unittest tests.test_build        (a few seconds)
"""
import filecmp
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "tools", "datamodel", "build.py")
DATA = os.path.join(ROOT, "neo4j-datamodel")


class Rebuild(unittest.TestCase):
    def test_build_reproduces_the_committed_data(self):
        with tempfile.TemporaryDirectory() as out:
            run = subprocess.run([sys.executable, BUILD, "100km", out], cwd=ROOT,
                                 capture_output=True, text=True, timeout=600)
            self.assertEqual(run.returncode, 0, run.stderr)
            made = sorted(os.path.relpath(os.path.join(d, f), out)
                          for d, _, files in os.walk(out) for f in files)
            self.assertTrue(made, "the build wrote nothing")
            differ = [f for f in made
                      if not os.path.isfile(os.path.join(DATA, f))
                      or not filecmp.cmp(os.path.join(out, f), os.path.join(DATA, f), shallow=False)]
            self.assertEqual(differ, [], "these files differ from neo4j-datamodel/: %s" % differ)


if __name__ == "__main__":
    unittest.main()
