from __future__ import annotations
import re
from pathlib import Path
from ..config import get_settings
from ..schemas import AnalysisJobCreate, CodeContextItem

TEXT_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".json", ".yaml", ".yml", ".toml", ".env", ".ini", ".sh", ".md"}

class SelectiveCodeRetriever:
    """The 'Eyes' of the agent. Pure logic decoupled from the agent loop."""
    def __init__(self) -> None:
        self.settings = get_settings()
        self.read_roots = [root for root in self.settings.read_roots if root.exists()]

    def retrieve(self, payload: AnalysisJobCreate) -> list[CodeContextItem]:
        if not self.read_roots: return []
        keywords = self._extract_keywords(payload)
        if not keywords: return []

        scored = []
        for file_path in self._iter_candidate_files():
            content = self._safe_read_text(file_path)
            if not content: continue
            score = self._score_text(file_path, content, keywords)
            if score > 0: scored.append((score, file_path, content))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for _, file_path, content in scored[:self.settings.max_context_files]:
            start, end, excerpt = self._build_excerpt(content, keywords)
            results.append(CodeContextItem(
                file_path=str(file_path.relative_to(self.settings.project_root)),
                line_start=start, line_end=end, excerpt=excerpt[:self.settings.max_context_excerpt_chars]
            ))
        return results

    def _extract_keywords(self, payload: AnalysisJobCreate) -> set[str]:
        tokens = [payload.service_name, payload.uptime_description, payload.device_or_node]
        tokens.extend(s.line for s in payload.log_snippets)
        words = re.findall(r"[a-zA-Z0-9_./-]+", " ".join(tokens).lower())
        return {w for w in words if len(w) >= 4}

    def _iter_candidate_files(self):
        for root in self.read_roots:
            for f in root.rglob("*"):
                if f.is_file() and f.suffix.lower() in TEXT_EXTENSIONS: yield f

    def _safe_read_text(self, path: Path) -> str:
        try: return path.read_text(encoding="utf-8", errors="ignore")
        except: return ""

    def _score_text(self, file_path: Path, content: str, keywords: set[str]) -> int:
        haystack = f"{file_path.name.lower()}\n{content.lower()}"
        return sum(haystack.count(k) for k in keywords)

    def _build_excerpt(self, content: str, keywords: set[str]) -> tuple[int, int, str]:
        lines = content.splitlines()
        idx = next((i for i, l in enumerate(lines) if any(k in l.lower() for k in keywords)), 0)
        start, end = max(0, idx - 4), min(len(lines), idx + 5)
        return start + 1, end, "\n".join(lines[start:end])
