# src/utils/excel.py
"""极简 xlsx 写出：不依赖任何第三方库，直接按 OOXML 规范拼一个工作簿。

.xlsx 本质上就是个 zip，最少要有这几个部件（每多一张表就多一组 sheetN）：

    [Content_Types].xml
    _rels/.rels
    xl/workbook.xml
    xl/_rels/workbook.xml.rels
    xl/worksheets/sheet1.xml
    xl/worksheets/sheet2.xml ...

单元格一律用 inlineStr 把文本写在单元格里，省掉 sharedStrings 那一份；
数字写成数值单元格，打开后可以直接求和。
"""
from __future__ import annotations

import os
import zipfile
from typing import Any, Iterable, List, Optional, Sequence, Tuple, Union

Cell = Union[str, int, float, None]
# 一张表：(表名, 二维数据)
Sheet = Tuple[str, Sequence[Sequence[Cell]]]

_WORKSHEET_CONTENT_TYPE = ("application/vnd.openxmlformats-officedocument"
                           ".spreadsheetml.worksheet+xml")

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
__OVERRIDES__</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
__RELATIONSHIPS__</Relationships>"""

_WORKBOOK = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
             'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
             '<sheets>__SHEETS__</sheets></workbook>')


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


def sheet_name_ok(name: str, used: Sequence[str] = ()) -> str:
    """把表名收拾成 Excel 认的：去掉非法字符、最多 31 字、不重名。"""
    text = "".join("_" if ch in "[]:*?/\\" else ch for ch in str(name)).strip() or "Sheet"
    text = text[:31]
    candidate = text
    index = 2
    while candidate in used:
        suffix = f"({index})"
        candidate = text[:31 - len(suffix)] + suffix
        index += 1
    return candidate


def write_sheets(path: str, sheets: Iterable[Sheet]) -> str:
    """把若干张表写进同一个 xlsx，返回其绝对路径。

    `sheets` 是 (表名, 二维数据) 的序列，**第一张就是打开时默认显示的那张**。
    只写一张表时用 write_sheet 更方便（它就是本函数的单表版本）。
    """
    pairs: List[Sheet] = list(sheets)
    if not pairs:
        raise ValueError("至少要有一张表")

    names: List[str] = []
    for name, _rows in pairs:
        names.append(sheet_name_ok(name, names))

    absolute = os.path.abspath(path)
    parent = os.path.dirname(absolute)
    if parent:
        os.makedirs(parent, exist_ok=True)

    overrides = "\n".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        f'ContentType="{_WORKSHEET_CONTENT_TYPE}"/>'
        for index in range(1, len(pairs) + 1))
    content_types = _CONTENT_TYPES.replace("__OVERRIDES__", overrides)

    sheets_xml = "".join(
        f'<sheet name="{_escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(names, start=1))
    workbook = _WORKBOOK.replace("__SHEETS__", sheets_xml)

    relationships = "\n".join(
        f'<Relationship Id="rId{index}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, len(pairs) + 1))
    workbook_rels = _WORKBOOK_RELS.replace("__RELATIONSHIPS__", relationships)

    with zipfile.ZipFile(absolute, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        for index, (_name, rows) in enumerate(pairs, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _sheet_xml(rows))
    return absolute


def write_sheet(path: str, rows: Sequence[Sequence[Cell]], sheet_name: str = "成绩") -> str:
    """把二维数据写成一个单表 xlsx 文件，返回其绝对路径。"""
    return write_sheets(path, [(sheet_name, rows)])


def _column_index(reference: str) -> int:
    """'C12' -> 2（A=0）"""
    letters = "".join(ch for ch in reference if ch.isalpha())
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch.upper()) - ord("A") + 1)
    return index - 1


def read_rows_typed(path: str, sheet: int = 1) -> List[List[Cell]]:
    """读回表格，数值单元格还原成数字、文本还原成字符串。

    追加历史成绩时必须用这个：如果按文本读回来再写出去，
    之前那些分数就会退化成"文本格式的数字"。
    `sheet` 是第几张表（1 起，和 write_sheets 里的顺序一致）。
    """
    import xml.etree.ElementTree as ET

    if sheet < 1:
        raise ValueError("sheet 从 1 开始数")
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read(f"xl/worksheets/sheet{sheet}.xml"))

    result: List[List[Cell]] = []
    for row in root.iter(f"{namespace}row"):
        values: List[Cell] = []
        for cell in row.iter(f"{namespace}c"):
            text = cell.find(f"{namespace}is/{namespace}t")
            if text is not None:
                value: Cell = text.text or ""
            else:
                number = cell.find(f"{namespace}v")
                if number is None or number.text is None:
                    value = ""
                else:
                    try:
                        number_value = float(number.text)
                        value = int(number_value) if number_value.is_integer() else number_value
                    except ValueError:
                        value = number.text
            column = _column_index(cell.get("r", ""))
            while len(values) < column:
                values.append("")
            values.append(value)
        result.append(values)
    return result


def _pad_row(row: Sequence[Cell], width: int) -> List[Cell]:
    cells = list(row)
    while len(cells) < width:
        cells.append("")
    return cells[:width]


def read_sheet(path: str, sheet: int = 1) -> List[List[Optional[str]]]:
    """把 write_sheet(s) 写出来的文件读回二维文本，用来校验写出的内容（Excel 里也能直接打开）。"""
    return [[None if cell is None else str(cell) for cell in row]
            for row in read_rows_typed(path, sheet=sheet)]


def append_score_match(path: str, block: Sequence[Sequence[Cell]], track_count: int,
                       sheet_name: str = "成绩") -> int:
    """把一场计分赛的成绩块**追加**到成绩表末尾（文件不存在就新建）。

    这样换一支队伍再打，新成绩是加在后面，不会把之前的覆盖掉。
    `block` 每行宽度应当是 4 + track_count + 1（场次/比赛/队伍/队员 + 各曲目 + 总分），
    第一列留空即可，由这里统一填场次号。
    已有的行如果比这次窄，会在曲目列补空；表头也会跟着加宽到最宽的那一场。
    返回这场比赛的场次号（1 起）。
    """
    existing: List[List[Cell]] = []
    if os.path.isfile(path):
        try:
            existing = read_rows_typed(path)
        except (OSError, zipfile.BadZipFile, KeyError) as error:
            print(f"警告：成绩表读不出来（{error}），这一场会重开一个表")
            existing = []

    header = existing[0] if existing else []
    data_rows = existing[1:] if existing else []

    # 兼容最早那版没有「场次/比赛」两列的成绩表：补上两列，整份算作第 1 场
    legacy = bool(header) and str(header[0]) != "场次"
    if legacy:
        data_rows = [["", ""] + list(row) for row in data_rows]
        print("提示：检测到旧格式的成绩表（没有场次列），已按第 1 场并入，之后继续往后追加")

    # 场次号 = 已有数据里出现过的最大场次 + 1
    numbers: List[int] = []
    for row in data_rows:
        try:
            numbers.append(int(float(str(row[0]))))
        except (IndexError, ValueError):
            continue
    match_no = (max(numbers) + 1) if numbers else (1 if not legacy else 2)

    numbered: List[List[Cell]] = []
    for row in block:
        cells = list(row)
        cells[0] = match_no
        numbered.append(cells)

    old_tracks = max(0, max((len(row) for row in data_rows), default=0) - 5)
    width = 4 + max(track_count, old_tracks) + 1
    new_header: List[Cell] = (["场次", "比赛", "队伍", "队员"]
                              + [f"Track{i + 1}" for i in range(width - 5)] + ["总分"])

    rows: List[List[Cell]] = [new_header]
    rows.extend(_pad_row(row, width) for row in data_rows)
    rows.extend(_pad_row(row, width) for row in numbered)
    write_sheet(path, rows, sheet_name=sheet_name)
    return match_no


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


def score_match_rows(records: Sequence[dict], ranks: Sequence[int],
                     match_name: str = "") -> List[List[Cell]]:
    """把一场计分赛的逐局记录整理成一个**成绩块**（不含表头，交给 append_score_match 追加）。

    每支队占若干行：先每个队员一行（每首歌的分数），再来一行"队伍总分"。
    开头还有一行"曲目"，在各 Track 列里写明那一场打的是哪首歌——
    因为成绩表是逐场累加的，没有这行就分不清第 2 场的 Track1 是哪首。
    行的宽度 = 4（场次/比赛/队伍/队员）+ 局数 + 1（总分），第一列（场次）由追加时填。
    """
    if not records:
        return []
    track_count = len(records)
    team_count = len(records[0]["teams"])

    # 曲目行：队伍列留空、队员列写"曲目"，各 Track 列写曲目 id
    rows: List[List[Cell]] = [[
        "", match_name, "", "曲目",
    ] + [record.get("song_id", "") for record in records] + [""]]

    for team_index in range(team_count):
        first = records[0]["teams"][team_index]
        team_name = first["name"]
        for player_index, (player_name, _) in enumerate(first["players"]):
            scores = [record["teams"][team_index]["players"][player_index][1]
                      for record in records]
            rows.append(["", match_name, team_name, player_name]
                        + scores + [round(sum(scores), 1)])
        totals = [record["teams"][team_index]["total"] for record in records]
        rows.append(["", match_name, team_name, f"队伍总分（第 {ranks[team_index]} 名）"]
                    + totals + [round(sum(totals), 1)])
    return rows
