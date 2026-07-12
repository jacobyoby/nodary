"""The feature registry: every feature's name, weight, and group, in one
place. Changing anything here bumps ENGINE_VERSION, which marks previously
stored scores as stale (the UI shows the version each score was computed with).

Score = min(100, Σ raw × weight). Features are monotone: absence of anomaly
contributes 0, never negative — normal-looking behavior cannot buy down a
lookalike-domain flag.
"""

from __future__ import annotations

from dataclasses import dataclass

ENGINE_VERSION = "1.0.0"

# Behavioral features need a baseline to betray.
MIN_BASELINE_N = 8
# Confidence ramp for novelty features: conf(n) = n / (n + CONF_K).
CONF_K = 10
# Lookalike candidates must be at least this long (short domains collide
# by chance) and within this OSA distance.
LOOKALIKE_MIN_LEN = 6
LOOKALIKE_MAX_DIST = 2
# Dormant-contact thresholds.
DORMANT_MIN_GAP_SECONDS = 90 * 86400
DORMANT_GAP_MULTIPLIER = 6
# Statistical clamps.
SIZE_Z_START, SIZE_Z_FULL = 2.5, 6.5
LINKS_Z_START, LINKS_Z_FULL = 2.0, 6.0
HOUR_SMOOTHING_ALPHA = 0.5

MAX_SCORE = 100.0


@dataclass(frozen=True)
class FeatureDef:
    name: str
    weight: float  # points contributed at raw = 1.0
    group: str  # 'identity' | 'behavioral' | 'cold'
    doc: str


FEATURES: dict[str, FeatureDef] = {
    f.name: f
    for f in [
        # Group A — identity & spoofing (all tiers)
        FeatureDef(
            "lookalike_domain",
            25,
            "identity",
            "Sender domain resembles a trusted domain (homoglyph "
            "skeleton collision or edit distance ≤ 2).",
        ),
        FeatureDef(
            "display_name_collision",
            25,
            "identity",
            "Display name matches a Tier-3 contact's name but the address differs.",
        ),
        FeatureDef(
            "auth_fail",
            15,
            "identity",
            "SPF/DKIM/DMARC failure recorded by the receiving server.",
        ),
        FeatureDef(
            "reply_to_divergence",
            10,
            "identity",
            "Reply-To points at a domain this sender has never used.",
        ),
        FeatureDef(
            "embedded_addr_mismatch",
            10,
            "identity",
            "Display name contains an email address whose domain "
            "differs from the real sender.",
        ),
        # Group B — behavioral shift vs sender's own baseline (tier ≥ 2)
        FeatureDef(
            "attachment_type_novelty",
            15,
            "behavioral",
            "Attachment type never seen from this sender.",
        ),
        FeatureDef(
            "first_attachment_ever",
            10,
            "behavioral",
            "First attachment of any kind from this sender.",
        ),
        FeatureDef(
            "link_domain_novelty",
            10,
            "behavioral",
            "Links to domains this sender has never linked.",
        ),
        FeatureDef(
            "send_hour_anomaly",
            8,
            "behavioral",
            "Sent at an hour (sender-local) this sender never writes.",
        ),
        FeatureDef(
            "link_density_anomaly",
            5,
            "behavioral",
            "Far more links than this sender's baseline.",
        ),
        FeatureDef(
            "size_anomaly",
            5,
            "behavioral",
            "Message size far outside this sender's baseline.",
        ),
        FeatureDef(
            "dormant_resurrection",
            5,
            "behavioral",
            "Long-dormant contact resumed, combined with other flags.",
        ),
        # Group C — cold-contact context (tier 0-1)
        FeatureDef(
            "cold_attachment", 12, "cold", "Attachment from a never-seen sender."
        ),
        FeatureDef("cold_links", 6, "cold", "Links from a never-seen sender."),
        FeatureDef(
            "cold_replyto", 8, "cold", "Never-seen sender redirects replies elsewhere."
        ),
    ]
}


def confidence(n: int) -> float:
    """0..1 ramp: how much a baseline of n messages is worth."""
    return n / (n + CONF_K)
