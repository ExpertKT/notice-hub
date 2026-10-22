import json
import os
import tempfile
import unittest
from pathlib import Path

if os.name != "nt":
    raise unittest.SkipTest("Windows-only floating-ball host")

from qq_live_digest.floatball_host import (
    CLOSED_SIZE,
    OPEN_SIZE,
    clamp_position,
    snap_position,
    process_matches_commandline,
    size_for_title,
    FloatBallHost,
    window_class,
    write_state,
)

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
    def test_title_layout_signal_ignores_loading_titles(self):
        self.assertEqual(size_for_title("nh-floatball:closed"), CLOSED_SIZE)
        self.assertEqual(size_for_title("nh-floatball:open"), OPEN_SIZE)
        self.assertIsNone(size_for_title("http://127.0.0.1:8765/floatball"))
        self.assertIsNone(size_for_title("nh-floatball:unknown"))

    def test_process_commandline_filter(self):
        profile=Path(r'F:\probe\floatball-profile')
        self.assertTrue(process_matches_commandline('msedge.exe --user-data-dir=F:\\probe\\floatball-profile', profile))
        self.assertFalse(process_matches_commandline('msedge.exe --user-data-dir=F:\\other', profile))

    def test_window_class_of_invalid_handle_is_empty(self):
        self.assertEqual(window_class(0), "")

    def test_find_window_needs_a_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            host=FloatBallHost(Path(tmp))
            self.assertIsNone(host._find_window())

    def test_stop_clears_pid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            host=FloatBallHost(Path(tmp))
            (Path(tmp)/'floatball.pid').write_text('999999', encoding='utf-8')
            host.stop()
            self.assertFalse((Path(tmp)/'floatball.pid').exists())

if __name__ == '__main__': unittest.main()
