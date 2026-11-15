"""Office Document Generator for JAI (Sprint 3).

Provides automated generation of professional Microsoft Word (.docx) documents,
markdown-to-docx compilation, and styled Microsoft Excel (.xlsx) workbooks.
Outputs directly into the user's active Microsoft OneDrive Documents folder.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Tuple, Union

# Windows PowerShell UTF-8 console output compatibility
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logger = logging.getLogger("jai.office_generator")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [OfficeDocGen] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Default destination: Active Microsoft OneDrive folder, falling back to local vault
ONEDRIVE_PATH = Path("C:/Users/gjaga/OneDrive/Documents/JAI_Generated")
FALLBACK_PATH = BASE_DIR / "vault" / "personal" / "documents"


def get_default_output_dir() -> Path:
    """Return the preferred destination directory for generated Office documents."""
    try:
        if Path("C:/Users/gjaga/OneDrive/Documents").exists():
            ONEDRIVE_PATH.mkdir(parents=True, exist_ok=True)
            return ONEDRIVE_PATH
    except Exception as e:
        logger.warning(f"Could not initialize OneDrive directory: {e}")

    FALLBACK_PATH.mkdir(parents=True, exist_ok=True)
    return FALLBACK_PATH


class OfficeDocumentGenerator:
    """Generates styled Word documents and Excel spreadsheets for JAI Workspace."""

    def __init__(self, output_dir: Optional[Union[str, Path]] = None) -> None:
        self.output_dir = Path(output_dir) if output_dir else get_default_output_dir()
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_word_document(
        self,
        title: str,
        content_sections: List[Dict[str, Any]],
        subtitle: Optional[str] = None,
        author: str = "Jagath (via JAI Assistant)",
        filename: Optional[str] = None,
    ) -> Path:
        """Create a styled Microsoft Word document (.docx).
        
        Args:
            title: Document title.
            content_sections: List of section dicts with keys:
                - 'heading': Section heading text
                - 'paragraphs': List of paragraph strings
                - 'bullets': Optional list of bullet points
                - 'code': Optional code block string
                - 'table': Optional dict with 'headers' and 'rows'
            subtitle: Optional subtitle or executive summary.
            author: Author attribution.
            filename: Optional custom filename. Auto-generated if omitted.
            
        Returns:
            Path to the created .docx file.
        """
        import docx
        from docx.shared import Inches, Pt, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.enum.table import WD_TABLE_ALIGNMENT
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        doc = docx.Document()

        # Page margins
        for section in doc.sections:
            section.top_margin = Inches(1.0)
            section.bottom_margin = Inches(1.0)
            section.left_margin = Inches(1.0)
            section.right_margin = Inches(1.0)

        # Title styling
        title_p = doc.add_paragraph()
        title_run = title_p.add_run(title)
        title_run.font.name = "Arial"
        title_run.font.size = Pt(24)
        title_run.font.bold = True
        title_run.font.color.rgb = RGBColor(26, 54, 93)  # Deep Navy #1A365D
        title_p.paragraph_format.space_after = Pt(4)

        # Subtitle
        if subtitle:
            sub_p = doc.add_paragraph()
            sub_run = sub_p.add_run(subtitle)
            sub_run.font.name = "Arial"
            sub_run.font.size = Pt(13)
            sub_run.font.italic = True
            sub_run.font.color.rgb = RGBColor(74, 85, 104)
            sub_p.paragraph_format.space_after = Pt(8)

        # Metadata Header line
        now_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
        meta_p = doc.add_paragraph()
        meta_run = meta_p.add_run(f"Author: {author}  |  Generated: {now_str}")
        meta_run.font.name = "Arial"
        meta_run.font.size = Pt(9.5)
        meta_run.font.color.rgb = RGBColor(113, 128, 150)
        meta_p.paragraph_format.space_after = Pt(18)

        # Horizontal Divider line
        p_div = doc.add_paragraph()
        p_div_run = p_div.add_run("―" * 55)
        p_div_run.font.color.rgb = RGBColor(203, 213, 225)
        p_div.paragraph_format.space_after = Pt(16)

        # Render Content Sections
        for sec in content_sections:
            heading = sec.get("heading")
            if heading:
                h_p = doc.add_paragraph()
                h_run = h_p.add_run(heading)
                h_run.font.name = "Arial"
                h_run.font.size = Pt(15)
                h_run.font.bold = True
                h_run.font.color.rgb = RGBColor(43, 108, 176)  # Blue #2B6CB0
                h_p.paragraph_format.space_before = Pt(14)
                h_p.paragraph_format.space_after = Pt(6)

            # Paragraphs
            for para_text in sec.get("paragraphs", []):
                p = doc.add_paragraph()
                p_run = p.add_run(para_text)
                p_run.font.name = "Arial"
                p_run.font.size = Pt(11)
                p_run.font.color.rgb = RGBColor(45, 55, 72)
                p.paragraph_format.line_spacing = 1.18
                p.paragraph_format.space_after = Pt(8)

            # Bullet points
            for bullet in sec.get("bullets", []):
                b_p = doc.add_paragraph(style="List Bullet")
                b_run = b_p.add_run(bullet)
                b_run.font.name = "Arial"
                b_run.font.size = Pt(10.5)
                b_run.font.color.rgb = RGBColor(45, 55, 72)
                b_p.paragraph_format.space_after = Pt(3)

            # Code block
            code_text = sec.get("code")
            if code_text:
                c_p = doc.add_paragraph()
                c_run = c_p.add_run(code_text.strip())
                c_run.font.name = "Consolas"
                c_run.font.size = Pt(9.5)
                c_run.font.color.rgb = RGBColor(30, 41, 59)
                c_p.paragraph_format.left_indent = Inches(0.4)
                c_p.paragraph_format.space_before = Pt(6)
                c_p.paragraph_format.space_after = Pt(10)

            # Table
            table_data = sec.get("table")
            if table_data and "headers" in table_data and "rows" in table_data:
                headers = table_data["headers"]
                rows = table_data["rows"]
                table = doc.add_table(rows=len(rows) + 1, cols=len(headers))
                table.alignment = WD_TABLE_ALIGNMENT.CENTER
                table.style = "Table Grid"

                # Header formatting
                hdr_cells = table.rows[0].cells
                for i, h_text in enumerate(headers):
                    hdr_cells[i].text = str(h_text)
                    for r in hdr_cells[i].paragraphs[0].runs:
                        r.font.name = "Arial"
                        r.font.bold = True
                        r.font.size = Pt(10)
                        r.font.color.rgb = RGBColor(255, 255, 255)
                    # Shading XML
                    tcPr = hdr_cells[i]._tc.get_or_add_tcPr()
                    shd = OxmlElement('w:shd')
                    shd.set(qn('w:val'), 'clear')
                    shd.set(qn('w:color'), 'auto')
                    shd.set(qn('w:fill'), '1A365D')  # Deep navy fill
                    tcPr.append(shd)

                # Data rows
                for r_idx, row in enumerate(rows):
                    row_cells = table.rows[r_idx + 1].cells
                    for c_idx, val in enumerate(row):
                        if c_idx < len(row_cells):
                            row_cells[c_idx].text = str(val)
                            for r in row_cells[c_idx].paragraphs[0].runs:
                                r.font.name = "Arial"
                                r.font.size = Pt(9.5)
                                r.font.color.rgb = RGBColor(51, 65, 85)

                doc.add_paragraph().paragraph_format.space_after = Pt(8)

        # Clean sanitized filename
        if not filename:
            clean_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', title.lower()).strip('_')[:40]
            timestamp = datetime.now().strftime("%Y%m%d_%H%M")
            filename = f"{clean_name}_{timestamp}.docx"
        elif not filename.endswith(".docx"):
            filename = f"{filename}.docx"

        dest_file = self.output_dir / filename
        doc.save(str(dest_file))
        logger.info(f"Successfully generated Word document: {dest_file} ({dest_file.stat().st_size} bytes)")
        return dest_file

    def convert_markdown_to_docx(
        self,
        markdown_text: str,
        title: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> Path:
        """Parse markdown text and compile it into a formatted Word (.docx) document."""
        lines = markdown_text.strip().splitlines()
        doc_title = title or "JAI Generated Document"
        subtitle = None
        sections: List[Dict[str, Any]] = []

        cur_section: Dict[str, Any] = {"heading": "", "paragraphs": [], "bullets": []}
        in_code_block = False
        code_lines: List[str] = []

        for line in lines:
            trimmed = line.strip()

            # Handle code blocks
            if trimmed.startswith("```"):
                if in_code_block:
                    in_code_block = False
                    cur_section["code"] = "\n".join(code_lines)
                    code_lines = []
                else:
                    in_code_block = True
                    code_lines = []
                continue

            if in_code_block:
                code_lines.append(line)
                continue

            if not trimmed:
                continue

            # Heading 1 -> Title
            if trimmed.startswith("# ") and doc_title == "JAI Generated Document":
                doc_title = trimmed[2:].strip()
                continue

            # Heading 2 -> Subtitle or new section
            if trimmed.startswith("## "):
                if cur_section["paragraphs"] or cur_section["bullets"] or cur_section.get("code"):
                    sections.append(cur_section)
                cur_section = {"heading": trimmed[3:].strip(), "paragraphs": [], "bullets": []}
                continue

            # Heading 3
            if trimmed.startswith("### "):
                if cur_section["paragraphs"] or cur_section["bullets"] or cur_section.get("code"):
                    sections.append(cur_section)
                cur_section = {"heading": trimmed[4:].strip(), "paragraphs": [], "bullets": []}
                continue

            # Bullets
            if trimmed.startswith(("- ", "* ", "• ")):
                cur_section["bullets"].append(trimmed[2:].strip())
                continue

            # Standard paragraph
            cur_section["paragraphs"].append(trimmed)

        if cur_section["paragraphs"] or cur_section["bullets"] or cur_section.get("code"):
            sections.append(cur_section)

        return self.generate_word_document(
            title=doc_title,
            subtitle=subtitle,
            content_sections=sections,
            filename=filename,
        )

    def generate_excel_spreadsheet(
        self,
        title: str,
        sheets: List[Dict[str, Any]],
        filename: Optional[str] = None,
    ) -> Path:
        """Create a styled Microsoft Excel workbook (.xlsx).
        
        Args:
            title: Title of workbook.
            sheets: List of sheet definitions with keys:
                - 'name': Sheet name (<= 31 chars)
                - 'headers': List of column header strings
                - 'rows': List of row lists
                - 'totals': Optional boolean to add total calculation row
            filename: Optional custom filename.
            
        Returns:
            Path to the created .xlsx file.
        """
        import openpyxl
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()
        # Remove default sheet
        wb.remove(wb.active)

        for sheet_def in sheets:
            sheet_name = sheet_def.get("name", "Sheet1")[:31]
            ws = wb.create_sheet(title=sheet_name)
            ws.views.sheetView[0].showGridLines = True

            headers = sheet_def.get("headers", [])
            rows = sheet_def.get("rows", [])

            # Title banner row
            ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(len(headers), 1))
            title_cell = ws.cell(row=1, column=1, value=f"{title} ― {sheet_name}")
            title_cell.font = Font(name="Arial", size=14, bold=True, color="FFFFFF")
            title_cell.fill = PatternFill(start_color="1A365D", end_color="1A365D", fill_type="solid")
            title_cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
            ws.row_dimensions[1].height = 32

            # Header row
            header_fill = PatternFill(start_color="2B6CB0", end_color="2B6CB0", fill_type="solid")
            header_font = Font(name="Arial", size=10.5, bold=True, color="FFFFFF")
            border_thin = Border(
                left=Side(style='thin', color='E2E8F0'),
                right=Side(style='thin', color='E2E8F0'),
                top=Side(style='thin', color='E2E8F0'),
                bottom=Side(style='thin', color='E2E8F0')
            )

            for col_idx, h_text in enumerate(headers, 1):
                c = ws.cell(row=3, column=col_idx, value=h_text)
                c.font = header_font
                c.fill = header_fill
                c.alignment = Alignment(horizontal="center", vertical="center")
                c.border = border_thin
            ws.row_dimensions[3].height = 24

            # Data rows with zebra striping
            zebra_fill = PatternFill(start_color="F7FAFC", end_color="F7FAFC", fill_type="solid")
            regular_font = Font(name="Arial", size=10, color="2D3748")

            cur_row = 4
            for r_idx, row_data in enumerate(rows):
                is_zebra = (r_idx % 2 == 1)
                for c_idx, val in enumerate(row_data, 1):
                    c = ws.cell(row=cur_row, column=c_idx, value=val)
                    c.font = regular_font
                    c.border = border_thin
                    if is_zebra:
                        c.fill = zebra_fill
                    
                    # Align numbers right, text left
                    if isinstance(val, (int, float)):
                        c.alignment = Alignment(horizontal="right", vertical="center")
                    else:
                        c.alignment = Alignment(horizontal="left", vertical="center")
                ws.row_dimensions[cur_row].height = 20
                cur_row += 1

            # Auto-fit column widths
            for col in ws.columns:
                max_len = 0
                col_letter = get_column_letter(col[0].column)
                for cell in col:
                    if cell.row == 1:
                        continue  # Skip title banner row
                    val_str = str(cell.value or "")
                    if len(val_str) > max_len:
                        max_len = len(val_str)
                ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

        # Clean filename
        if not filename:
            clean_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', title.lower()).strip('_')[:40]
            timestamp = datetime.now().strftime("%Y%m%d_%H%M")
            filename = f"{clean_name}_{timestamp}.xlsx"
        elif not filename.endswith(".xlsx"):
            filename = f"{filename}.xlsx"

        dest_file = self.output_dir / filename
        wb.save(str(dest_file))
        logger.info(f"Successfully generated Excel workbook: {dest_file} ({dest_file.stat().st_size} bytes)")
        return dest_file


# Singleton instance
office_doc_gen = OfficeDocumentGenerator()

