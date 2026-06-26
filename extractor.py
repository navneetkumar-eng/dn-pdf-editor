"""
DN PDF Editor Pro - Extractor
Detects PDF type, extracts text/tables, maps editable field coordinates.
"""

from __future__ import annotations

import re
import fitz          # PyMuPDF
import pdfplumber
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any, Tuple

from config import OCR_DPI
from utils import setup_logger, parse_float, clean_text

log = setup_logger(__name__)


# ── Data models ────────────────────────────────────────────────────────────────

@dataclass
class SpanInfo:
    """One text span extracted from a PDF page."""
    text:  str
    bbox:  Tuple[float, float, float, float]   # (x0,y0,x1,y1) in pts
    font:  str
    size:  float
    page:  int


@dataclass
class LineItem:
    """One product row in the DN table."""
    sr_no:      int
    upc:        str
    name:       str
    hsn:        str
    qty:        float
    rate:       float
    sub_total:  float
    gst_pct:    float
    net_amount: float
    reason:     str
    remark:     str
    # coordinate bboxes for editable cells  {field: bbox}
    coords:     Dict[str, Tuple] = field(default_factory=dict)


@dataclass
class DNDocument:
    """Full parsed Debit / Discrepancy Note."""
    # Header / meta
    title:          str = ""
    buyer_id:       str = ""
    purchase_no:    str = ""
    date:           str = ""
    buyer_gst:      str = ""
    cin_no:         str = ""
    pan_no:         str = ""
    supplier_gst:   str = ""
    original_inv_id: str = ""
    inv_to_name:    str = ""
    inv_to_address: str = ""
    buyer_name:     str = ""
    buyer_address:  str = ""

    # Transporter
    lr_gr_no:       str = ""
    carrier_name:   str = ""
    vehicle_no:     str = ""
    total_weight:   str = ""

    # Line items
    line_items:     List[LineItem] = field(default_factory=list)

    # Totals
    num_items:      int   = 0
    sub_total:      float = 0.0
    gst_total:      float = 0.0
    total_payable:  float = 0.0
    gst_type:       str   = "IGST"   # "IGST" or "CGST+SGST"
    igst_breakdown: str   = ""
    cess_text:      str   = ""

    # Footer
    footer_text:    str   = ""

    # Raw spans for coordinate lookup
    spans:          List[SpanInfo] = field(default_factory=list)
    is_scanned:     bool  = False


# ── Main extractor ─────────────────────────────────────────────────────────────

