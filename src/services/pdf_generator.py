"""ReportLab ATS-Compliant PDF Resume Generator.

Produces clean, single-page, vector PDF resumes using standard Type-1 Helvetica fonts
modeled directly on the candidate's master Word/ATS resume layout:
- Standard 0.45-inch margins (32 pt)
- Centered header with professional subtitle and interactive clickable hyperlinks
- Bold uppercase section headers with full-width horizontal dividing rules
- Two-column tables for Education and Project headers (Title + Link Left, Dates Right)
- Categorized Technical Skills layout
- Interactive clickable GitHub links for every project
- Dynamic depth calibration to fit all sections on exactly 1 page with minimal wasted space
- 100% text-searchable, vector ATS parseable by Workday, Greenhouse, Taleo, Ashby, Lever
"""

import io
from pathlib import Path
from typing import List, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.platypus.flowables import HRFlowable

from src.models.candidate import CandidateProfile

_FRAME_PADDING = 6.0  # ReportLab Frame default inner padding (all sides)
_BOTTOM_BREATHING_ROOM_IN = 0.45  # guaranteed breathing room left under filled content
_MAX_FILLER_CANDIDATES = 3  # locked call: only 3 add-on (is_filler) projects, extras purged
_MAX_WRAP_HEIGHT = 100000.0  # effectively unbounded wrapping budget for estimation

# Diagnostics of the most recent generate_resume_pdf run (fit decisions and estimates)
_LAST_FIT_STATS = {
    "page_budget": 0.0,
    "usable_h": 0.0,
    "est_prefix": 0.0,
    "tail_h": 0.0,
    "est_primary_total": 0.0,
    "fillers_included": 0,
    "fill_left": 0.0,
    "estimated_total": 0.0,
}


def usable_frame_geometry(builder: "PDFResumeBuilder") -> Tuple[float, float]:
    """Content width/height inside the SimpleDocTemplate Frame, accounting for padding."""
    width = letter[0] - builder.left_margin - builder.right_margin - 2 * _FRAME_PADDING
    height = letter[1] - builder.top_margin - builder.bottom_margin - 2 * _FRAME_PADDING
    return width, height


def estimate_flowable_height(flowable, usable_width: float, prev_space_after: float = 0.0) -> float:
    """Merged ReportLab wrap height for one flowable given the previous flowable's spaceAfter.

    Mirrors ReportLab Frame._add semantics: the vertical gap before a flowable is
    max(its own spaceBefore, the previous flowable's spaceAfter).
    """
    _w, h = flowable.wrap(usable_width, _MAX_WRAP_HEIGHT)
    gap = max(flowable.getSpaceBefore() or 0, prev_space_after or 0)
    return h + gap


def estimate_story_height(story: List, usable_width: float) -> float:
    """Measured height of a ReportLab story sequence when appended to one frame."""
    total = 0.0
    prev_after = 0.0
    for f in story:
        _w, h = f.wrap(usable_width, _MAX_WRAP_HEIGHT)
        total += h + max(f.getSpaceBefore() or 0, prev_after)
        prev_after = f.getSpaceAfter() or 0
    return total


def sanitize_text(text: str) -> str:
    """Sanitize text to clean standard characters for XML/PDF flowables."""
    if not text:
        return ""
    text = text.replace(chr(0x2013), "-")      # en-dash -> hyphen
    text = text.replace(chr(0x2014), " - ")    # em-dash -> spaced hyphen
    text = text.replace(chr(0x2212), "-")      # minus sign -> hyphen
    text = text.replace(chr(0x2192), "->")     # right arrow
    text = text.replace(chr(0x2018), "'")      # left single quote
    text = text.replace(chr(0x2019), "'")      # right single quote
    text = text.replace(chr(0x201C), chr(34))  # left double quote
    text = text.replace(chr(0x201D), chr(34))  # right double quote
    text = text.replace(chr(0x2022), "*")      # bullet
    text = text.replace(chr(0x2026), "...")    # ellipsis
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


def clean_link_display(url: Optional[str]) -> str:
    """Format a clean display label for a URL by removing scheme and trailing slash."""
    if not url:
        return ""
    clean = url.replace("https://", "").replace("http://", "").rstrip("/")
    return clean


