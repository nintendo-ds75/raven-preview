"""The live brief that reversed both outcomes on a397f1c, and faithful and
swapped rewrites of it, through Raven's deterministic brief check. The
reproduction for R1 in reviews/main-a397f1c-live-retest.md."""
import json
from pathlib import Path

from bridge.llm import swapped_outcome

LIVE = Path(__file__).parent / "results" / "main-a397f1c-live-retest" / "cap-brief-reversal.json"
OPTIONS = ["clamp after jitter: min(retry_after_max, delay + jitter) — matches get_backoff_time precedent, "
           "cap is never exceeded",
           "jitter after clamp: delay may exceed retry_after_max by up to the jitter amount",
           "raise the effective ceiling to retry_after_max + retry_after_jitter and document it"]

if __name__ == "__main__":
    case = json.loads(LIVE.read_text())
    context = case["context"].split("\nPaths:")[0]
    briefs = {
        "live, reversed": case["brief"],
        "swapped, reworded": ("Jitter before the clamp can push waits past the documented cap, and jitter after the "
                              "clamp makes workers at the cap wake together."),
        "faithful": ("If jitter is added after the clamp, a wait can exceed retry_after_max, while if it is added "
                     "before the clamp, jitter collapses to zero near the cap."),
        "neutral": "The owner decides whether the jittered Retry-After wait may go above retry_after_max.",
    }
    print(json.dumps({name: swapped_outcome(text, context, OPTIONS) or "shown" for name, text in briefs.items()},
                     indent=2))