class DNExtractor:
    """Extract all information from a Debit Note PDF."""

    def __init__(self, pdf_path: str | Path):
        self.pdf_path = Path(pdf_path)
        self._doc: fitz.Document | None = None

    # ── Public ────────────────────────────────────────────────────────────────

    def extract(self) -> DNDocument:
        """Full extraction pipeline. Returns a populated DNDocument."""
        try:
            self._doc = fitz.open(str(self.pdf_path))
        except Exception as e:
            log.error("Cannot open PDF: %s", e)
            raise

        dn = DNDocument()

        # 1. Detect scan vs searchable
        dn.is_scanned = self._is_scanned()
        log.info("PDF type: %s", "SCANNED" if dn.is_scanned else "SEARCHABLE")

        # 2. Collect all spans with coordinates
        dn.spans = self._collect_spans()

        # 3. Parse header / meta fields
        self._parse_header(dn)

        # 4. Parse line items with coordinates
        self._parse_line_items(dn)

        # 5. Parse totals
        self._parse_totals(dn)

        # 6. Parse footer (page 2)
        self._parse_footer(dn)

        return dn

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _is_scanned(self) -> bool:
        """Return True if the PDF has no searchable text layer."""
        for page in self._doc:
            text = page.get_text("text").strip()
            if text:
                return False
        return True

    def _collect_spans(self) -> List[SpanInfo]:
        """Walk every page and collect every text span with position data."""
        spans: List[SpanInfo] = []
        for page_num, page in enumerate(self._doc):
            blocks = page.get_text("dict")["blocks"]
            for b in blocks:
                if b.get("type") != 0:
                    continue
                for line in b["lines"]:
                    for sp in line["spans"]:
                        t = clean_text(sp["text"])
                        if t:
                            spans.append(SpanInfo(
                                text=t,
                                bbox=tuple(sp["bbox"]),
                                font=sp["font"],
                                size=sp["size"],
                                page=page_num,
                            ))
        return spans

    # ── Header parsing ─────────────────────────────────────────────────────────

    def _span_value_after(self, label: str, page: int = 0) -> str:
        """Return the concatenated text of spans that appear to the right of a label on the same row."""
        label_spans = [s for s in self.spans_on_page(page) if label.lower() in s.text.lower()]
        if not label_spans:
            return ""
        ls = label_spans[0]
        row_y = (ls.bbox[1] + ls.bbox[3]) / 2
        right_spans = [
            s for s in self.spans_on_page(page)
            if s.bbox[0] > ls.bbox[2] - 5
            and abs((s.bbox[1] + s.bbox[3]) / 2 - row_y) < 8
        ]
        right_spans.sort(key=lambda s: s.bbox[0])
        return clean_text(" ".join(s.text for s in right_spans).lstrip(": "))

    def spans_on_page(self, page: int) -> List[SpanInfo]:
        return [s for s in self._doc_dn_spans if s.page == page]

    def _parse_header(self, dn: DNDocument) -> None:
        """Populate DNDocument header fields from spans."""
        self._doc_dn_spans = dn.spans  # make accessible to helpers

        p0 = [s for s in dn.spans if s.page == 0]

        # Title
        title_parts = [s.text for s in p0 if "discrepancy" in s.text.lower() or "debit" in s.text.lower()]
        dn.title = " ".join(title_parts) or "Discrepancy Note"

        # Buyer section: right column (x > 300)
        right = [s for s in p0 if s.bbox[0] > 300]

        dn.buyer_id      = self._right_val(right, "Id")
        dn.purchase_no   = self._right_val(right, "Purchase")
        dn.date          = self._right_val(right, "Date")
        dn.buyer_gst     = self._right_val(right, "Gst Tin")
        dn.cin_no        = self._right_val(right, "Cin")
        dn.pan_no        = self._right_val(right, "Pan")
        dn.supplier_gst  = self._right_val(right, "24")    # supplier GST starts with 24

        # For supplier GST, find the span containing supplier detail
        supp = [s for s in right if "supplier" in s.text.lower()]
        if supp:
            sy = (supp[0].bbox[1] + supp[0].bbox[3]) / 2
            # Gst Tin No line below supplier heading
            gst_lines = [s for s in right if s.bbox[1] > sy + 5 and "24" in s.text]
            if gst_lines:
                below = sorted(gst_lines, key=lambda s: s.bbox[1])
                row_y = (below[0].bbox[1] + below[0].bbox[3]) / 2
                row_spans = [s for s in right if abs((s.bbox[1]+s.bbox[3])/2 - row_y) < 6]
                row_spans.sort(key=lambda s: s.bbox[0])
                dn.supplier_gst = clean_text(" ".join(s.text for s in row_spans))

        dn.original_inv_id = self._right_val(right, "Original")

        # Left column: buyer of goods (Invoice To)
        left = [s for s in p0 if s.bbox[0] < 300]
        dn.inv_to_name    = self._left_label_val(left, "Name")
        dn.inv_to_address = self._left_label_val(left, "Address")

    def _right_val(self, spans: List[SpanInfo], key: str) -> str:
        """Find label span by keyword, return text of same-row spans to its right."""
        matches = [s for s in spans if key.lower() in s.text.lower()]
        if not matches:
            return ""
        ls = matches[0]
        row_y = (ls.bbox[1] + ls.bbox[3]) / 2
        right_of = [s for s in spans if s.bbox[0] > ls.bbox[2] - 5 and abs((s.bbox[1]+s.bbox[3])/2 - row_y) < 6]
        right_of.sort(key=lambda s: s.bbox[0])
        val = clean_text(" ".join(s.text for s in right_of).lstrip(": "))
        return val

    def _left_label_val(self, spans: List[SpanInfo], key: str) -> str:
        """Find label by key in left column and collect value spans."""
        matches = [s for s in spans if key.lower() in s.text.lower() and s.bbox[0] < 100]
        if not matches:
            return ""
        ls = matches[0]
        row_y = (ls.bbox[1] + ls.bbox[3]) / 2
        right_of = [s for s in spans if s.bbox[0] > 90 and abs((s.bbox[1]+s.bbox[3])/2 - row_y) < 6]
        right_of.sort(key=lambda s: s.bbox[0])
        return clean_text(" ".join(s.text for s in right_of).lstrip(": "))

    # ── Line item parsing ──────────────────────────────────────────────────────

    def _parse_line_items(self, dn: DNDocument) -> None:
        """
        Detect table rows by finding Sr.no column spans, then
        collect all cells horizontally and record their bboxes.
        """
        p0_spans = [s for s in dn.spans if s.page == 0]

        # Identify row anchors: sr_no column is x < 60, numeric text
        sr_spans = [
            s for s in p0_spans
            if s.bbox[0] < 55 and re.match(r"^\d+$", s.text.strip())
            and s.bbox[1] > 400  # below headers
        ]

        if not sr_spans:
            log.warning("No line items found by sr_no detection.")
            return

        for sr_span in sr_spans:
            row_y_center = (sr_span.bbox[1] + sr_span.bbox[3]) / 2
            # Collect all spans whose vertical center falls within ±80 pts (multi-line cells)
            row_band_top    = sr_span.bbox[1] - 5
            row_band_bottom = sr_span.bbox[3] + 5

            # Extend to capture multi-line product names
            # Find next sr_span to define band bottom
            next_srs = [s for s in sr_spans if s.bbox[1] > sr_span.bbox[3] + 5]
            if next_srs:
                next_sr = min(next_srs, key=lambda s: s.bbox[1])
                row_band_bottom = next_sr.bbox[1] - 5
            else:
                # Last row – extend to total line
                row_band_bottom = 640  # approximate bottom of table

            row_spans = [
                s for s in p0_spans
                if s.bbox[1] >= row_band_top and s.bbox[3] <= row_band_bottom + 5
            ]

            item = self._build_line_item(int(sr_span.text.strip()), row_spans)
            if item:
                dn.line_items.append(item)

    def _build_line_item(self, sr: int, row_spans: List[SpanInfo]) -> LineItem | None:
        """Assemble a LineItem from the spans belonging to one row."""

        def spans_in_x(x0, x1):
            return sorted(
                [s for s in row_spans if s.bbox[0] >= x0 - 5 and s.bbox[2] <= x1 + 10],
                key=lambda s: (s.bbox[1], s.bbox[0])
            )

        def text_in_x(x0, x1):
            return clean_text(" ".join(s.text for s in spans_in_x(x0, x1)))

        def bbox_in_x(x0: float, x1: float) -> tuple | None:
            ss = spans_in_x(x0, x1)
            if not ss:
                return None
            # Use only the FIRST (topmost) numeric span for financial fields
            # to avoid bbox expanding into adjacent rows
            numeric_ss = [s for s in ss if any(c.isdigit() for c in s.text)]
            if numeric_ss:
                s = numeric_ss[0]
                return (s.bbox[0], s.bbox[1], s.bbox[2], s.bbox[3])
            s = ss[0]
            return (s.bbox[0], s.bbox[1], s.bbox[2], s.bbox[3])

        # Column x-ranges (empirically verified against this PDF template)
        # Sr.no: 39-60  UPC: 60-138  Name: 138-222  Qty: 222-260
        # Rate: 258-310  SubTotal: 307-357  GST%: 357-395
        # NetAmt: 393-445  Reason: 445-503  Remark: 503-560

        upc  = text_in_x(60, 138)
        name = text_in_x(138, 222)
        qty_text  = text_in_x(220, 258)
        rate_text = text_in_x(258, 308)
        sub_text  = text_in_x(307, 357)
        gst_text  = text_in_x(357, 395)
        net_text  = text_in_x(393, 445)
        reason    = text_in_x(445, 503)
        remark    = text_in_x(503, 560)

        # Filter: only keep numeric values for financial fields by re-checking
        # that qty/rate spans actually sit above the "Number of Items" line (y<622)
        def numeric_text_in_x(x0: float, x1: float, max_y: float = 620.0) -> str:
            ss = [s for s in row_spans
                  if s.bbox[0] >= x0 - 5 and s.bbox[2] <= x1 + 10 and s.bbox[1] < max_y]
            ss.sort(key=lambda s: (s.bbox[1], s.bbox[0]))
            return clean_text(" ".join(s.text for s in ss))

        qty_text  = numeric_text_in_x(220, 258)
        rate_text = numeric_text_in_x(258, 308)

        # Extract HSN from name
        hsn_match = re.search(r'HSN[:\s]+(\d+)', name, re.I)
        hsn = hsn_match.group(1) if hsn_match else ""

        coords = {
            "qty":        bbox_in_x(222, 257),
            "rate":       bbox_in_x(257, 307),
            "sub_total":  bbox_in_x(307, 357),
            "gst_pct":    bbox_in_x(357, 393),
            "net_amount": bbox_in_x(393, 445),
            "reason":     bbox_in_x(445, 503),
            "remark":     bbox_in_x(503, 560),
        }

        return LineItem(
            sr_no=sr,
            upc=upc,
            name=name,
            hsn=hsn,
            qty=parse_float(qty_text),
            rate=parse_float(rate_text),
            sub_total=parse_float(sub_text),
            gst_pct=parse_float(gst_text),
            net_amount=parse_float(net_text),
            reason=reason,
            remark=remark,
            coords=coords,
        )

    # ── Totals parsing ─────────────────────────────────────────────────────────

    def _parse_totals(self, dn: DNDocument) -> None:
        """Extract footer totals from page 0 spans."""
        p0 = [s for s in dn.spans if s.page == 0]

        def amount_at_y(target_y: float, tolerance: float = 10) -> float:
            """Find the rightmost span (x>500) near a given y coordinate."""
            right = [
                s for s in p0
                if s.bbox[0] > 500 and abs((s.bbox[1] + s.bbox[3]) / 2 - target_y) < tolerance
            ]
            right.sort(key=lambda s: s.bbox[0])
            return parse_float(right[-1].text) if right else 0.0

        # Known y-positions from PDF analysis:
        # Number of Items ≈ y=622, Sub Total ≈ y=643, GST Total ≈ y=665, Total Payable ≈ y=686
        dn.num_items     = int(amount_at_y(628))
        dn.sub_total     = amount_at_y(650)
        dn.gst_total     = amount_at_y(671)
        dn.total_payable = amount_at_y(692)

        # GST type — IGST if there's a breakdown line near y=708
        igst_label = [s for s in p0 if s.text.strip() == "IGST" and s.bbox[1] > 700]
        igst_eq    = [s for s in p0 if "=" in s.text and s.bbox[1] > 700 and s.bbox[0] > 150]
        dn.gst_type = "IGST" if (igst_label and igst_eq) else "CGST+SGST"
        dn.igst_breakdown = clean_text(" ".join(
            s.text for s in sorted(
                [s for s in p0 if s.bbox[1] > 700 and s.bbox[1] < 740],
                key=lambda s: (s.bbox[1], s.bbox[0])
            )
        ))

        cess_spans = [s for s in p0 if "CESS" in s.text]
        dn.cess_text = clean_text(" ".join(s.text for s in cess_spans))

    # ── Footer (page 2) ────────────────────────────────────────────────────────

    def _parse_footer(self, dn: DNDocument) -> None:
        if len(self._doc) < 2:
            return
        p1_spans = [s for s in dn.spans if s.page == 1]
        dn.footer_text = clean_text(" ".join(s.text for s in p1_spans))
