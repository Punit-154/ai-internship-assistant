"""Pure-Python ATS-Compliant PDF Resume Generator.

Produces clean, single-page, vector PDF resumes using standard Type 1 fonts
(Helvetica, Helvetica-Bold, Helvetica-Oblique) adhering strictly to the
industry-standard Jake's Resume layout:
- Standard 0.5-inch margins (36 pt)
- Bold section headers with crisp dividing rules
- Left-aligned titles with right-aligned dates/locations
- STAR/X-Y-Z bullet points with proper hanging indents
- Fully text-searchable and 100% parseable by modern ATS systems (Workday, Greenhouse, Taleo)
- Zero external binary or LaTeX compiler dependencies
"""

from pathlib import Path
from typing import List, Optional
from src.models.candidate import CandidateProfile


def sanitize_text(text: str) -> str:
    """Sanitize text to standard ASCII/Latin-1 characters for Type 1 fonts."""
    if not text:
        return ""
    replacements = {
        "\u2013": "-",   # en-dash
        "\u2014": "--",  # em-dash
        "\u2018": "'",   # left single quote
        "\u2019": "'",   # right single quote
        "\u201c": '"',   # left double quote
        "\u201d": '"',   # right double quote
        "\u2022": "*",   # bullet
        "\u2026": "...", # ellipsis
        "\u00a0": " ",   # non-breaking space
        "\u2010": "-",   # hyphen
    }
    for orig, rep in replacements.items():
        text = text.replace(orig, rep)
    # Encode to latin-1, replacing unknown chars
    return text.encode("latin-1", "replace").decode("latin-1")


