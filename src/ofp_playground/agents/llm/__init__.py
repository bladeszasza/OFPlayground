from .anthropic import AnthropicAgent
from .anthropic_coding import AnthropicCodingAgent
from .anthropic_vision import AnthropicVisionAgent
from .classifier import ImageClassificationAgent
from .codex import BaseCodingAgent, CodingAgent, OpenAICodingAgent
from .detector import ObjectDetectionAgent
from .google import GoogleAgent
from .google_coding import GoogleCodingAgent
from .google_image import GeminiImageAgent, GeminiVisionAgent
from .google_music import GeminiMusicAgent
from .huggingface import HuggingFaceAgent
from .multimodal import MultimodalAgent
from .ner import NERAgent
from .ocr import OCRAgent
from .openai import OpenAIAgent
from .openai_image import OpenAIImageAgent, OpenAIVisionAgent
from .segmenter import ImageSegmentationAgent
from .summarizer import SummarizationAgent
from .text_classifier import TextClassificationAgent

__all__ = [
    "AnthropicAgent",
    "AnthropicCodingAgent",
    "AnthropicVisionAgent",
    "BaseCodingAgent",
    "CodingAgent",
    "GeminiImageAgent",
    "GeminiMusicAgent",
    "GeminiVisionAgent",
    "GoogleAgent",
    "GoogleCodingAgent",
    "HuggingFaceAgent",
    "ImageClassificationAgent",
    "ImageSegmentationAgent",
    "MultimodalAgent",
    "NERAgent",
    "OCRAgent",
    "ObjectDetectionAgent",
    "OpenAIAgent",
    "OpenAICodingAgent",
    "OpenAIImageAgent",
    "OpenAIVisionAgent",
    "SummarizationAgent",
    "TextClassificationAgent",
]
