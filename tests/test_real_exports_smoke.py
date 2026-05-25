from collections import Counter
from pathlib import Path
import unittest

from ga_import import parse_gymassistant_export


class RealGymAssistantExportSmokeTests(unittest.TestCase):
    def test_current_xls_and_txt_exports_parse_consistently(self):
        xls_path = Path("data/Member Detail Report.xls")
        txt_path = Path("data/Report.txt")
        if not xls_path.exists() or not txt_path.exists():
            self.skipTest("Local GymAssistant sample exports are not available")

        xls = parse_gymassistant_export(xls_path)
        txt = parse_gymassistant_export(txt_path)

        self.assertGreater(len(xls.members), 0)
        self.assertEqual(len(xls.members), len(txt.members))
        self.assertEqual(
            {member["member_id"] for member in xls.members},
            {member["member_id"] for member in txt.members},
        )
        self.assertEqual(
            Counter(member["contract_type"] for member in xls.members),
            Counter(member["contract_type"] for member in txt.members),
        )
        self.assertLessEqual(len(xls.issues), 10)
        self.assertLessEqual(len(txt.issues), 10)


if __name__ == "__main__":
    unittest.main()
