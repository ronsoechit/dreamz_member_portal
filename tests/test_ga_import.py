from datetime import date
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path

from ga_import import (
    derive_contract_type,
    parse_ga_date,
    parse_gymassistant_export,
    parse_money,
)


XML_SAMPLE = """<?xml version="1.0"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
          xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
  <Worksheet ss:Name="Report">
    <Table>
      <Row>
        <Cell><Data ss:Type="String">1206</Data></Cell>
        <Cell><Data ss:Type="String">Example, Member</Data></Cell>
        <Cell><Data ss:Type="String">contract Dreamz 12 m</Data></Cell>
        <Cell><Data ss:Type="String">ACTIVE</Data></Cell>
        <Cell><Data ss:Type="String">ACH</Data></Cell>
        <Cell><Data ss:Type="String">55.00</Data></Cell>
        <Cell><Data ss:Type="String">* 01/05/2025</Data></Cell>
        <Cell><Data ss:Type="String">05/10/2021</Data></Cell>
        <Cell><Data ss:Type="String">05/10/2022</Data></Cell>
        <Cell><Data ss:Type="String">05/10/2021</Data></Cell>
        <Cell><Data ss:Type="String">29/03/2025</Data></Cell>
        <Cell><Data ss:Type="String">55.00</Data></Cell>
        <Cell><Data ss:Type="String">member@example.com</Data></Cell>
        <Cell><Data ss:Type="String">0.00</Data></Cell>
      </Row>
      <Row>
        <Cell><Data ss:Type="String">149</Data></Cell>
        <Cell><Data ss:Type="String">Flex, Member</Data></Cell>
        <Cell><Data ss:Type="String">no contract 1 month</Data></Cell>
        <Cell><Data ss:Type="String">ACTIVE</Data></Cell>
        <Cell><Data ss:Type="String">Monthly</Data></Cell>
        <Cell><Data ss:Type="String">80.00</Data></Cell>
        <Cell><Data ss:Type="String">15/03/2025</Data></Cell>
        <Cell><Data ss:Type="String">27/06/2011</Data></Cell>
        <Cell><Data ss:Type="String">00/00/0000</Data></Cell>
        <Cell><Data ss:Type="String">27/06/2011</Data></Cell>
        <Cell><Data ss:Type="String">15/02/2025</Data></Cell>
        <Cell><Data ss:Type="String">80.00</Data></Cell>
        <Cell><Data ss:Type="String"></Data></Cell>
        <Cell><Data ss:Type="String">3.00</Data></Cell>
      </Row>
    </Table>
  </Worksheet>
</Workbook>
"""

BACKUP_SAMPLE = """MN=1206
LN=Damon
FN=Norluze
CB=20211005
CE=20221005
PU=20260601
LP=20260429
SU=20211005
MTN=contract Dreamz 12 months
R$=400
BT=1 MONTHS EFT
N$=6500
PH=
PM=701-0000
BD=00000000
TV=12
EM=noortje_lies@hotmail.com
ST=0
$B=0
-
"""


