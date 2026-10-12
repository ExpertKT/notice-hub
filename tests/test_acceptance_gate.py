from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
P0_INVARIANTS = (
    "health and token home",
    "httpServers and one NapCatWinBootMain",
    "napcat_running during QR",
    "fresh env subscriptions",
    "task lifecycle and ics",
    "tray quit cleanup",
    "doctor broken hint",
)


class AcceptanceGateContractTests(unittest.TestCase):
    def test_gate_has_all_layers_and_safe_defaults(self):
        text = (ROOT / "tools" / "gate.ps1").read_text(encoding="ascii")
        for layer in ("L0 syntax", "L1 unittest", "L2 package PYZ", "L3 cold install"):
            self.assertIn(layer, text)
        self.assertIn("tools\\syntaxcheck.py", text)
        self.assertIn("tools\\packcheck.py", text)
        self.assertIn("packaging\\QQ-Notice-Hub.zip", text)
        self.assertIn("[guid]::NewGuid()", text)
        self.assertIn("Remove-Item -LiteralPath $InstallDir", text)
        self.assertNotIn("D:\\QQ-Notice-Hub-beta'", text)
        self.assertIn("TcpListener", text)
        self.assertIn("Get-NetTCPConnection -LocalPort $Port", text)


    def test_e2e_mentions_each_machine_check(self):
        text = (ROOT / "tools" / "e2e-install.ps1").read_text(encoding="ascii")
        required = (
            "/api/health", "/api/napcat/doctor", "/api/subscriptions",
            "/calendar.ics", "BEGIN:VCALENDAR", "httpServers", "napcat_running", "VerifyTrayQuit",
            "Get-NetTCPConnection", "NapCatWinBootMain", "InstallProcesses",
            "ExecutablePath", "BROWSER = 'nonexistent-browser-probe'",
        )
        for marker in required:
            self.assertIn(marker, text)

    def test_p0_inventory_is_explicit(self):
        self.assertEqual(len(P0_INVARIANTS), 7)

    def test_l0_checks_imported_page_scripts(self):
        text = (ROOT / "tools" / "syntaxcheck.py").read_text(encoding="ascii")
        self.assertIn("PAGE_HTML", text)
        self.assertIn("node", text)
        self.assertIn("--check", text)
        self.assertIn("SKIP", text)


if __name__ == "__main__":
    unittest.main()
