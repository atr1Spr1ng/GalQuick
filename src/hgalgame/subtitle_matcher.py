"""Conservative, offline matching of ordinary subtitle text to aligned rows."""
from dataclasses import dataclass
from difflib import SequenceMatcher
import unicodedata


def normalize(text):
    text = unicodedata.normalize('NFKC', text)
    return ''.join(c for c in text if not c.isspace() and unicodedata.category(c) != 'Cf')


@dataclass(frozen=True)
class MatchResult:
    translation: str | None = None
    reason: str = '未找到原文候选'
    source: str = ''
    score: float = 0
    runner_up: float = 0
    position: int | None = None
    lines: tuple = ()  # (position, display speaker, translated text)


class SubtitleMatcher:
    """Rows are (speaker, original, translation-or-None), in script order.

    Never invent translations or fuzzy-match short utterances. Adjacent rows
    may be merged, but only within the supplied scope. Cursor hints only break
    exact repeated-line ties when the next position is known unambiguously.
    """
    def __init__(self, rows, threshold=.92, margin=.05):
        self.rows = list(rows)
        self.threshold, self.margin = threshold, margin
        self.cursor = None
        self.last_query = None
        self.last_result = None
        self.candidates = []
        for start in range(len(self.rows)):
            for size in range(1, min(3, len(self.rows)-start)+1):
                part = self.rows[start:start+size]
                source = '\n'.join(r[1] for r in part)
                aliases = {normalize(source), normalize(''.join(r[0]+r[1] for r in part))}
                translated = '\n'.join(r[2] for r in part) if all(r[2] for r in part) else None
                self.candidates.append((start, size, source, aliases, translated))

    def match(self, text, speaker=''):
        query = normalize(text)
        cache_key = (query, speaker)
        if cache_key == self.last_query:
            return self.last_result
        if not query or len(query) > 12000:
            return MatchResult(reason='文本为空或过长')
        ranked = []
        # Fast-forward can deliver a batch much longer than three rows.
        # Expand exact candidates only; never fuzzy-match arbitrary long spans.
        for start in range(len(self.rows)):
            body = named = ''
            for end in range(start, min(start+64, len(self.rows))):
                row = self.rows[end]
                body += normalize(row[1])
                named += normalize(row[0]+row[1])
                if end-start >= 3 and query in (body, named):
                    part = self.rows[start:end+1]
                    if not speaker or normalize(part[0][0]) == normalize(speaker):
                        ranked.append((1, start, len(part), '\n'.join(r[1] for r in part),
                                       '\n'.join(r[2] for r in part) if all(r[2] for r in part) else None))
                if len(body) > len(query):
                    break
        for start, size, source, aliases, translated in self.candidates:
            if speaker and normalize(self.rows[start][0]) != normalize(speaker):
                continue
            score = 0
            for alias in aliases:
                if alias == query:
                    score = 1
                    break
                if len(query) < 12 or not alias:
                    continue
                # Length bound avoids expensive comparisons of unrelated rows.
                if 2*min(len(query), len(alias))/(len(query)+len(alias)) < self.threshold:
                    continue
                score = max(score, SequenceMatcher(None, query, alias, autojunk=False).ratio())
            if score:
                ranked.append((score, start, size, source, translated))
        ranked.sort(key=lambda r: (-r[0], r[1], r[2]))
        if not ranked:
            result = MatchResult(reason='未找到可靠原文；短句仅精确匹配，拆分片段需等待完整台词')
        else:
            best = ranked[0]
            exact = [r for r in ranked if r[0] == 1]
            if len({r[4] for r in exact}) > 1 and self.cursor is not None:
                next_rows = [r for r in exact if r[1] == self.cursor+1]
                if len(next_rows) == 1:
                    best = next_rows[0]
                    ranked = [best] + [r for r in ranked if r[0] < 1]
            other = max((r[0] for r in ranked if r[4] != best[4]), default=0)
            score, start, size, source, translated = best
            reason = ('原文已匹配，但缺少译文' if not translated else
                      '候选太接近，无法唯一确定' if score-other < self.margin else
                      '相似度不足' if score < self.threshold else
                      '精确匹配' if score == 1 else '相似匹配')
            accepted = translated if translated and score >= self.threshold and score-other >= self.margin else None
            lines = tuple((i+1, (r[3] if len(r)>3 and r[3] else r[0]), r[2])
                          for i,r in enumerate(self.rows[start:start+size], start)) if accepted else ()
            result = MatchResult(accepted, reason, source, score, other, start+1, lines)
            if accepted:
                self.cursor = start+size-1
        self.last_query, self.last_result = cache_key, result
        return result
