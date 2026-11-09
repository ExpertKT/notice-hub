"""parse_groups：只把真正的群返回，排除个人/临时会话，空标题照常返回。

依据 2026-11-08 真机解密快照（nh-tmp/dtprobe/result4.txt）：
type=2 的 cid=61632057492 是 subType=1 / visible_subdomains='personal' 的双人临时会话，
8 个名字列全空；其余 7 个群名都在 tbconversation.title。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from noticehub.dingtalk_receiver import DingTalkLocalReader

_FULL_COLUMNS = ("cid", "title", "memberCount", "type", "subType", "visible_subdomains")
_FULL_DEFS = (
    "cid TEXT, title TEXT, memberCount INTEGER, type INTEGER, "
    "subType INTEGER, visible_subdomains TEXT"
)
_LEGACY_COLUMNS = ("cid", "title", "memberCount", "type")
_LEGACY_DEFS = "cid TEXT, title TEXT, memberCount INTEGER, type TEXT"


def _db_bytes(rows, columns=_FULL_COLUMNS, defs=_FULL_DEFS):
    """造一张明文 tbconversation 表，返回其文件字节（parse_groups 接收的格式）。"""
    fd, name = tempfile.mkstemp(prefix="test-dt-groups-", suffix=".db")
    os.close(fd)
    path = Path(name)
    try:
        con = sqlite3.connect(str(path))
        con.execute(f"CREATE TABLE tbconversation ({defs})")
        con.executemany(
            "INSERT INTO tbconversation (%s) VALUES (%s)"
            % (",".join(columns), ",".join("?" for _ in columns)),
            [tuple(row.get(col, "") for col in columns) for row in rows],
        )
        con.commit()
        con.close()
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


def _cids(rows, **kwargs):
    return [row["cid"] for row in DingTalkLocalReader.parse_groups(_db_bytes(rows, **kwargs))]


class ParseGroupsPersonalFilterTest(unittest.TestCase):
    def test_subtype_1_conversation_is_excluded(self):
        rows = [
            {"cid": "g_real", "title": "项目群", "memberCount": 8, "type": 2, "subType": 0, "visible_subdomains": ""},
            {"cid": "61632057492", "title": "", "memberCount": 2, "type": 2, "subType": 1, "visible_subdomains": ""},
        ]
        self.assertEqual(_cids(rows), ["g_real"])

    def test_personal_visible_domain_is_excluded_when_subtype_zero(self):
        rows = [
            {"cid": "g_real", "title": "项目群", "memberCount": 8, "type": 2, "subType": 0, "visible_subdomains": ""},
            {"cid": "c_personal", "title": "", "memberCount": 2, "type": 2, "subType": 0, "visible_subdomains": "personal"},
        ]
        self.assertEqual(_cids(rows), ["g_real"])

    def test_empty_title_group_still_returned_with_empty_title(self):
        rows = [
            {"cid": "g_empty", "title": "", "memberCount": 4, "type": 2, "subType": 0, "visible_subdomains": ""},
        ]
        self.assertEqual(
            DingTalkLocalReader.parse_groups(_db_bytes(rows)),
            [{"cid": "g_empty", "title": "", "members": 4}],
        )

    def test_normal_groups_private_type_and_two_member_group_unchanged(self):
        rows = [
            {"cid": "g1", "title": "群一", "memberCount": 8, "type": 2, "subType": 0, "visible_subdomains": ""},
            {"cid": "g_two", "title": "两人群", "memberCount": 2, "type": 2, "subType": 0, "visible_subdomains": ""},
            {"cid": "g_one", "title": "欢迎试用钉钉群", "memberCount": 1, "type": 2, "subType": 0, "visible_subdomains": ""},
            {"cid": "u1", "title": "单聊", "memberCount": 2, "type": 1, "subType": 0, "visible_subdomains": ""},
            {"cid": "u2", "title": "单聊", "memberCount": 2, "type": "single", "subType": 0, "visible_subdomains": ""},
            {"cid": "u3", "title": "单聊", "memberCount": 2, "type": "private", "subType": 0, "visible_subdomains": ""},
        ]
        self.assertEqual(_cids(rows), ["g1", "g_two", "g_one"])

    def test_legacy_schema_without_personal_columns_still_filters_by_type(self):
        rows = [
            {"cid": "g1", "title": "项目群", "memberCount": 8, "type": "2"},
            {"cid": "u1", "title": "单聊", "memberCount": 2, "type": "1"},
        ]
        self.assertEqual(
            DingTalkLocalReader.parse_groups(_db_bytes(rows, columns=_LEGACY_COLUMNS, defs=_LEGACY_DEFS)),
            [{"cid": "g1", "title": "项目群", "members": 8}],
        )

    def test_real_snapshot_eight_rows_filter_to_seven(self):
        # 口径 = 2026-11-08 真机快照 8 条 type=2 会话（result4.txt:10-18）。
        rows = [
            {"cid": "67312386248", "title": "2023级25班", "memberCount": 105, "type": 2, "subType": 0, "visible_subdomains": ""},
            {"cid": "49152446729", "title": "904实验室", "memberCount": 36, "type": 2, "subType": 0, "visible_subdomains": ""},
            {"cid": "48497936559", "title": "904班家校群", "memberCount": 92, "type": 2, "subType": 0, "visible_subdomains": ""},
            {"cid": "50833723101", "title": "工作通知:SCAVS", "memberCount": 3, "type": 2, "subType": 0, "visible_subdomains": ""},
            {"cid": "51600222713", "title": "SCAVS", "memberCount": 1, "type": 2, "subType": 0, "visible_subdomains": ""},
            {"cid": "49216448318", "title": "工作通知:904班家校群", "memberCount": 3, "type": 2, "subType": 0, "visible_subdomains": ""},
            {"cid": "48672941284", "title": "欢迎试用钉钉群", "memberCount": 1, "type": 2, "subType": 0, "visible_subdomains": ""},
            {"cid": "61632057492", "title": "", "memberCount": 2, "type": 2, "subType": 1, "visible_subdomains": "personal"},
        ]
        self.assertEqual(len(rows), 8)  # 先锁原始 8 条口径
        result = DingTalkLocalReader.parse_groups(_db_bytes(rows))
        cids = [row["cid"] for row in result]
        self.assertEqual(len(result), 7)
        self.assertNotIn("61632057492", cids)
        self.assertEqual(cids, [
            "67312386248", "49152446729", "48497936559", "50833723101",
            "51600222713", "49216448318", "48672941284",
        ])


if __name__ == "__main__":
    unittest.main()
