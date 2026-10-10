"""Convert OCR content with Pandoc, leaving page placement to the exporter."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pypandoc
from lxml import etree, html

MARKDOWN_FORMAT = (
    "markdown+tex_math_single_backslash+hard_line_breaks"
    "+raw_html-raw_tex-raw_attribute-yaml_metadata_block-pandoc_title_block-footnotes"
)

INLINE_TAGS = {"sup": "Superscript", "sub": "Subscript", "u": "Underline",
               "s": "Strikeout", "del": "Strikeout", "b": "Strong", "strong": "Strong",
               "i": "Emph", "em": "Emph"}


def format_html_inlines(items: list[Any]) -> list[Any]:
    """Translate balanced, attribute-free formatting tags; leave everything else literal."""
    output: list[Any] = []
    stack: list[tuple[str, dict[str, Any], list[Any]]] = []
    for item in items:
        match = None
        if isinstance(item, dict) and item.get("t") == "RawInline" and item["c"][0] == "html":
            match = re.fullmatch(r"<(/?)(sup|sub|u|s|del|b|strong|i|em)\s*>", item["c"][1], re.IGNORECASE)
        if match and not match[1]:
            stack.append((match[2].lower(), item, output))
            output = []
        elif match and stack and match[1] and match[2].lower() == stack[-1][0]:
            tag, _, parent = stack.pop()
            parent.append({"t": INLINE_TAGS[tag], "c": output})
            output = parent
        else:
            output.append(item)
    while stack:
        _, opening, parent = stack.pop()
        parent.extend([opening, *output])
        output = parent
    return output


def normalize_math(source: str) -> str:
    """Group scripted ordinary parentheses for Pandoc/LibreOffice interoperability.

    Escaped delimiters, existing sizing commands and text command groups are
    untouched. This is a token-level compatibility fix, not a LaTeX parser.
    """
    tokens = re.findall(r"\\[a-zA-Z]+|\\.|[^\\]", source, re.DOTALL)
    stack: list[tuple[int, bool]] = []
    braces: list[bool] = []
    replacements: dict[int, str] = {}
    previous = ""
    text_commands = {r"\text", r"\textrm", r"\textnormal", r"\textbf", r"\textit", r"\operatorname"}
    for index, token in enumerate(tokens):
        if token == "{":
            braces.append(any(braces) or previous in text_commands)
        elif token == "}" and braces:
            braces.pop()
        elif not any(braces):
            if token == "(":
                stack.append((index, previous in {r"\left", r"\big", r"\Big", r"\bigg", r"\Bigg", r"\bigl", r"\Bigl"}))
            elif token == ")" and stack:
                opening, sized = stack.pop()
                following = next((value for value in tokens[index + 1:] if not value.isspace()), "")
                if not sized and previous != r"\right" and following in {"_", "^"}:
                    replacements[opening] = r"\left("
                    replacements[index] = r"\right)"
        if not token.isspace():
            previous = token
    return "".join(replacements.get(index, token) for index, token in enumerate(tokens))


def dotted_list(source: str) -> list[re.Match[str]]:
    matches = [re.fullmatch(r"\s*(\d+(?:\.\d+)*)([.)])\s+(.+)", line) for line in source.splitlines() if line.strip()]
    if not matches or not all(matches) or not any("." in item[1] for item in matches):
        return []
    previous: list[int] = []
    for item in matches:
        path = [int(value) for value in item[1].split(".")]
        if len(path) > min(9, len(previous) + 1) or path[:-1] != previous[:len(path) - 1]:
            return []
        if previous and path[-1] != (1 if len(path) > len(previous) else previous[len(path) - 1] + 1):
            return []
        previous = path
    return matches


def normalize_list_indents(source: str) -> str:
    """Recover explicit dotted hierarchy and common two-space Markdown nesting."""
    lines = source.splitlines()
    dotted = dotted_list(source)
    if dotted:
        # Pandoc still parses and owns the actual native lists and their numbering.
        return "\n".join("    " * item[1].count(".") + item[1].split(".")[-1] + ". " + item[3] for item in dotted)
    if any(re.match(r"^  [*+-] ", line) for line in lines) and not any(line.startswith("    ") for line in lines):
        return "\n".join("  " + line if re.match(r"^  [*+-] ", line) else line for line in lines)
    return source


def run_pandoc(source: str, *arguments: str) -> bytes:
    """Use the bundled executable; never download an engine at export time."""
    executable = Path(pypandoc.__file__).parent / "files" / "pandoc"
    if not executable.is_file():
        executable = executable.with_suffix(".exe")
    if not executable.is_file():
        raise ValueError("Install the Word exporter with: uv sync --extra word")
    try:
        result = subprocess.run(
            [str(executable), "--sandbox", *arguments],
            input=source.encode("utf-8"),
            capture_output=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Pandoc conversion exceeded 120 seconds") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Pandoc conversion failed: {detail}")
    return result.stdout


def read_content(source: str, format: str) -> dict[str, Any]:
    return json.loads(run_pandoc(source, "--from", format, "--to", "json"))


def plain_blocks(source: str) -> list[dict[str, Any]]:
    """Preserve unsupported source literally, including its line breaks."""
    inlines: list[dict[str, Any]] = []
    for index, line in enumerate(source.split("\n")):
        if index:
            inlines.append({"t": "LineBreak"})
        inlines.append({"t": "Str", "c": line})
    return [{"t": "Para", "c": inlines}]


def normalize_table(source: str) -> str:
    """Keep header row spans in the same Pandoc row group as body cells."""
    root = html.fragment_fromstring(source, create_parent="div")
    tables = root.xpath(".//table")
    if len(tables) != 1 or len(root) != 1 or root[0].tag != "table":
        raise ValueError("expected exactly one HTML table")
    if (root.text or "").strip() or (root[0].tail or "").strip():
        raise ValueError("unexpected content outside the table")
    table = tables[0]
    rows = table.xpath("./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr")
    if not rows:
        raise ValueError("table has no rows")
    # Broken/truncated markup can otherwise be silently repaired by the HTML reader.
    if "</table>" not in source.lower():
        raise ValueError("table is incomplete")
    occupied: set[tuple[int, int]] = set()
    for row_index, row in enumerate(rows):
        column = 0
        cells = row.xpath("./td | ./th")
        if not cells and not any(r == row_index for r, _ in occupied):
            raise ValueError("table row has no cells")
        for cell in cells:
            while (row_index, column) in occupied:
                column += 1
            rowspan = int(cell.get("rowspan", "1"))
            colspan = int(cell.get("colspan", "1"))
            if not 1 <= rowspan <= len(rows) - row_index or not 1 <= colspan <= 1000:
                raise ValueError("invalid table cell span")
            positions = {
                (r, c)
                for r in range(row_index, row_index + rowspan)
                for c in range(column, column + colspan)
            }
            if occupied.intersection(positions):
                raise ValueError("overlapping table cell spans")
            occupied.update(positions)
            column += colspan
            if cell.tag == "th":
                cell.tag = "td"
                strong = etree.Element("strong")
                strong.text = cell.text
                cell.text = None
                for child in list(cell):
                    strong.append(child)
                cell.append(strong)
    body = etree.Element("tbody")
    for row in rows:
        body.append(row)
    for child in list(table):
        if child.tag in {"thead", "tbody", "tfoot"}:
            table.remove(child)
    table.append(body)
    return html.tostring(table, encoding="unicode")


def sanitize_content(value: Any, warnings: list[str], label: str) -> Any:
    """Only source-image crops may become images; OCR markup cannot fetch files."""
    if isinstance(value, list):
        return [sanitize_content(item, warnings, label) for item in format_html_inlines(value)]
    if not isinstance(value, dict):
        return value
    kind = value.get("t")
    if kind == "Image":
        warnings.append(f"{label}: inline image markup kept as alt text; use Picture regions for images")
        return {"t": "Span", "c": [["", [], []], value["c"][1]]}
    if kind in {"RawBlock", "RawInline"}:
        warnings.append(f"{label}: unsupported markup kept as editable source text")
        source = value["c"][1]
        return plain_blocks(source)[0] if kind == "RawBlock" else {"t": "Str", "c": source}
    return {key: sanitize_content(item, warnings, label) for key, item in value.items()}


def region_blocks(
    category: str, source: str, label: str, warnings: list[str]
) -> list[dict[str, Any]]:
    if category == "Formula":
        formula = source.strip()
        for opening, closing in (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$")):
            if formula.startswith(opening) and formula.endswith(closing):
                formula = formula[len(opening) : -len(closing)].strip()
                break
        return [{"t": "Para", "c": [{"t": "Math", "c": [{"t": "DisplayMath"}, formula]}]}]
    if category == "Picture":
        return []
    if category == "Table":
        try:
            normalized = normalize_table(source)
            blocks = read_content(normalized, "html+tex_math_dollars+tex_math_single_backslash")["blocks"]
            if len(blocks) != 1 or blocks[0]["t"] != "Table":
                raise ValueError("Pandoc did not recognize the table")
        except (ValueError, etree.ParserError) as exc:
            warnings.append(f"{label}: table kept as editable HTML source ({exc})")
            return plain_blocks(source)
    else:
        if category == "List-item":
            # OCR frequently supplies a literal bullet that Markdown does not
            # recognize. Consume it before creating the native list marker.
            source = re.sub(r"^(\s*)[•●○◆◇▪■►‣]\s*", r"\1- ", source, flags=re.MULTILINE)
        blocks = read_content(normalize_list_indents(source), MARKDOWN_FORMAT)["blocks"]
        if category in {"Title", "Section-header"} and len(blocks) == 1 and blocks[0]["t"] == "Para":
            level = 1 if category == "Title" else 2
            blocks = [{"t": "Header", "c": [level, ["", [], []], blocks[0]["c"]]}]
        if category == "List-item" and blocks and not any(
            block["t"] in {"BulletList", "OrderedList"} for block in blocks
        ):
            blocks = [{"t": "BulletList", "c": [blocks]}]
    return sanitize_content(blocks, warnings, label)


def record_math(value: Any, label: str, formulas: dict[str, list[str]], originals: dict[str, str]) -> None:
    if isinstance(value, dict):
        if value.get("t") == "Math":
            source = value["c"][1]
            normalized = normalize_math(source)
            value["c"][1] = normalized
            originals[normalized] = source
            formulas.setdefault(normalized, []).append(label)
        for child in value.values():
            record_math(child, label, formulas, originals)
    elif isinstance(value, list):
        for child in value:
            record_math(child, label, formulas, originals)


def restore_failed_math(value: Any, failed: set[str], originals: dict[str, str]) -> Any:
    if isinstance(value, list):
        return [restore_failed_math(item, failed, originals) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("t") == "Math" and value["c"][1] in failed:
        return {"t": "Str", "c": originals[value["c"][1]]}
    return {key: restore_failed_math(item, failed, originals) for key, item in value.items()}


def convert_regions(regions: list[Any], directory: Path, warnings: list[str]) -> Path:
    """Write all regions together so Pandoc owns styles, numbering, and relationships."""
    document = read_content("", MARKDOWN_FORMAT)
    formulas: dict[str, list[str]] = {}
    originals: dict[str, str] = {}
    for index, region in enumerate(regions):
        # Generated raw XML is a boundary marker, never taken from OCR content.
        marker = (
            '<w:p><w:pPr><w:pStyle w:val="OcrBoundary"/></w:pPr>'
            f'<w:r><w:t>{index}</w:t></w:r></w:p>'
        )
        document["blocks"].append({"t": "RawBlock", "c": ["openxml", marker]})
        blocks = region_blocks(region.category, region.text, region.label, warnings)
        document["blocks"].extend(blocks)

        record_math(blocks, region.label, formulas, originals)
    for formula, labels in formulas.items():
        # Successful conversion cannot certify recognition of root scope.
        if formula.count(r"\sqrt") > 1:
            for label in dict.fromkeys(labels):
                warnings.append(f"{label}: multiple roots; verify radical scope against the source image (OCR grouping is not validated)")
    target = directory / "content.docx"
    log_path = directory / "pandoc.json"
    run_pandoc(
        json.dumps(document, ensure_ascii=False),
        "--from", "json", "--to", "docx", "--output", str(target), "--log", str(log_path),
    )
    failed = set()
    for item in json.loads(log_path.read_text(encoding="utf-8")):
        if item.get("type") == "CouldNotConvertTeXMath":
            failed.add(item["contents"])
            for label in dict.fromkeys(formulas.get(item["contents"], ["Document"])):
                warnings.append(f"{label}: unsupported equation kept as editable LaTeX: {originals.get(item['contents'], item['contents'])}")
        elif item.get("verbosity") in {"WARNING", "ERROR"}:
            warnings.append(f"Pandoc: {item.get('pretty', item['type'])}")
    if failed:
        run_pandoc(json.dumps(restore_failed_math(document, failed, originals), ensure_ascii=False),
                   "--from", "json", "--to", "docx", "--output", str(target))
    return target
