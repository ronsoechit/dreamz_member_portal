from datetime import date
import csv
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

BACKUP_LINKED_SAMPLE = BACKUP_SAMPLE.replace("$B=0\n-", "$B=0\nDPL=2000\n-", 1) + """MN=2000
LN=Dependent
FN=Member
PU=20260601
LP=20260429
MTN=contract Dreamz 12 months
R$=6500
BT=1 MONTHS EFT
N$=6500
ST=0
$B=0
DPL=
-
"""


class GymAssistantImportTests(unittest.TestCase):
    def write_temp(self, suffix, content):
        handle = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="latin-1")
        with handle:
            handle.write(content)
        return Path(handle.name)

    def write_official_csv(self, rows):
        fieldnames = [
            "MemberNum",
            "LastName",
            "FirstName",
            "MemberType",
            "BillingOption",
            "DueDate",
            "BillingAmount",
            "LastPaidDate",
            "LastPaidAmount",
            "SignupDate",
            "ContractEnd",
            "ContractBegin",
            "BirthDate",
            "Email",
            "BillingStatus",
            "CurrentBalance",
            "IsDeleted",
            "HomePhone",
            "MobilePhone",
            "NUM_VISITS_TOTAL",
            "ResponsibleMemberNum",
        ]
        handle = tempfile.NamedTemporaryFile(
            "w",
            suffix=".csv",
            delete=False,
            encoding="cp1252",
            newline="",
        )
        with handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
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

    def test_parse_members_btx_imports_linked_memberships(self):
        path = self.write_temp(".btx", BACKUP_LINKED_SAMPLE)
        result = parse_gymassistant_export(path)

        members = {member["member_id"]: member for member in result.members}
        self.assertEqual(members["1206"]["dependent_member_ids"], "2000")
        self.assertEqual(members["2000"]["responsible_member_id"], "1206")

    def test_parse_gbu_backup_export(self):
        with tempfile.NamedTemporaryFile(suffix=".gbu", delete=False) as handle:
            path = Path(handle.name)

        with zipfile.ZipFile(path, "w") as backup:
            backup.writestr("Members.btx", BACKUP_SAMPLE)

        result = parse_gymassistant_export(path)

        self.assertEqual(len(result.members), 1)
        self.assertEqual(result.members[0]["member_id"], "1206")

    def test_parse_official_csv_handles_quoted_names_statuses_and_relationships(self):
        path = self.write_official_csv([
            {
                "MemberNum": "1206",
                "LastName": "Last, Jr.",
                "FirstName": "José",
                "MemberType": "contract Dreamz 12 m",
                "BillingOption": "ACH",
                "DueDate": "01/08/2026",
                "BillingAmount": "65.00",
                "LastPaidDate": "01/07/2026",
                "LastPaidAmount": "65.00",
                "SignupDate": "01/01/2026",
                "ContractEnd": "01/01/2027",
                "ContractBegin": "01/01/2026",
                "BirthDate": "02/03/1990",
                "Email": "jose@example.com",
                "BillingStatus": "ACTIVE",
                "CurrentBalance": "0.00",
                "IsDeleted": "0",
                "MobilePhone": "700-0000",
                "NUM_VISITS_TOTAL": "12",
            },
            {
                "MemberNum": "1207",
                "LastName": "Dependent",
                "FirstName": "Member",
                "MemberType": "Delfins Fitness",
                "BillingOption": "Monthly",
                "DueDate": "01/08/2026",
                "BillingAmount": "0.00",
                "LastPaidDate": "01/07/2026",
                "LastPaidAmount": "0.00",
                "SignupDate": "01/01/2026",
                "ContractEnd": "00/00/0000",
                "ContractBegin": "01/01/2026",
                "BirthDate": "00/00/0000",
                "BillingStatus": "TERMINATED",
                "CurrentBalance": "0.00",
                "IsDeleted": "0",
                "ResponsibleMemberNum": "1206",
            },
            {
                "MemberNum": "1208",
                "LastName": "Deleted",
                "FirstName": "Member",
                "MemberType": "Delfins Fitness",
                "BillingOption": "Monthly",
                "BillingStatus": "ACTIVE",
                "IsDeleted": "1",
            },
        ])

        result = parse_gymassistant_export(path)

        self.assertEqual(len(result.members), 2)
        members = {member["member_id"]: member for member in result.members}
        self.assertEqual(members["1206"]["name"], "Last, Jr., José")
        self.assertEqual(members["1206"]["billing_option"], "1 MONTHS EFT")
        self.assertEqual(members["1206"]["birthdate"], date(1990, 3, 2))
        self.assertEqual(members["1206"]["dependent_member_ids"], "1207")
        self.assertTrue(members["1206"]["is_active"])
        self.assertEqual(members["1207"]["billing_status"], "TERMINATED")
        self.assertFalse(members["1207"]["is_active"])
        self.assertEqual(members["1207"]["responsible_member_id"], "1206")
        self.assertNotIn("1208", members)

    def test_official_csv_unknown_status_is_never_active_and_is_critical(self):
        path = self.write_official_csv([{
            "MemberNum": "1300",
            "LastName": "Unknown",
            "FirstName": "Status",
            "MemberType": "Delfins Fitness",
            "BillingOption": "Monthly",
            "BillingStatus": "",
            "IsDeleted": "0",
        }])

        result = parse_gymassistant_export(path)

        self.assertEqual(result.members[0]["billing_status"], "UNKNOWN")
        self.assertIsNone(result.members[0]["is_active"])
        self.assertTrue(any(issue.critical for issue in result.issues))

    def test_backup_missing_st_is_never_assumed_active(self):
        path = self.write_temp(
            ".btx",
            "MN=1400\nLN=Missing\nFN=Status\nMTN=Delfins Fitness\n-\n",
        )

        result = parse_gymassistant_export(path)

        self.assertEqual(result.members[0]["billing_status"], "UNKNOWN")
        self.assertIsNone(result.members[0]["is_active"])
        self.assertTrue(any(issue.critical for issue in result.issues))

    def test_official_csv_requires_canonical_headers(self):
        path = self.write_temp(".csv", "MemberNum,LastName\n100,Tester\n")

        with self.assertRaisesRegex(ValueError, "missing required column"):
            parse_gymassistant_export(path)


if __name__ == "__main__":
    unittest.main()
