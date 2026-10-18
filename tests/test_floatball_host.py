import json
import tempfile
import unittest
from pathlib import Path
from qq_live_digest.floatball_host import clamp_position, snap_position, process_matches_commandline, FloatBallHost, write_state

class FloatBallHostTests(unittest.TestCase):
    def test_clamps_overflow(self):
        self.assertEqual(clamp_position(-5, 999, 100, 80, (0, 0, 800, 600)), (0, 520))
    def test_snaps_each_edge(self):
        work=(0,0,800,600)
        self.assertEqual(snap_position(2,200,100,80,work)[0], 8)
        self.assertEqual(snap_position(690,200,100,80,work)[0], 692)
        self.assertEqual(snap_position(300,2,100,80,work)[1], 8)
        self.assertEqual(snap_position(300,518,100,80,work)[1], 512)
    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            host=FloatBallHost(Path(tmp)); write_state(host.state_path, 12, 34, True)
            self.assertEqual(json.loads(host.state_path.read_text(encoding='utf-8')), {'x':12,'y':34,'expanded':True})
            self.assertEqual(host._load_state()['x'],12)
    def test_process_commandline_filter(self):
        profile=Path(r'F:\probe\floatball-profile')
        self.assertTrue(process_matches_commandline('msedge.exe --user-data-dir=F:\\probe\\floatball-profile', profile))
        self.assertFalse(process_matches_commandline('msedge.exe --user-data-dir=F:\\other', profile))

if __name__ == '__main__': unittest.main()
