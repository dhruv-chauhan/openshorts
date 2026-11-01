"""Provider-neutral legacy response schemas; compatibility names remain in gemini_worker."""
from typing import List
from pydantic import BaseModel


class ScoredWindowModel(BaseModel):
    id: str
    start: float
    end: float
    score: int
    reason: str


class ScoreResponse(BaseModel):
    windows: List[ScoredWindowModel]


class DetailClipModel(BaseModel):
    start: float
    end: float
    source_window_id: str
    predicted_score: int
    video_description_for_tiktok: str
    video_description_for_instagram: str
    video_title_for_youtube_short: str
    viral_hook_text: str
    # One sentence on what makes THIS moment a clip, shown under the score in
    # the dashboard. Defaulted so a small local model that skips it does not
    # fail schema validation and lose the clip.
    why: str = ""


class DetailResponse(BaseModel):
    shorts: List[DetailClipModel]


class VisualClipModel(BaseModel):
    start: float
    end: float
    predicted_score: int
    video_description_for_tiktok: str
    video_description_for_instagram: str
    video_title_for_youtube_short: str
    viral_hook_text: str


class VisualResponse(BaseModel):
    shorts: List[VisualClipModel]


class GroundedHook(BaseModel):
    on_screen: str
    viral_hook_text: str
    video_title_for_youtube_short: str


class LayoutChoice(BaseModel):
    layout: str
    confidence: float
    why: str


class ShotContentModel(BaseModel):
    shot: int
    kind: str
    focus_left: float
    focus_right: float
    presenter_cam: bool


class ShotContentResponse(BaseModel):
    shots: List[ShotContentModel]
