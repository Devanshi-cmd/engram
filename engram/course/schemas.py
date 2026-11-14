"""What Opus writes, as JSON schemas Claude Code enforces, and what Engram assembles from it.

Model output never carries a timestamp of its own. Every item carries an `Anchor` (a frame
id or a quote), and code resolves it (see `engram.timeline.anchor`).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from engram.timeline.anchor import Anchor

# --- Chapters (one call for the whole timeline, or map-reduce over windows) -------------


class ChapterDraft(BaseModel):
    starts_at: str = Field(
        description="The [mm:ss] or [h:mm:ss] stamp, copied exactly from the timeline, of the "
        "first line of this chapter"
    )
    title: str = Field(description="2-7 words, specific, like a textbook section heading")
    summary: str = Field(description="One sentence: what this chapter teaches")


class ChapterPlan(BaseModel):
    title: str = Field(
        description="The lecture's own title, cleaned up: no channel, course code or lecturer"
    )
    overview: str = Field(description="2-3 sentences: what the whole lecture teaches")
    chapters: list[ChapterDraft]


# --- One chapter's content ---------------------------------------------------------------


class NotePoint(BaseModel):
    text: str = Field(
        description="1-3 sentences of Markdown. Maths in $...$ or $$...$$; **bold** key terms"
    )
    anchor: Anchor


class NoteSection(BaseModel):
    heading: str
    figure: str = Field(
        description="Id of the clearest frame that shows this section's board or slide, or ''"
    )
    points: list[NotePoint]


class KeyTerm(BaseModel):
    term: str
    definition: str = Field(description="One sentence, as the lecture defines it")
    anchor: Anchor


class Diagram(BaseModel):
    title: str
    mermaid: str = Field(description="Mermaid source (flowchart preferred), at most 15 nodes")
    caption: str = Field(description="One sentence on what the diagram shows")
    anchor: Anchor


class Flashcard(BaseModel):
    front: str = Field(description="A question or cue; Markdown and $maths$ allowed")
    back: str = Field(description="The answer, at most 30 words")
    anchor: Anchor


class QuizQuestion(BaseModel):
    question: str
    options: list[str] = Field(description="Exactly 4 options", min_length=4, max_length=4)
    answer: int = Field(description="Index (0-3) of the one correct option", ge=0, le=3)
    explanation: str = Field(description="Why the answer is right, as the lecture explains it")
    evidence: str = Field(
        description="Words copied exactly from the timeline (speech or board) that support "
        "the answer"
    )
    anchor: Anchor


class Correction(BaseModel):
    frame: str
    reading_said: str = Field(description="What the timeline's reading of the frame says")
    image_shows: str = Field(description="What the image actually shows")


class ChapterContent(BaseModel):
    summary: str = Field(description="2-4 sentences")
    sections: list[NoteSection]
    key_terms: list[KeyTerm]
    diagrams: list[Diagram] = Field(description="0-2 diagrams, only where structure helps")
    flashcards: list[Flashcard]
    quiz: list[QuizQuestion]
    corrections: list[Correction] = Field(
        description="Frames you opened whose reading in the timeline was wrong"
    )


class MermaidFix(BaseModel):
    mermaid: str = Field(description="The corrected Mermaid source, or '' to give up")


# --- The grounding check (a separate, cheaper model) -------------------------------------

Verdict = Literal["supported", "unsupported", "ambiguous"]


class ItemVerdict(BaseModel):
    id: str
    verdict: Verdict
    reason: str = Field(description="One short sentence")


class GroundingReport(BaseModel):
    verdicts: list[ItemVerdict]
