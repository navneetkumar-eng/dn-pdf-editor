"""
DN PDF Editor Pro - PDF Generator
Overlays edited values on the original PDF without re-creating the layout.

Key facts from PDF analysis:
- All values: font NotoSansDevanagari-Regular, size 9.22
- Total values (Number of Items, Sub Total, GST Total, Grand Total): Bold
- Values are RIGHT-ALIGNED within their columns
- Column right edges (from table borders): qty=255, rate=305, sub=355, gst=392, net=442
- Totals right edge: 554
"""

from __future__ import annotations

import fitz
from pathlib import Path
from typing import Tuple, Any

from config import OUTPUT_DIR
from extractor import DNDocument, SpanInfo
from utils import setup_logger

log = setup_logger(__name__)

# ── Font constants matching original PDF ───────────────────────────────────────
FONT_SIZE    = 9.22   # exact size from original PDF
FONT_REGULAR = "helv"
FONT_BOLD    = "hebo"  # Helvetica Bold

# Column RIGHT edges (from vertical line positions in PDF)
COL_RIGHT = {
    "qty":        253.0,
    "rate":       304.0,
    "sub_total":  354.0,
    "gst_pct":    391.0,
    "net_amount": 441.0,
    "totals":     554.5,   # right edge for all total values
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _redact(page: fitz.Page, bbox: Tuple, padding: float = 2.0) -> None:
    """Permanently erase text in bbox using redaction annotation."""
    x0, y0, x1, y1 = bbox
    rect = fitz.Rect(x0 - padding, y0 - padding, x1 + padding, y1 + padding)
    page.add_redact_annot(rect, fill=(1, 1, 1))


def _insert_right_aligned(
    page:      fitz.Page,
    text:      str,
    right_x:   float,
    baseline_y: float,
    font_size: float = FONT_SIZE,
    bold:      bool  = False,
) -> None:
    """Insert text RIGHT-ALIGNED so its right edge is at right_x."""
    fontname = FONT_BOLD if bold else FONT_REGULAR
    font     = fitz.Font(fontname)
    tw       = font.text_length(text, fontsize=font_size)
    x        = right_x - tw
    page.insert_text(
        fitz.Point(x, baseline_y),
        text,
        fontname=fontname,
        fontsize=font_size,
        color=(0, 0, 0),
    )


def _find_amount_bbox(
    spans:    list[SpanInfo],
    target_y: float,
    min_x:    float = 500.0,
    page:     int   = 0,
) -> Tuple | None:
    """Find the rightmost span near target_y with x > min_x."""
    candidates = [
        s for s in spans
        if s.page == page
        and s.bbox[0] > min_x
        and abs((s.bbox[1] + s.bbox[3]) / 2 - target_y) < 10
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda s: s.bbox[0])
    return candidates[-1].bbox


# ── Public API ─────────────────────────────────────────────────────────────────

def generate_pdf(
    original_pdf_path: str | Path,
    dn:                DNDocument,
    edited_rows:       list[dict],
    totals:            Any,
    output_filename:   str | None = None,
) -> Path:
    """
    Generate updated PDF by redacting old values and inserting new ones
    with exact font, size, bold, and right-alignment matching the original.
    """
    original_pdf_path = Path(original_pdf_path)
    doc   = fitz.open(str(original_pdf_path))
    page0 = doc[0]

    # ── Step 1: Queue all redactions ──────────────────────────────────────────
    inserts = []   # (text, right_x, baseline_y, bold)

    # Line item cells
    for item, edited in zip(dn.line_items, edited_rows):
        coords = item.coords

        cell_updates = {
            "qty":        (_fmt_qty(float(edited.get("qty",       item.qty))),       COL_RIGHT["qty"]),
            "rate":       (_fmt_num(float(edited.get("rate",      item.rate))),      COL_RIGHT["rate"]),
            "sub_total":  (_fmt_num(float(edited.get("sub_total", item.sub_total))), COL_RIGHT["sub_total"]),
            "gst_pct":    (_fmt_num(float(edited.get("gst_pct",   item.gst_pct))),   COL_RIGHT["gst_pct"]),
            "net_amount": (_fmt_num(float(edited.get("net_amount",item.net_amount))),COL_RIGHT["net_amount"]),
        }

        for field_key, (new_text, right_x) in cell_updates.items():
            bbox = coords.get(field_key)
            if not bbox:
                continue
            _redact(page0, bbox)
            baseline_y = bbox[3] - 1.5
            inserts.append((new_text, right_x, baseline_y, False))
            log.debug("Queued: %s → %s right_x=%.1f", field_key, new_text, right_x)

    # Totals (bold, right-aligned to 554.5)
    total_items = [
        (628.0, str(totals.num_items),          True),
        (650.0, _fmt_num(totals.sub_total),     True),
        (671.0, _fmt_num(totals.gst_total),     True),
        (692.0, _fmt_num(totals.total_payable), True),
    ]

    for target_y, new_text, bold in total_items:
        bbox = _find_amount_bbox(dn.spans, target_y)
        if bbox:
            _redact(page0, bbox)
            baseline_y = bbox[3] - 1.5
            inserts.append((new_text, COL_RIGHT["totals"], baseline_y, bold))

    # IGST breakdown lines
    _queue_igst(page0, dn.spans, edited_rows, inserts)

    # ── Step 2: Apply all redactions ─────────────────────────────────────────
    page0.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

    # ── Step 3: Insert all new values ────────────────────────────────────────
    for (text, right_x, baseline_y, bold) in inserts:
        _insert_right_aligned(page0, text, right_x, baseline_y, bold=bold)

    # ── Save ──────────────────────────────────────────────────────────────────
    if not output_filename:
        output_filename = f"{original_pdf_path.stem}_edited.pdf"

    out_path = OUTPUT_DIR / output_filename
    doc.save(str(out_path), garbage=4, deflate=True)
    doc.close()
    log.info("PDF saved → %s", out_path)
    return out_path


# ── IGST line rebuilder ────────────────────────────────────────────────────────

def _queue_igst(
    page:        fitz.Page,
    spans:       list[SpanInfo],
    edited_rows: list[dict],
    inserts:     list,
) -> None:
    """Redact and rewrite the IGST and Total IGST lines."""
    from calculation import build_igst_breakdown

    p0 = [s for s in spans if s.page == 0]

    # IGST breakdown line (~y=708)
    igst_line = [s for s in p0 if s.text.strip() == "IGST" and s.bbox[1] > 700]
    if not igst_line:
        return

    row_y     = (igst_line[0].bbox[1] + igst_line[0].bbox[3]) / 2
    line_spans = [s for s in p0 if abs((s.bbox[1] + s.bbox[3]) / 2 - row_y) < 8]

    if line_spans:
        x0 = min(s.bbox[0] for s in line_spans)
        y0 = min(s.bbox[1] for s in line_spans)
        x1 = max(s.bbox[2] for s in line_spans)
        y1 = max(s.bbox[3] for s in line_spans)
        _redact(page, (x0, y0, x1, y1))
        baseline_y = y1 - 1.5

        new_text = build_igst_breakdown(edited_rows)
        lines    = new_text.split("\n")
        inserts.append((lines[0] if lines else "", x0 + 200, baseline_y, False))

    # Total IGST line (~y=720)
    ti_spans = [s for s in p0 if "Total" in s.text and "IGST" in s.text and s.bbox[1] > 715]
    if not ti_spans:
        ti_spans = [s for s in p0 if abs((s.bbox[1] + s.bbox[3]) / 2 - 726) < 8]

    if ti_spans:
        ti_x0 = min(s.bbox[0] for s in ti_spans)
        ti_y0 = min(s.bbox[1] for s in ti_spans)
        ti_x1 = max(s.bbox[2] for s in ti_spans)
        ti_y1 = max(s.bbox[3] for s in ti_spans)
        _redact(page, (ti_x0, ti_y0, ti_x1, ti_y1))

        new_text = build_igst_breakdown(edited_rows)
        lines    = new_text.split("\n")
        if len(lines) > 1:
            inserts.append((lines[1], ti_x0 + 150, ti_y1 - 1.5, True))


# ── Format helpers ─────────────────────────────────────────────────────────────

def _fmt_num(v: float) -> str:
    return f"{v:.2f}"

def _fmt_qty(v: float) -> str:
    return str(int(v)) if v == int(v) else f"{v:.2f}"