class PDFResumeBuilder:
    """ReportLab-based vector ATS resume builder producing pixel-perfect single-page layouts."""

    def __init__(self, left_margin: float = 32, right_margin: float = 32, top_margin: float = 24, bottom_margin: float = 24):
        self.left_margin = left_margin
        self.right_margin = right_margin
        self.top_margin = top_margin
        self.bottom_margin = bottom_margin
        self.story = []
        self._init_styles()

    def _init_styles(self):
        styles = getSampleStyleSheet()
        self.name_style = ParagraphStyle(
            "DocName",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=16.0,
            leading=18.0,
            alignment=TA_CENTER,
            textColor=colors.black,
            spaceAfter=1.5,
        )
        self.subtitle_style = ParagraphStyle(
            "DocSubtitle",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=10.5,
            alignment=TA_CENTER,
            textColor=colors.black,
            spaceAfter=1.5,
        )
        self.contact_style = ParagraphStyle(
            "DocContact",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8.0,
            leading=10.0,
            alignment=TA_CENTER,
            textColor=colors.black,
            spaceAfter=3.5,
        )
        self.sec_title_style = ParagraphStyle(
            "SecTitle",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=9.5,
            leading=11.5,
            textColor=colors.black,
            spaceBefore=3.5,
            spaceAfter=1,
        )
        self.body_style = ParagraphStyle(
            "Body",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8.2,
            leading=10.2,
            textColor=colors.black,
        )
        self.bullet_style = ParagraphStyle(
            "Bullet",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8.2,
            leading=10.2,
            leftIndent=11,
            firstLineIndent=-11,
            spaceAfter=1.2,
            textColor=colors.black,
        )

    def render_header(
        self,
        name: str,
        contact_str: str,
        subtitle: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        linkedin_url: Optional[str] = None,
        github_url: Optional[str] = None,
        portfolio_url: Optional[str] = None,
    ):
        name_clean = sanitize_text(name).upper()
        self.story.append(Paragraph(f"<b>{name_clean}</b>", self.name_style))

        if subtitle:
            sub_clean = sanitize_text(subtitle)
            self.story.append(Paragraph(sub_clean, self.subtitle_style))

        # Build interactive contact line with clickable links
        parts = []
        if email:
            parts.append(f'<a href="mailto:{email}" color="#1a0dab">{email}</a>')
        if phone:
            parts.append(phone)
        if linkedin_url:
            parts.append(f'<a href="{linkedin_url}" color="#1a0dab">{clean_link_display(linkedin_url)}</a>')
        if github_url:
            parts.append(f'<a href="{github_url}" color="#1a0dab">{clean_link_display(github_url)}</a>')
        if portfolio_url:
            parts.append(f'<a href="{portfolio_url}" color="#1a0dab">{clean_link_display(portfolio_url)}</a>')

        if parts:
            contact_html = "  |  ".join(parts)
        else:
            contact_html = sanitize_text(contact_str)

        self.story.append(Paragraph(contact_html, self.contact_style))

    def render_section_header(self, title: str, story: Optional[List] = None):
        title_clean = sanitize_text(title).upper()
        sink = story if story is not None else self.story
        sink.append(Paragraph(f"<b>{title_clean}</b>", self.sec_title_style))
        sink.append(HRFlowable(width="100%", thickness=0.5, color=colors.black, spaceBefore=0.5, spaceAfter=2.5))

    def render_summary(self, summary_text: str):
        if not summary_text:
            return
        self.story.append(Paragraph(sanitize_text(summary_text), self.body_style))
        self.story.append(Spacer(1, 2.5))

    def render_education(
        self,
        institution: str,
        location: str,
        degree: str,
        dates: str,
        cgpa_str: Optional[str] = None,
    ):
        inst_clean = sanitize_text(institution)
        loc_clean = sanitize_text(location)
        deg_clean = sanitize_text(degree)
        dates_clean = sanitize_text(dates)

        deg_line = deg_clean
        if cgpa_str:
            deg_line += f" | {sanitize_text(cgpa_str)}"

        edu_data = [
            [
                Paragraph(f"<b>{inst_clean}</b>", self.body_style),
                Paragraph(f"<b>{dates_clean}</b>", ParagraphStyle("ER", parent=self.body_style, alignment=TA_RIGHT)),
            ],
            [
                Paragraph(deg_line, self.body_style),
                Paragraph(loc_clean, ParagraphStyle("ER2", parent=self.body_style, alignment=TA_RIGHT)),
            ]
        ]
        t = Table(edu_data, colWidths=[380, 168])
        t.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('BOTTOMPADDING', (0,0), (-1,-1), 0.5),
            ('TOPPADDING', (0,0), (-1,-1), 0.5),
            ('LEFTPADDING', (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 0),
        ]))
        self.story.append(t)
        self.story.append(Spacer(1, 2.5))

    def render_technical_skills(
        self,
        languages: List[str],
        frameworks: List[str],
        tools: List[str],
        concepts: List[str],
    ):
        rows = [
            ("Programming Languages", languages),
            ("Frameworks & Libraries", frameworks),
            ("Cloud, Databases & Tools", tools),
            ("Core Concepts", concepts),
        ]
        for cat_name, items in rows:
            if items:
                items_str = ", ".join(sanitize_text(i) for i in items)
                line_html = f"<b>{cat_name}:</b> {items_str}"
                self.story.append(Paragraph(line_html, self.body_style))
                self.story.append(Spacer(1, 0.5))
        self.story.append(Spacer(1, 2.5))

    def render_project(
        self,
        title: str,
        tech_stack: List[str],
        bullets: List[str],
        date_str: str = "2026",
        github_url: Optional[str] = None,
        story: Optional[List] = None,
    ):
        sink = story if story is not None else self.story
        title_clean = sanitize_text(title)
        date_clean = sanitize_text(date_str)

        if github_url:
            clean_gh = clean_link_display(github_url)
            header_html = f'<b>{title_clean}</b>  |  <a href="{github_url}" color="#1a0dab">{clean_gh}</a>'
        else:
            header_html = f'<b>{title_clean}</b>'

        pt = Table([
            [
                Paragraph(header_html, self.body_style),
                Paragraph(f"<b>{date_clean}</b>", ParagraphStyle("PR", parent=self.body_style, alignment=TA_RIGHT)),
            ]
        ], colWidths=[420, 128])
        pt.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('BOTTOMPADDING', (0,0), (-1,-1), 0),
            ('TOPPADDING', (0,0), (-1,-1), 0),
            ('LEFTPADDING', (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 0),
        ]))
        sink.append(pt)

        if tech_stack:
            tech_str = ", ".join(sanitize_text(t) for t in tech_stack)
            sink.append(Paragraph(f"<i>{tech_str}</i>", self.body_style))

        for b in bullets:
            bullet_clean = sanitize_text(b).lstrip("*-? ").strip()
            sink.append(Paragraph(f"&bull; {bullet_clean}", self.bullet_style))

        sink.append(Spacer(1, 1.5))

    def render_research_experience(
        self,
        title: str,
        organization: str,
        date_range: str,
        bullets: List[str],
        story: Optional[List] = None,
    ):
        sink = story if story is not None else self.story
        t_clean = sanitize_text(title)
        org_clean = sanitize_text(organization)
        dates_clean = sanitize_text(date_range)

        rt = Table([
            [
                Paragraph(f"<b>{t_clean}</b>", self.body_style),
                Paragraph(f"<b>{dates_clean}</b>", ParagraphStyle("RXR", parent=self.body_style, alignment=TA_RIGHT)),
            ],
            [
                Paragraph(f"<i>{org_clean}</i>", self.body_style),
                Paragraph("", self.body_style),
            ]
        ], colWidths=[420, 128])
        rt.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('BOTTOMPADDING', (0,0), (-1,-1), 0),
            ('TOPPADDING', (0,0), (-1,-1), 0),
            ('LEFTPADDING', (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 0),
        ]))
        sink.append(rt)

        for b in bullets:
            bullet_clean = sanitize_text(b).lstrip("*-? ").strip()
            sink.append(Paragraph(f"&bull; {bullet_clean}", self.bullet_style))

        sink.append(Spacer(1, 2.5))

    def render_achievements(self, achievements: List[str], story: Optional[List] = None):
        sink = story if story is not None else self.story
        for ach in achievements:
            ach_clean = sanitize_text(ach).lstrip("*-? ").strip()
            sink.append(Paragraph(f"&bull; {ach_clean}", self.bullet_style))

    def compile_pdf_bytes(self) -> bytes:
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=letter,
            leftMargin=self.left_margin,
            rightMargin=self.right_margin,
            topMargin=self.top_margin,
            bottomMargin=self.bottom_margin,
        )
        doc.build(self.story)
        return buf.getvalue()

    def save_to_file(self, file_path: Path) -> Path:
        target = Path(file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        pdf_bytes = self.compile_pdf_bytes()
        target.write_bytes(pdf_bytes)
        return target


def generate_resume_pdf(
    profile: CandidateProfile,
    tailored_llm,
    output_path: Path,
) -> Path:
    """Produce an ATS-optimized, high-fidelity vector PDF resume matching the Word master resume."""
    builder = PDFResumeBuilder()

    # 1. Header with Clickable Links
    subtitle = getattr(profile, "subtitle", None) or "Computer Science Engineering Undergraduate | VIT Chennai"
    builder.render_header(
        name=profile.full_name,
        contact_str=f"{profile.email or ''} | {profile.phone or ''}",
        subtitle=subtitle,
        email=profile.email,
        phone=profile.phone,
        linkedin_url=profile.linkedin_url,
        github_url=profile.github_url,
        portfolio_url=profile.portfolio_url,
    )

    # 2. Professional Summary (profile summary; the tailored match_rationale is
    #    surfaced in the chat/CLI deliverables and audit report, not the resume itself)
    if profile.summary:
        builder.render_section_header("Professional Summary")
        builder.render_summary(profile.summary)

    # 3. Education
    builder.render_section_header("Education")
    dates_str = getattr(profile, "education_dates", None) or f"Aug 2024 - May {profile.graduation_year} (Expected)"
    cgpa_val = f"CGPA: {profile.cgpa}/10.0" if profile.cgpa else None
    builder.render_education(
        institution=profile.college,
        location="Chennai, India",
        degree=profile.degree,
        dates=dates_str,
        cgpa_str=cgpa_val,
    )

    # 4. Technical Skills (Categorized)
    builder.render_section_header("Technical Skills")
    builder.render_technical_skills(
        languages=tailored_llm.languages,
        frameworks=tailored_llm.frameworks,
        tools=tailored_llm.tools_and_cloud,
        concepts=tailored_llm.core_concepts,
    )

    # 5. Projects with Clickable GitHub URLs and measured single-page space filling
    builder.render_section_header("Projects")
    profile_projects_map = {
        p.title.lower(): p for p in profile.verified_projects
    }

    # If tailored_projects exists, use them
    tailored_projects = getattr(tailored_llm, "tailored_projects", [])
    if not tailored_projects:
        tailored_projects = profile.verified_projects[:3]

    primary_projects = [p for p in tailored_projects if not getattr(p, "is_filler", False)]
    filler_projects = [p for p in tailored_projects if getattr(p, "is_filler", False)][: _MAX_FILLER_CANDIDATES]
    if not primary_projects and filler_projects:
        primary_projects = filler_projects[:3]
        filler_projects = []

    usable_w, usable_h = usable_frame_geometry(builder)
    bottom_floor = _BOTTOM_BREATHING_ROOM_IN * 72.0

    est_prefix = estimate_story_height(builder.story, usable_w)

    # Reserved tail sections (Research + Achievements) are measured off-line first so
    # filler placement can never push them onto a second page
    tail_story: List = []
    research_list = getattr(profile, "research_experience", [])
    if research_list:
        builder.render_section_header("Research Experience", story=tail_story)
        for rx in research_list:
            rx_bullets = rx.get("bullet_points", [])[:2]  # Top 2 bullets for tight single-page fit
            builder.render_research_experience(
                title=rx.get("title", "Research Worklet"),
                organization=rx.get("organization", "VIT"),
                date_range=rx.get("date_range", "2026 - Present"),
                bullets=rx_bullets,
                story=tail_story,
            )
    achievements = getattr(profile, "achievements", [])
    if achievements:
        builder.render_section_header("Achievements & Certifications", story=tail_story)
        builder.render_achievements(achievements[:3], story=tail_story)
    tail_h = estimate_story_height(tail_story, usable_w)

    def _resolve_orig(tp):
        """Match a tailored project to its master knowledge-base entry for authentic URL/date."""
        matched_orig = None
        for k, v in profile_projects_map.items():
            if k in tp.title.lower() or tp.title.lower() in k:
                matched_orig = v
                break
        gh_url = getattr(tp, "github_url", None) or (matched_orig.github_url if matched_orig else None)
        date_str = (
            matched_orig.date_range
            if matched_orig and hasattr(matched_orig, "date_range") and matched_orig.date_range
            else "2026"
        )
        tech = getattr(tp, "tech_stack", [])
        bullets = getattr(tp, "bullet_points", [])
        if not bullets and matched_orig and hasattr(matched_orig, "bullet_points") and matched_orig.bullet_points:
            bullets = matched_orig.bullet_points
        if not bullets:
            bullets = [getattr(tp, "description", tp.title)]
        return tech, list(bullets), date_str, gh_url

    def _measure_project(title, tech, bullets, date_str, gh_url, cap: int) -> List:
        """Flowables for one project rendered at the given bullet cap (height-measured)."""
        flows: List = []
        builder.render_project(
            title=title,
            tech_stack=tech,
            bullets=bullets[:cap],
            date_str=date_str,
            github_url=gh_url,
            story=flows,
        )
        return flows

    def _bullet_est(text: str) -> float:
        bullet_clean = sanitize_text(text).lstrip("*-? ").strip()
        para = Paragraph(f"&bull; {bullet_clean}", builder.bullet_style)
        _w, h = para.wrap(usable_w, _MAX_WRAP_HEIGHT)
        return h + max(builder.bullet_style.spaceBefore or 0, builder.bullet_style.spaceAfter or 0)

    resolved_primary = [(p, *_resolve_orig(p)) for p in primary_projects]

    room_hard = usable_h - tail_h - est_prefix
    fill_budget = usable_h - bottom_floor - tail_h - est_prefix

    # Phase 1 (mandatory): base depth per original calibration; trim if page is dense
    caps: List[int] = []
    est_primary_total = 0.0
    for idx, (tp, tech, bullets, date_str, gh_url) in enumerate(resolved_primary):
        base_cap = min(2, len(bullets)) if idx < 2 else min(1, len(bullets))
        cap = base_cap
        est_proj = estimate_story_height(_measure_project(tp.title, tech, bullets, date_str, gh_url, cap), usable_w)
        est_all = est_primary_total + est_proj
        while est_all > room_hard and cap > 1:
            cap -= 1
            est_proj = estimate_story_height(_measure_project(tp.title, tech, bullets, date_str, gh_url, cap), usable_w)
            est_all = est_primary_total + est_proj
        caps.append(cap)
        est_primary_total += est_proj

    # Phase 2 (depth-first fill): restore extra bullets for the most relevant projects
    fill_left = fill_budget - est_primary_total
    for idx, (tp, tech, bullets, date_str, gh_url) in enumerate(resolved_primary):
        while caps[idx] < len(bullets):
            est_bullet = _bullet_est(bullets[caps[idx]])
            if est_bullet > fill_left:
                break
            fill_left -= est_bullet
            caps[idx] += 1

    # Phase 3 (breadth fill): ranked filler candidates, one bullet each, while space allows
    included_filler_flows: List[List] = []
    for fp in filler_projects:
        fp_tech, fp_bullets, fp_date, fp_gh = _resolve_orig(fp)
        fp_flows = _measure_project(fp.title, fp_tech, fp_bullets, fp_date, fp_gh, min(1, len(fp_bullets)))
        est_filler = estimate_story_height(fp_flows, usable_w)
        if est_filler > fill_left:
            break
        fill_left -= est_filler
        included_filler_flows.append(fp_flows)

    # Final render straight from the measured decisions
    for idx, (tp, tech, bullets, date_str, gh_url) in enumerate(resolved_primary):
        builder.story.extend(_measure_project(tp.title, tech, bullets, date_str, gh_url, caps[idx]))
    for flows in included_filler_flows:
        builder.story.extend(flows)

    # 6/7. Reserved tail sections (Research Experience + Achievements), appended untouched
    builder.story.extend(tail_story)

    _LAST_FIT_STATS.clear()
    _LAST_FIT_STATS.update(
        page_budget=usable_h - bottom_floor,
        usable_h=usable_h,
        est_prefix=est_prefix,
        tail_h=tail_h,
        est_primary_total=est_primary_total,
        fillers_included=len(included_filler_flows),
        fill_left=fill_left,
        estimated_total=estimate_story_height(builder.story, usable_w),
    )

    return builder.save_to_file(output_path)
