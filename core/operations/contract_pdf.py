"""Generate a professional signable contract PDF for contributor onboarding.

Renders with reportlab from caller-supplied facts. Callers decide where to
write; this module never touches the network or writes outside the given path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def render_contract_pdf(
    *,
    out_path: str | Path,
    recipient_name: str,
    recipient_email: str,
    role_title: str,
    headline: str,
    scope_lines: list[str],
    effective_date: str = "",
) -> Path:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleX",
        parent=styles["Heading1"],
        fontSize=16,
        spaceAfter=8,
        alignment=1,
    )
    section = ParagraphStyle(
        "SectionHead", parent=styles["Heading2"], fontSize=11,
        spaceBefore=10, spaceAfter=4, fontName="Helvetica-Bold",
    )
    body = ParagraphStyle(
        "Body", parent=styles["BodyText"], fontSize=10, leading=13, spaceAfter=6,
    )
    foot = ParagraphStyle(
        "Foot", parent=styles["BodyText"], fontSize=8.5, leading=11,
        spaceAfter=3, textColor="444444",
    )

    effective = effective_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    doc = SimpleDocTemplate(
        str(out), rightMargin=50, leftMargin=50, topMargin=56, bottomMargin=48,
        pagesize=LETTER,
    )

    story = [
        Paragraph("IdentityOS Corporation", title_style),
        Paragraph(
            "Contributorship & Learning Engagement Letter",
            ParagraphStyle("sub", parent=styles["Heading2"], alignment=1, spaceAfter=8),
        ),
        Paragraph("____________________", foot),
        Spacer(1, 0.08 * inch),
        Paragraph(f"Dear {recipient_name},", body),
        Paragraph(
            f"This letter formalizes a learning-and-collaboration engagement "
            f"between <b>IdentityOS Corporation</b> (the Organization, led by "
            f"founder and CEO Arsène Manzi) and <b>{recipient_name}</b> "
            f"({recipient_email}), who will serve as <b>{role_title}</b>, "
            f"reporting to the Distiller identity for personnel oversight and "
            f"reviews. Arsène Manzi authorized this approach personally on "
            f"{today}.",
            body,
        ),
        Paragraph(headline, section),
    ]
    for item in scope_lines:
        story.append(Paragraph(f"• {item}", body))
    story.append(Spacer(1, 0.06 * inch))
    story.extend([
        Paragraph("Ownership & disclosure", section),
        Paragraph(
            "Background work is work-for-hire; the results belong to "
            "IdentityOS Corporation. You retain the right to reference your "
            "public contributions (commits, issues, messages) on your resume "
            "and portfolio. Month-to-month expectations are listed below.",
            body,
        ),
        Paragraph(
            "You may keep copies of your own work for your records; you may not "
            "resell or redistribute the Organization's confidential information, "
            "keys, or assets.",
            body,
        ),
        Paragraph("Terms of engagement", section),
        Paragraph(
            "This is a learning/practice role that builds experience and "
            "portfolio evidence. It is not an employment contract and does not "
            "create an employment relationship with either party. Either side "
            "may modify scope with notice. Outreach began as part of our "
            "IdentityOS outreach effort; the first verified contact is logged "
            "and auditable.",
            body,
        ),
        Paragraph(
            f"<b>Effective date:</b> {effective}",
            foot,
        ),
        Spacer(1, 0.25 * inch),
        Paragraph("Signatures", section),
        Paragraph(
            "_______________________     _______________________<br/>"
            f"Arsène Manzi                                    {recipient_name}<br/>"
            "Founder & CEO, IdentityOS                Contributor",
            body,
        ),
    ])
    doc.build(story)
    return out
