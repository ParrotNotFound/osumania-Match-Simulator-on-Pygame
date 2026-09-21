# src/utils/excel.py
"""极简 xlsx 写出：不依赖任何第三方库，直接按 OOXML 规范拼一个单表工作簿。

.xlsx 本质上就是个 zip，最少要有这几个部件：

    [Content_Types].xml
    _rels/.rels
    xl/workbook.xml
    xl/_rels/workbook.xml.rels
    xl/worksheets/sheet1.xml

单元格一律用 inlineStr 把文本写在单元格里，省掉 sharedStrings 那一份；
数字写成数值单元格，打开后可以直接求和。
"""
from __future__ import annotations

import os
import zipfile
from typing import Any, List, Optional, Sequence, Union

Cell = Union[str, int, float, None]

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""


def _escape(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _column_name(index: int) -> str:
    """0 -> A，25 -> Z，26 -> AA"""
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def _cell_xml(reference: str, value: Cell) -> str:
    # 空值也要写一个空单元格占位，否则后面几列会被 Excel 往左挤
    if value is None or value == "":
        return f'<c r="{reference}"/>'
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, (int, float)):
        return f'<c r="{reference}"><v>{value}</v></c>'
    return (f'<c r="{reference}" t="inlineStr"><is>'
            f'<t xml:space="preserve">{_escape(value)}</t></is></c>')


def _sheet_xml(rows: Sequence[Sequence[Cell]]) -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
             "<sheetData>"]
    for row_index, row in enumerate(rows, start=1):
        cells = "".join(_cell_xml(f"{_column_name(col)}{row_index}", value)
                        for col, value in enumerate(row))
        parts.append(f'<row r="{row_index}">{cells}</row>')
    parts.append("</sheetData></worksheet>")
    return "".join(parts)


def write_sheet(path: str, rows: Sequence[Sequence[Cell]], sheet_name: str = "成绩") -> str:
    """把二维数据写成一个 xlsx 文件，返回其绝对路径。"""
    absolute = os.path.abspath(path)
    parent = os.path.dirname(absolute)
    if parent:
        os.makedirs(parent, exist_ok=True)

    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<sheets><sheet name="{_escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>'
                "</workbook>")

    with zipfile.ZipFile(absolute, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        archive.writestr("xl/worksheets/sheet1.xml", _sheet_xml(rows))
    return absolute


def _column_index(reference: str) -> int:
    """'C12' -> 2（A=0）"""
    letters = "".join(ch for ch in reference if ch.isalpha())
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch.upper()) - ord("A") + 1)
    return index - 1


def read_sheet(path: str) -> List[List[Optional[str]]]:
    """把 write_sheet 写出来的文件读回二维文本，用来校验写出的内容（Excel 里也能直接打开）。"""
    import xml.etree.ElementTree as ET

    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))

    result: List[List[Optional[str]]] = []
    for row in root.iter(f"{namespace}row"):
        values: List[Optional[str]] = []
        for cell in row.iter(f"{namespace}c"):
            text = cell.find(f"{namespace}is/{namespace}t")
            if text is not None:
                value: Optional[str] = text.text or ""
            else:
                number = cell.find(f"{namespace}v")
                value = number.text if number is not None else ""
            # 按单元格自己的 r 属性定位，空单元格也不会让后面的列错位
            column = _column_index(cell.get("r", ""))
            while len(values) < column:
                values.append(None)
            values.append(value)
        result.append(values)
    return result


def match_result_rows(records: Sequence[dict], scores: Sequence[int],
                      winner_name: str = "") -> List[List[Cell]]:
    """常规比赛的一张表：每一局双方得分 + 本局胜者，最后附上大比分。

    `records` 就是 Match.round_records，`scores` 是两队大比分。
    """
    if not records:
        return []
    team_count = len(records[0]["teams"])
    names = [team["name"] for team in records[0]["teams"]]

    header: List[Cell] = ["局", "曲目", "曲目 id"] + names + ["本局胜者"]
    rows: List[List[Cell]] = [header]

    for index, record in enumerate(records):
        totals = [team["total"] for team in record["teams"]]
        best = max(range(len(totals)), key=lambda i: totals[i]) if totals else 0
        winner = names[best] if best < len(names) else ""
        rows.append([index + 1, record.get("song_title", ""), record.get("song_id", "")]
                    + [round(value, 1) for value in totals] + [winner])

    rows.append([])
    summary: List[Cell] = ["大比分", "", ""]
    for index in range(team_count):
        summary.append(scores[index] if index < len(scores) else "")
    summary.append(f"{winner_name} 获胜" if winner_name else "")
    rows.append(summary)

    rows.append([])
    rows.append(["说明", "每行是一局；分数是该队三名队员本局的合计"])
    return rows


def score_match_rows(records: Sequence[dict], ranks: Sequence[int]) -> List[List[Cell]]:
    """把计分赛的逐局记录整理成表格。

    每支队占若干行：先每个队员一行（每首歌的分数），再来一行"队伍总分"；
    最后一列是这名队员（或这支队伍）的合计。
    """
    if not records:
        return []
    track_count = len(records)
    team_count = len(records[0]["teams"])

    header: List[Cell] = ["队伍", "队员"] + [f"Track{i + 1}" for i in range(track_count)] + ["总分"]
    rows: List[List[Cell]] = [header]

    for team_index in range(team_count):
        first = records[0]["teams"][team_index]
        team_name = first["name"]
        for player_index, (player_name, _) in enumerate(first["players"]):
            scores = [record["teams"][team_index]["players"][player_index][1]
                      for record in records]
            rows.append([team_name, player_name] + scores + [round(sum(scores), 1)])
        totals = [record["teams"][team_index]["total"] for record in records]
        rows.append([team_name, f"队伍总分（第 {ranks[team_index]} 名）"]
                    + totals + [round(sum(totals), 1)])

    # 顺便把每首歌的歌名附在表格末尾，方便对照
    rows.append([])
    rows.append(["曲目", "Track", "歌名"])
    for index, record in enumerate(records):
        rows.append([f"Track{index + 1}", record.get("song_id", ""), record.get("song_title", "")])
    return rows
