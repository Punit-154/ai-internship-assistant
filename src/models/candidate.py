"""Candidate Profile Model and Preferences Schema.

Represents your student background, graduation timeline, domain targets,
and location/availability preferences.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ProjectItem(BaseModel):
    """Structured representation of a verified student project."""
    title: str = Field(description="Clear title of the project")
    tech_stack: List[str] = Field(default_factory=list, description="Technologies, frameworks, and databases used")
    description: str = Field(description="Concise, impact-oriented summary of what the project does")
    github_url: Optional[str] = Field(default=None, description="GitHub repository URL if available")
    live_demo_url: Optional[str] = Field(default=None, description="Live deployed URL or demo link if available")
    date_range: Optional[str] = Field(default=None, description="Project duration date range (e.g., 'Aug 2026 - Sep 2026')")
    bullet_points: List[str] = Field(default_factory=list, description="Detailed verified bullet points describing accomplishments")


class CandidateProfile(BaseModel):
    """Personal candidate profile and internship constraints."""

    # Personal & Academic Background
    full_name: str = Field(default="Your Full Name", description="Candidate full name")
    subtitle: str = Field(default="Computer Science Engineering Undergraduate | VIT Chennai", description="Professional subtitle")
    college: str = Field(default="Vellore Institute of Technology (VIT), Chennai", description="University name")
    degree: str = Field(default="B.Tech in Computer Science Engineering", description="Degree program")
    current_college_year: int = Field(default=3, description="Current year of study (3rd Year / Pre-final)")
    graduation_year: int = Field(default=2028, description="Target graduation year")
    education_dates: str = Field(default="Aug 2024 - Aug 2028 (Expected)", description="Duration of academic program")
    cgpa: Optional[float] = Field(default=8.5, description="Academic CGPA")
    email: Optional[str] = Field(default="you@example.com", description="Candidate contact email")
    phone: Optional[str] = Field(default="+91 0000000000", description="Candidate contact phone number")
    linkedin_url: Optional[str] = Field(default="https://linkedin.com/in/your-handle", description="Candidate LinkedIn URL")
    github_url: Optional[str] = Field(default="https://github.com/your-handle", description="Candidate GitHub URL")
    portfolio_url: Optional[str] = Field(default="https://your-portfolio.example.com", description="Candidate portfolio website URL")
    summary: Optional[str] = Field(
        default="Computer Science undergraduate seeking Software Engineering Intern and Cloud/DevOps Intern roles. Proficient in Python, Amazon Web Services (AWS), and FastAPI, with hands-on experience building scalable RESTful APIs and Generative AI / Retrieval-Augmented Generation (RAG) applications.",
        description="Professional summary",
    )

    # Target Career Tracks
    target_roles: List[str] = Field(
        default=[
            "Software Engineering Intern",
            "Backend Engineering Intern",
            "Full Stack Developer Intern",
            "Cloud Engineering Intern",
            "AI/ML Intern",
        ],
        description="Target job titles and tracks",
    )

    # Availability & Location Constraints
    current_availability: str = Field(
        default="Remote",
        description="Immediate availability during college semester (Remote only)",
    )
    summer_locations: List[str] = Field(
        default=["Bangalore", "Chennai", "Hyderabad"],
        description="Primary nearby locations for on-site/hybrid summer internships",
    )
    relocation_preference: str = Field(
        default="Willing to relocate farther for summer if company provides relocation assistance or housing stipend",
        description="Policy for locations outside primary hubs",
    )

    # Verified Technical Skills (Zero fabrication base)
    verified_skills: List[str] = Field(
        default=[
            "Python",
            "Java",
            "C",
            "FastAPI",
            "Flask",
            "Next.js",
            "Django",
            "Django REST Framework",
            "PostgreSQL",
            "AWS",
            "Git",
            "Data Structures",
            "Algorithms",
            "RESTful APIs",
            "pytest",
        ],
        description="Skills you have actively used and can defend in interviews",
    )

    # Projects Portfolio (Type-safe ProjectItem records)
    verified_projects: List[ProjectItem] = Field(
        default_factory=list,
        description="List of verified personal projects and achievements",
    )

    # Research & Industry Experience
    research_experience: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Verified research worklets or industry lab experience",
    )

    # Achievements & Certifications
    achievements: List[str] = Field(
        default_factory=list,
        description="Verified hackathon awards, honors, and industrial certifications",
    )

    notes: Optional[str] = Field(
        default="Prioritize backend, cloud, and AI engineering opportunities with mentorship.",
        description="General guidance notes",
    )