class GymAssistantImportTests(unittest.TestCase):
    def write_temp(self, suffix, content):
        handle = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="latin-1")
        with handle:
            handle.write(content)
        return Path(handle.name)

    def test_parse_money(self):
        self.assertEqual(parse_money("1,234.50"), 1234.5)
        self.assertEqual(parse_money(""), 0.0)
        self.assertEqual(parse_money("bad"), 0.0)

    def test_parse_ga_date_strips_markers_and_zero_dates(self):
        parsed = parse_ga_date("* 15/03/2025")
        self.assertEqual(parsed.value, date(2025, 3, 15))
        self.assertEqual(parsed.marker, "*")
        self.assertEqual(parse_ga_date("d     01/05/2025").marker, "d")
        self.assertIsNone(parse_ga_date("00/00/0000").value)

    def test_derive_contract_type(self):
        self.assertEqual(derive_contract_type("contract Dreamz 6 mo"), "6-months")
        self.assertEqual(derive_contract_type("Contract 12 months 2"), "12-months")
        self.assertEqual(derive_contract_type("no contract 1 month"), "No-Contract")

    def test_parse_xml_spreadsheet_export(self):
        path = self.write_temp(".xls", XML_SAMPLE)
        result = parse_gymassistant_export(path)

        self.assertEqual(result.issues, [])
        self.assertEqual(len(result.members), 2)

        contract = result.members[0]
        self.assertEqual(contract["member_id"], "1206")
        self.assertEqual(contract["billing_status"], "ACTIVE")
        self.assertEqual(contract["billing_type"], "ACH")
        self.assertEqual(contract["billing_amount"], 55.0)
        self.assertEqual(contract["due_date"], date(2025, 5, 1))
        self.assertEqual(contract["due_date_marker"], "*")
        self.assertEqual(contract["start_date"], date(2021, 10, 5))
        self.assertEqual(contract["end_date"], date(2022, 10, 5))
        self.assertEqual(contract["contract_type"], "12-months")
        self.assertEqual(contract["next_payment"], date(2025, 5, 1))

        flex = result.members[1]
        self.assertIsNone(flex["end_date"])
        self.assertEqual(flex["contract_type"], "No-Contract")
        self.assertEqual(flex["balance"], 3.0)

    def test_parse_fixed_width_report_txt_fallback(self):
        content = textwrap.dedent(
            """\
              Member  Member                    Membership            Billing        Billing          Billing       Due         Contract    Contract    Signup         Last Pd          Last Pd  Email                                Current
              Number  Name                      Type                  Status         Option            Amount       Date        Begin       End         Date           Date              Amount                                       Balance
            ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
                1206  Example, Member           contract Dreamz 12 m  ACTIVE         ACH              55.00        01/05/2025   05/10/2021  05/10/2022  05/10/2021     29/03/2025         55.00  member@example.com                      0.00
            """
        )
        path = self.write_temp(".txt", content)
        result = parse_gymassistant_export(path)

        self.assertEqual(len(result.members), 1)
        member = result.members[0]
        self.assertEqual(member["member_id"], "1206")
        self.assertEqual(member["contract_type"], "12-months")
        self.assertEqual(member["billing_type"], "ACH")
        self.assertEqual(member["due_date"], date(2025, 5, 1))

    def test_parse_members_btx_backup_export(self):
        path = self.write_temp(".btx", BACKUP_SAMPLE)
        result = parse_gymassistant_export(path)

        self.assertEqual(result.issues, [])
        self.assertEqual(len(result.members), 1)
        member = result.members[0]
        self.assertEqual(member["member_id"], "1206")
        self.assertEqual(member["name"], "Damon, Norluze")
        self.assertEqual(member["contract_type"], "12-months")
        self.assertEqual(member["billing_type"], "1 MONTHS EFT")
        self.assertEqual(member["billing_amount"], 65.0)
        self.assertEqual(member["last_payment_amount"], 4.0)
        self.assertEqual(member["due_date"], date(2026, 6, 1))
        self.assertEqual(member["next_payment"], date(2026, 6, 1))
        self.assertEqual(member["last_payment"], date(2026, 4, 29))
        self.assertEqual(member["start_date"], date(2021, 10, 5))
        self.assertEqual(member["end_date"], date(2022, 10, 5))
        self.assertEqual(member["signup_date"], date(2021, 10, 5))
        self.assertEqual(member["mobile"], "701-0000")
        self.assertEqual(member["visits"], 12)
        self.assertTrue(member["is_active"])

    def test_parse_members_btx_imports_birthdate_from_gymassistant(self):
        content = BACKUP_SAMPLE.replace("BD=00000000", "BD=19920415")
        path = self.write_temp(".btx", content)
        result = parse_gymassistant_export(path)

        self.assertEqual(result.issues, [])
        self.assertEqual(result.members[0]["birthdate"], date(1992, 4, 15))

    def test_parse_gbu_backup_export(self):
        with tempfile.NamedTemporaryFile(suffix=".gbu", delete=False) as handle:
            path = Path(handle.name)

        with zipfile.ZipFile(path, "w") as backup:
            backup.writestr("Members.btx", BACKUP_SAMPLE)

        result = parse_gymassistant_export(path)

        self.assertEqual(len(result.members), 1)
        self.assertEqual(result.members[0]["member_id"], "1206")


if __name__ == "__main__":
    unittest.main()
