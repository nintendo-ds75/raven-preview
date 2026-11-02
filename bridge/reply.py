"""A person's typed reply, filed as a call and its reason.

The For you card is one box: the person writes what they would say in chat
("Four cents an answer. That is about three times what one costs us.") and
Raven files the call and the why apart, the way the record keeps every other
answer. Deterministic and instant: no model in the path of a typed reply.
"""

from __future__ import annotations

import re

# A reason inside the sentence that states the call.
_INLINE = re.compile(r"\s*[,;:]?\s+(?:because|since|as|so that|given that|seeing that|now that)\s+", re.IGNORECASE)
# A later sentence that explains rather than instructs.
_REASON_START = re.compile(
    r"^(?:because|since|that(?:'s| is| way|’s)|this (?:way|is|keeps|avoids|means)|it(?:'s| is|’s)?\b|they|we|most|otherwise|"
    r"the (?:reason|heavy|rest)|no point|there(?:'s| is|’s)|our|everything else|"
    r"anything (?:else|more)|nobody|nothing|too|less|more|cheaper|safer|simpler|easier)\b", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“'(])")
_LEAD = re.compile(r"^(?:why|reason|rationale)\s*[:\-–—]\s*", re.IGNORECASE)


def split_reply(text: str) -> tuple[str, str]:
    """Return (answer, rationale) from one typed reply. The rationale is empty
    when the reply gives none; the answer keeps every condition that is part
    of the call itself."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return "", ""
    sentences = [s.strip() for s in _SENTENCE.split(text) if s.strip()]
    answer, reasons = [], []
    for sentence in sentences:
        if answer and (reasons or _REASON_START.match(sentence) or _LEAD.match(sentence)):
            reasons.append(_LEAD.sub("", sentence))
            continue
        parts = _INLINE.split(sentence, maxsplit=1)
        found = _INLINE.search(sentence)
        if found and len(parts) == 2 and len(parts[0]) >= 12:
            head = parts[0].rstrip(" ,;:")
            answer.append(head if head[-1] in ".!?" else head + ".")
            tail = sentence[found.start():].strip(" ,;:")
            reasons.append(tail[0].upper() + tail[1:])
            continue
        answer.append(sentence)
    return " ".join(answer).strip(), " ".join(reasons).strip()