class PDFResumeBuilder:
    """Builds an ATS-optimized Jake's Resume PDF in pure Python."""

    PAGE_WIDTH = 612.0   # Letter size width
    PAGE_HEIGHT = 792.0  # Letter size height
    MARGIN_LEFT = 36.0   # 0.5 inch margins
    MARGIN_RIGHT = 576.0 # 612 - 36
    CONTENT_WIDTH = 540.0

    def __init__(self):
        self.stream_commands: List[bytes] = []
        self.cursor_y = 756.0  # Top margin: 792 - 36

    def _escape_pdf_str(self, text: str) -> str:
        clean = sanitize_text(text)
        return clean.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    def add_command(self, cmd: str) -> None:
        self.stream_commands.append(cmd.encode("latin1"))

    def draw_text(self, text: str, x: float, y: float, font: str = "F2", size: float = 10.0) -> None:
        escaped = self._escape_pdf_str(text)
        self.add_command(f"BT /{font} {size:.2f} Tf {x:.2f} {y:.2f} Td ({escaped}) Tj ET\n")

    def estimate_text_width(self, text: str, font: str, size: float) -> float:
        """Estimate text width in points for Helvetica."""
        factor = 0.54 if "Bold" in font or font == "F1" else 0.50
        return len(text) * size * factor

    def draw_centered_text(self, text: str, y: float, font: str = "F2", size: float = 10.0) -> None:
        width = self.estimate_text_width(text, font, size)
        x = (self.PAGE_WIDTH - width) / 2.0
        self.draw_text(text, max(self.MARGIN_LEFT, x), y, font, size)

    def draw_right_aligned_text(self, text: str, right_x: float, y: float, font: str = "F2", size: float = 10.0) -> None:
        width = self.estimate_text_width(text, font, size)
        x = right_x - width
        self.draw_text(text, x, y, font, size)

    def draw_horizontal_rule(self, y: float, width: float = 0.5) -> None:
        self.add_command(f"{width:.2f} w 0.25 0.25 0.25 RG {self.MARGIN_LEFT:.2f} {y:.2f} m {self.MARGIN_RIGHT:.2f} {y:.2f} l S\n")

    def wrap_text(self, text: str, max_width: float, font: str, size: float) -> List[str]:
        """Wrap text into lines fitting within max_width."""
        words = text.split(" ")
        lines = []
        current_line = []

        for word in words:
            test_line = " ".join(current_line + [word])
            if self.estimate_text_width(test_line, font, size) <= max_width:
                current_line.append(word)
            else:
                if current_line:
                    lines.append(" ".join(current_line))
                    current_line = [word]
                else:
                    lines.append(word)
                    current_line = []
        if current_line:
            lines.append(" ".join(current_line))
        return lines

    def render_header(self, name: str, contact_line: str) -> None:
        # Full Name: Bold 17pt
        self.draw_centered_text(name.upper(), self.cursor_y, font="F1", size=17.0)
        self.cursor_y -= 14.0
        # Contact details: 9pt
        self.draw_centered_text(contact_line, self.cursor_y, font="F2", size=9.0)
        self.cursor_y -= 14.0

    def render_section_header(self, title: str) -> None:
        self.cursor_y -= 4.0
        self.draw_text(title.upper(), self.MARGIN_LEFT, self.cursor_y, font="F1", size=10.5)
        self.cursor_y -= 3.0
        self.draw_horizontal_rule(self.cursor_y, width=0.6)
        self.cursor_y -= 10.0

    def render_education(self, college: str, location: str, degree: str, grad_date: str) -> None:
        self.draw_text(college, self.MARGIN_LEFT, self.cursor_y, font="F1", size=10.0)
        self.draw_right_aligned_text(location, self.MARGIN_RIGHT, self.cursor_y, font="F2", size=9.5)
        self.cursor_y -= 11.0
        self.draw_text(degree, self.MARGIN_LEFT, self.cursor_y, font="F3", size=9.5)
        self.draw_right_aligned_text(grad_date, self.MARGIN_RIGHT, self.cursor_y, font="F3", size=9.0)
        self.cursor_y -= 12.0

    def render_technical_skills(
        self,
        languages: List[str],
        frameworks: List[str],
        tools: List[str],
        concepts: List[str],
    ) -> None:
        categories = [
            ("Languages", ", ".join(languages)),
            ("Frameworks & Libraries", ", ".join(frameworks)),
            ("Developer Tools & Cloud", ", ".join(tools)),
            ("Core Concepts", ", ".join(concepts)),
        ]
        for label, val in categories:
            if not val:
                continue
            prefix = f"{label}: "
            prefix_w = self.estimate_text_width(prefix, "F1", 9.5)
            self.draw_text(prefix, self.MARGIN_LEFT, self.cursor_y, font="F1", size=9.5)

            available_w = self.CONTENT_WIDTH - prefix_w
            lines = self.wrap_text(val, available_w, font="F2", size=9.0)
            if lines:
                self.draw_text(lines[0], self.MARGIN_LEFT + prefix_w, self.cursor_y, font="F2", size=9.0)
                for cont_line in lines[1:]:
                    self.cursor_y -= 10.0
                    self.draw_text(cont_line, self.MARGIN_LEFT + prefix_w, self.cursor_y, font="F2", size=9.0)
            self.cursor_y -= 11.0

    def render_project(
        self,
        title: str,
        tech_stack: List[str],
        bullets: List[str],
        date_str: str = "2026",
        github_url: Optional[str] = None,
    ) -> None:
        # Title in bold
        title_text = title
        tech_text = f" | {', '.join(tech_stack)}" if tech_stack else ""
        if github_url:
            tech_text += " | GitHub"

        self.draw_text(title_text, self.MARGIN_LEFT, self.cursor_y, font="F1", size=10.0)
        title_w = self.estimate_text_width(title_text, "F1", 10.0)

        # Tech stack italic next to title
        max_tech_w = self.MARGIN_RIGHT - self.MARGIN_LEFT - title_w - 60.0
        if tech_text and max_tech_w > 50:
            short_tech = tech_text
            if self.estimate_text_width(short_tech, "F3", 9.0) > max_tech_w:
                short_tech = short_tech[:int(max_tech_w / 5)] + "..."
            self.draw_text(short_tech, self.MARGIN_LEFT + title_w, self.cursor_y, font="F3", size=9.0)

        self.draw_right_aligned_text(date_str, self.MARGIN_RIGHT, self.cursor_y, font="F2", size=9.0)
        self.cursor_y -= 11.0

        # Bullet points
        for b in bullets:
            bullet_lines = self.wrap_text(b, self.CONTENT_WIDTH - 20.0, font="F2", size=9.0)
            if not bullet_lines:
                continue

            # First line with bullet symbol
            self.draw_text("-", self.MARGIN_LEFT + 6.0, self.cursor_y, font="F1", size=9.0)
            self.draw_text(bullet_lines[0], self.MARGIN_LEFT + 16.0, self.cursor_y, font="F2", size=9.0)

            # Continuation lines
            for cont in bullet_lines[1:]:
                self.cursor_y -= 10.0
                self.draw_text(cont, self.MARGIN_LEFT + 16.0, self.cursor_y, font="F2", size=9.0)

            self.cursor_y -= 10.5
        self.cursor_y -= 3.0

    def compile_pdf_bytes(self) -> bytes:
        """Assemble all PDF stream commands into a compliant PDF 1.4 binary."""
        content_stream = b"".join(self.stream_commands)
        pdf_header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"

        objs = [
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R /F2 6 0 R /F3 7 0 R >> >> >>\nendobj\n",
            f"4 0 obj\n<< /Length {len(content_stream)} >>\nstream\n".encode("latin1") + content_stream + b"\nendstream\nendobj\n",
            b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>\nendobj\n",
            b"6 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
            b"7 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique >>\nendobj\n",
        ]

        body = b""
        offsets = [0]
        for obj in objs:
            offsets.append(len(pdf_header) + len(body))
            body += obj

        xref_offset = len(pdf_header) + len(body)
        xref = f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("latin1")
        for off in offsets[1:]:
            xref += f"{off:010d} 00000 n \n".encode("latin1")

        trailer = f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("latin1")
        return pdf_header + body + xref + trailer


