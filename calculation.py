"""
DN PDF Editor Pro - Calculation Engine
Real-time recalculation of DN financial values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from config import GST_IGST, GST_CGST_SGST
from utils import setup_logger

log = setup_logger(__name__)


@dataclass
class RowCalc:
    """Calculated values for a single line item."""
    sub_total:  float
    igst:       float
    cgst:       float
    sgst:       float
    net_amount: float


@dataclass
class TotalsCalc:
    """Calculated document-level totals."""
    sub_total:      float
    gst_total:      float
    total_payable:  float
    igst_total:     float
    cgst_total:     float
    sgst_total:     float
    num_items:      int


def calc_row(
    qty:           float,
    rate:          float,
    gst_pct:       float,
    other_charges: float = 0.0,
    gst_type:      str   = GST_IGST,
) -> RowCalc:
    """
    Calculate derived values for a single line item.

    Rules:
        Sub Total  = Qty × Rate
        IGST       = Sub Total × (gst_pct / 100)          [when IGST]
        CGST       = Sub Total × (gst_pct / 2 / 100)      [when CGST+SGST]
        SGST       = same as CGST
        Net Amount = Sub Total + GST + Other Charges
    """
    sub_total = round(qty * rate, 2)

    if gst_type == GST_IGST:
        igst = round(sub_total * gst_pct / 100, 2)
        cgst = sgst = 0.0
    else:
        igst = 0.0
        cgst = round(sub_total * gst_pct / 2 / 100, 2)
        sgst = cgst

    gst_amount = igst + cgst + sgst
    net_amount = round(sub_total + gst_amount + other_charges, 2)

    return RowCalc(
        sub_total=sub_total,
        igst=igst,
        cgst=cgst,
        sgst=sgst,
        net_amount=net_amount,
    )


def calc_totals(
    rows:          List[dict],    # list of {"qty","rate","gst_pct","other_charges"}
    gst_type:      str = GST_IGST,
    extra_charges: float = 0.0,
) -> TotalsCalc:
    """
    Aggregate totals across all line items.

    Args:
        rows:          List of row dicts with qty, rate, gst_pct, other_charges.
        gst_type:      "IGST" or "CGST+SGST".
        extra_charges: Document-level additional charges.
    """
    sub_total_sum = 0.0
    igst_sum      = 0.0
    cgst_sum      = 0.0
    sgst_sum      = 0.0
    num_items     = 0

    for r in rows:
        rc = calc_row(
            qty=float(r.get("qty", 0)),
            rate=float(r.get("rate", 0)),
            gst_pct=float(r.get("gst_pct", 0)),
            other_charges=float(r.get("other_charges", 0)),
            gst_type=gst_type,
        )
        sub_total_sum += rc.sub_total
        igst_sum      += rc.igst
        cgst_sum      += rc.cgst
        sgst_sum      += rc.sgst
        num_items     += int(r.get("qty", 0))

    gst_total     = round(igst_sum + cgst_sum + sgst_sum, 2)
    total_payable = round(sub_total_sum + gst_total + extra_charges, 2)

    return TotalsCalc(
        sub_total=round(sub_total_sum, 2),
        gst_total=gst_total,
        total_payable=total_payable,
        igst_total=round(igst_sum, 2),
        cgst_total=round(cgst_sum, 2),
        sgst_total=round(sgst_sum, 2),
        num_items=num_items,
    )


def build_igst_breakdown(rows: List[dict]) -> str:
    """Rebuild IGST breakdown string matching the original PDF format."""
    parts = []
    igst_total = 0.0
    for r in rows:
        sub = round(float(r.get("qty", 0)) * float(r.get("rate", 0)), 2)
        gst = float(r.get("gst_pct", 0))
        amt = round(sub * gst / 100, 2)
        igst_total += amt
        parts.append(f"{sub} * {gst}% = {amt:.2f}")
    return ", ".join(parts) + f",\nTotal IGST = {igst_total:.2f}"
