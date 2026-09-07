"""Assemble the captured screenshots into a Word document.

    python docs/capture_ui.py        # first: take the screenshots
    python docs/build_walkthrough.py # then: build the .docx

Reads `docs/screenshots/manifest.json`, which records the caption and the HTTP
status observed for each shot, so the summary table reports what the run
actually returned rather than what anyone expected it to.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs" / "screenshots"
OUT = ROOT / "docs" / "UI-Walkthrough.docx"

CONTENT_WIDTH_IN = 6.5
# Leaves room for the heading and caption that share the page with each image.
MAX_IMAGE_HEIGHT_IN = 7.2

GREY = RGBColor(0x59, 0x5F, 0x66)

TITLES = {
    "01-overview": "The contract, served as the UI",
    "01b-operations": "The operations this API exposes",
    "02-security-schemes": "The two credentials, declared in the contract",
    "03-create-request": "Creating an issue: the request",
    "04-create-response": "Creating an issue: 201 Created",
    "05-validation-400": "The same call, rejected: 400 Bad Request",
    "06-list-issues": "Listing issues, with pagination headers",
    "07-get-issue": "Reading a single issue",
    "08-close-issue": "Closing an issue: this API's DELETE",
    "09-reopen-issue": "Reopening the same issue",
    "10-create-comment": "Commenting on an issue",
    "11-list-comments": "Reading the comment back",
    "12-webhook-valid": "A correctly signed webhook delivery: 204",
    "13-webhook-tampered": "A tampered delivery: 401 Unauthorized",
    "14-events": "The delivery log",
    "15-healthz": "Health, without depending on GitHub",
    "16-schemas": "The reusable schemas",
    "17-redoc": "The same contract in ReDoc",
}

# What each step is meant to demonstrate, for the summary table.
DEMONSTRATES = {
    "04-create-response": "201 + Location header; rate-limit headers forwarded",
    "05-validation-400": "Invalid input rejected as 400 before any GitHub call",
    "06-list-issues": "Link header rewritten to this service; ETag; X-Page",
    "07-get-issue": "Read-after-write through the gateway",
    "08-close-issue": "Close models DELETE (GitHub has no delete-issue)",
    "09-reopen-issue": "The close is reversible; closed_at returns to null",
    "10-create-comment": "201 + Location for a comment",
    "11-list-comments": "The comment is readable back from GitHub",
    "12-webhook-valid": "HMAC verified, delivery stored, fast 204 ack",
    "13-webhook-tampered": "Signature mismatch rejected: 401 invalid_signature",
    "14-events": "Accepted delivery stored; the rejected one absent",
    "15-healthz": "Liveness independent of the upstream",
}


def _run(*args: str) -> str:
    """Read a fact about the build environment; empty string if unavailable."""
    try:
        # noqa S603: every call site passes a fixed argv from this module, never
        # user input, and shell=False.
        return subprocess.run(  # noqa: S603
            args, cwd=ROOT, capture_output=True, text=True, timeout=10, check=False
        ).stdout.strip()
    except Exception:
        return ""


def add_caption(doc: Document, text: str) -> None:
    paragraph = doc.add_paragraph()
    run = paragraph.add_run(text)
    run.italic = True
    run.font.size = Pt(9.5)
    run.font.color.rgb = GREY
    paragraph.paragraph_format.space_after = Pt(10)


def add_image(doc: Document, path: Path, width_px: int, height_px: int) -> None:
    width_in = CONTENT_WIDTH_IN
    height_in = width_in * height_px / width_px
    if height_in > MAX_IMAGE_HEIGHT_IN:
        height_in = MAX_IMAGE_HEIGHT_IN
        width_in = height_in * width_px / height_px
    doc.add_picture(str(path), width=Inches(width_in))
    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER


def add_page_number_footer(doc: Document) -> None:
    """Insert a `PAGE` field, which Word evaluates when it renders."""
    footer = doc.sections[0].footer
    paragraph = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER

    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instr)
    run._r.append(end)
    run.font.size = Pt(9)
    run.font.color.rgb = GREY


def build() -> int:
    manifest_path = SHOTS / "manifest.json"
    if not manifest_path.exists():
        print("No manifest. Run docs/capture_ui.py first.", file=sys.stderr)
        return 1
    shots = json.loads(manifest_path.read_text(encoding="utf-8"))

    missing = [s["file"] for s in shots if not (SHOTS / s["file"]).exists()]
    if missing:
        print(f"Missing screenshots: {missing}", file=sys.stderr)
        return 1

    doc = Document()

    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    for side in ("left_margin", "right_margin"):
        setattr(section, side, Inches(1))
    section.top_margin = Inches(1)
    section.bottom_margin = Inches(1)

    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)

    add_page_number_footer(doc)

    # ---------------------------------------------------------------- title
    title = doc.add_heading("GitHub Issues Gateway", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = subtitle.add_run("UI Walkthrough with Screenshots")
    run.font.size = Pt(16)
    run.font.color.rgb = GREY

    context = doc.add_paragraph()
    context.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = context.add_run("CMPE 272 — Homework 2")
    run.font.size = Pt(12)

    doc.add_paragraph()

    commit = _run("git", "rev-parse", "--short", "HEAD")
    python_version = _run("./.venv/bin/python", "-V") or "Python 3.11+"
    rows = [
        ("Service", "FastAPI gateway over the GitHub Issues REST API"),
        ("Interface shown", "Swagger UI at /docs, rendered from openapi.yaml"),
        ("Source repository", "github.com/mikechau1/CMPE272-HW2"),
        ("Issues operated on", "github.com/mikechau1/cmpe272-issues-gw"),
        ("Commit", commit or "(not a git checkout)"),
        ("Captured", date.today().isoformat()),
        ("Runtime", f"{python_version}, uvicorn, macOS"),
        ("Captured with", "Playwright driving headless Google Chrome"),
        ("Screenshots", f"{len(shots)}, all from live calls against real GitHub"),
    ]
    table = doc.add_table(rows=0, cols=2)
    table.style = "Light List Accent 1"
    for label, value in rows:
        cells = table.add_row().cells
        cells[0].text = label
        cells[1].text = value
        for paragraph in cells[0].paragraphs:
            for run in paragraph.runs:
                run.bold = True

    # ------------------------------------------------------------ what/how
    doc.add_paragraph()
    doc.add_heading("What this document shows", level=1)
    doc.add_paragraph(
        "Every screenshot below is a real interaction with a running instance of "
        "the service, driven through its own web interface. The gateway serves "
        "its hand-written OpenAPI 3.1 contract at /docs, so that page is not a "
        "description of the code — it is the contract itself, and Swagger UI's "
        '"Try it out" issues genuine HTTP requests against the running process, '
        "which in turn calls the real GitHub API."
    )
    doc.add_paragraph(
        "Nothing here is mocked, staged, or edited. Issues really were created, "
        "renamed, closed, reopened and commented on in the test repository while "
        "these screenshots were taken; the webhook deliveries really were signed, "
        "verified and stored. The HTTP status shown in the summary table was read "
        "off the page during capture rather than typed in afterwards."
    )

    note = doc.add_paragraph()
    run = note.add_run(
        "Webhook secret: the signatures visible in these screenshots were "
        "produced with a throwaway secret used only for this document, so that "
        "no material derived from the real WEBHOOK_SECRET is committed alongside "
        "the images."
    )
    run.italic = True
    run.font.size = Pt(9.5)
    run.font.color.rgb = GREY

    doc.add_heading("How to reproduce it", level=1)
    steps = doc.add_paragraph()
    steps.add_run("1. ").bold = True
    steps.add_run("./scripts/run.sh").font.name = "Consolas"
    steps.add_run("  — start the service on port 8000.")
    steps = doc.add_paragraph()
    steps.add_run("2. ").bold = True
    steps.add_run("pip install -r docs/requirements.txt").font.name = "Consolas"
    steps.add_run("  — Playwright and python-docx.")
    steps = doc.add_paragraph()
    steps.add_run("3. ").bold = True
    steps.add_run("python docs/capture_ui.py").font.name = "Consolas"
    steps.add_run("  — drive the UI, write the screenshots.")
    steps = doc.add_paragraph()
    steps.add_run("4. ").bold = True
    steps.add_run("python docs/build_walkthrough.py").font.name = "Consolas"
    steps.add_run("  — rebuild this document.")

    # ----------------------------------------------------------- summary
    doc.add_heading("Summary of verified interactions", level=1)
    doc.add_paragraph(
        "Each row is one screenshot in this document. The status column is what "
        "the service actually returned during the capture run."
    )
    summary = doc.add_table(rows=1, cols=3)
    summary.style = "Light Grid Accent 1"
    header = summary.rows[0].cells
    for index, label in enumerate(("Interaction", "Status", "What it demonstrates")):
        header[index].text = label
        for paragraph in header[index].paragraphs:
            for run in paragraph.runs:
                run.bold = True

    for shot in shots:
        if not shot.get("status"):
            continue
        cells = summary.add_row().cells
        cells[0].text = TITLES.get(shot["name"], shot["name"])
        cells[1].text = shot["status"]
        cells[2].text = DEMONSTRATES.get(shot["name"], "")
        for paragraph in cells[1].paragraphs:
            for run in paragraph.runs:
                run.bold = True
        for cell in cells:
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(9.5)

    doc.add_page_break()

    # ------------------------------------------------------------- figures
    doc.add_heading("Walkthrough", level=1)

    for index, shot in enumerate(shots, start=1):
        heading = f"{index}. {TITLES.get(shot['name'], shot['name'])}"
        if shot.get("status"):
            heading += f"  —  HTTP {shot['status']}"
        doc.add_heading(heading, level=2)
        add_caption(doc, shot["caption"])
        add_image(doc, SHOTS / shot["file"], shot["width"], shot["height"])
        if index < len(shots):
            doc.add_page_break()

    doc.save(OUT)
    size_kb = OUT.stat().st_size / 1024
    print(f"Wrote {OUT.relative_to(ROOT)}  ({size_kb:.0f} KB, {len(shots)} figures)")
    return 0


if __name__ == "__main__":
    sys.exit(build())