def generate_resume_pdf(
    profile: CandidateProfile,
    tailored_response,
    output_pdf_path: Path,
) -> Path:
    """Generate a clean, professional, single-page ATS PDF resume."""
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    builder = PDFResumeBuilder()

    # 1. Header
    contact_parts = []
    if getattr(profile, "phone", None):
        contact_parts.append(profile.phone)
    if getattr(profile, "email", None):
        contact_parts.append(profile.email)
    if getattr(profile, "linkedin_url", None):
        clean_li = profile.linkedin_url.replace("https://", "").replace("http://", "").replace("www.", "")
        contact_parts.append(clean_li)
    if getattr(profile, "github_url", None):
        clean_gh = profile.github_url.replace("https://", "").replace("http://", "").replace("www.", "")
        contact_parts.append(clean_gh)
    if getattr(profile, "portfolio_url", None):
        clean_pf = profile.portfolio_url.replace("https://", "").replace("http://", "").replace("www.", "")
        contact_parts.append(clean_pf)

    if not contact_parts:
        contact_parts = ["you@example.com", "+91 0000000000", "github.com/Punit-154"]

    contact_str = "  |  ".join(contact_parts)
    builder.render_header(profile.full_name, contact_str)

    # 2. Education
    grad_date = f"Aug 2024 - May {profile.graduation_year} (Expected)"
    degree_str = profile.degree
    if getattr(profile, "cgpa", None):
        degree_str += f" | CGPA: {profile.cgpa}/10.0"

    builder.render_section_header("Education")
    builder.render_education(
        college=profile.college,
        location="Chennai, India",
        degree=degree_str,
        grad_date=grad_date,
    )

    # 3. Technical Skills
    builder.render_section_header("Technical Skills")
    builder.render_technical_skills(
        languages=tailored_response.languages,
        frameworks=tailored_response.frameworks,
        tools=tailored_response.tools_and_cloud,
        concepts=tailored_response.core_concepts,
    )

    # 4. Projects
    builder.render_section_header("Projects")
    for proj in tailored_response.tailored_projects:
        builder.render_project(
            title=proj.title,
            tech_stack=proj.tech_stack,
            bullets=proj.bullet_points,
            date_str="2026",
            github_url=proj.github_url,
        )

    # Compile and write bytes
    pdf_bytes = builder.compile_pdf_bytes()
    output_pdf_path.write_bytes(pdf_bytes)
    return output_pdf_path
