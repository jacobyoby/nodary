"""Offline weight calibration harness.

The bundled source replays synthetic mailbox scenarios through the real ingest
pipeline, then summarizes the stored scores. External labeled exports can plug
in by implementing CorpusSource.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from statistics import mean, median
from typing import Protocol

from nodary.feature_extraction.extract import record_from_message
from nodary.pipeline import ingest_message
from nodary.scoring.registry import FEATURES
from nodary.scoring.tiers import TIER_LABELS
from nodary.storage import connect

ME = "jacob@myco.com"
TZ = timezone(timedelta(hours=-5))
T0 = datetime(2026, 1, 5, 9, 30, tzinfo=TZ)

THRESHOLDS = (1, 5, 10, 15, 20, 25, 30, 40, 50, 60, 70)


@dataclass(frozen=True)
class CorpusLabel:
    message_row_id: int
    label: str
    subtype: str
    scenario: str


@dataclass(frozen=True)
class FeatureObservation:
    feature: str
    raw_value: float
    contribution: float


@dataclass(frozen=True)
class ScoredExample:
    message_row_id: int
    label: str
    subtype: str
    scenario: str
    trust_tier: int
    baseline_n: int
    score: float
    features: tuple[FeatureObservation, ...]


@dataclass(frozen=True)
class DistributionRow:
    label: str
    tier: int | None
    count: int
    min_score: float
    median_score: float
    mean_score: float
    max_score: float


@dataclass(frozen=True)
class FeatureFrequencyRow:
    label: str
    feature: str
    fired: int
    total: int
    mean_contribution: float

    @property
    def frequency(self) -> float:
        return self.fired / self.total if self.total else 0.0


@dataclass(frozen=True)
class ThresholdRow:
    threshold: float
    true_positive_rate: float
    false_positive_rate: float
    true_positives: int
    false_positives: int
    malicious_total: int
    benign_total: int


@dataclass(frozen=True)
class CalibrationResult:
    source_name: str
    examples: tuple[ScoredExample, ...]
    distributions: tuple[DistributionRow, ...]
    feature_frequencies: tuple[FeatureFrequencyRow, ...]
    thresholds: tuple[ThresholdRow, ...]
    headline: ThresholdRow

    def mean_score(self, label: str) -> float:
        scores = [e.score for e in self.examples if e.label == label]
        return mean(scores) if scores else 0.0


class CorpusSource(Protocol):
    name: str

    def replay(self, conn: sqlite3.Connection) -> list[CorpusLabel]:
        """Populate conn through ingest_message and return labels for scored mail."""


def make_email(
    from_addr: str,
    *,
    display: str | None = None,
    to: str = ME,
    when: datetime = T0,
    message_id: str,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    body: str = "hi, quick question about the quarterly numbers.",
    html: str | None = None,
    attachments: list[tuple[str, str, bytes]] = (),
    reply_to: str | None = None,
    auth_results: str | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{display} <{from_addr}>" if display else from_addr
    msg["To"] = to
    msg["Subject"] = "synthetic calibration"
    msg["Date"] = format_datetime(when)
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)
    if reply_to:
        msg["Reply-To"] = reply_to
    if auth_results:
        msg["Authentication-Results"] = auth_results
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    for filename, mime, data in attachments:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg


class SyntheticMailbox:
    """Synthetic replay helper mirroring the scoring test fixtures."""

    inbox_folder = 1
    sent_folder = 2

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._uid = {self.inbox_folder: 0, self.sent_folder: 0}
        self._labels: list[CorpusLabel] = []
        self._message_seq = 0

    @property
    def labels(self) -> list[CorpusLabel]:
        return list(self._labels)

    def message_id(self, slug: str) -> str:
        self._message_seq += 1
        return f"<cal-{self._message_seq:04d}-{slug}@nodary.test>"

    def _next_uid(self, folder_id: int) -> int:
        self._uid[folder_id] += 1
        return self._uid[folder_id]

    def deliver(
        self,
        msg: EmailMessage,
        *,
        label: str,
        subtype: str,
        scenario: str,
    ) -> int:
        rec = record_from_message(msg, direction="in", my_addrs=frozenset({ME}))
        row_id = ingest_message(
            self.conn, self.inbox_folder, self._next_uid(self.inbox_folder), rec
        )
        self._labels.append(CorpusLabel(row_id, label, subtype, scenario))
        return row_id

    def send(self, msg: EmailMessage) -> int:
        rec = record_from_message(msg, direction="out", my_addrs=frozenset({ME}))
        return ingest_message(
            self.conn, self.sent_folder, self._next_uid(self.sent_folder), rec
        )

    def reply_to(self, peer: str, original: EmailMessage, when: datetime) -> int:
        return self.send(
            make_email(
                ME,
                to=peer,
                when=when,
                message_id=self.message_id("reply"),
                in_reply_to=original["Message-ID"],
                references=[original["Message-ID"]],
                body="thanks, will do.",
            )
        )

    def establish_contact(
        self,
        peer: str,
        *,
        display: str | None,
        n: int,
        start: datetime = T0,
        every_days: int = 3,
        body: str = "status update as usual, numbers attached below inline.",
        reply: bool = True,
        subtype: str = "baseline",
        scenario: str = "benign_baseline",
    ) -> list[EmailMessage]:
        messages = []
        for i in range(n):
            msg = make_email(
                peer,
                display=display,
                when=start + timedelta(days=i * every_days, minutes=(7 * i) % 90),
                message_id=self.message_id(f"{scenario}-{i}"),
                body=body,
            )
            self.deliver(msg, label="benign", subtype=subtype, scenario=scenario)
            messages.append(msg)
        if reply and messages:
            self.reply_to(
                peer,
                messages[-1],
                when=start + timedelta(days=n * every_days, hours=3),
            )
        return messages


class SyntheticCorpusSource:
    """Corpus source built from the scenarios covered by tests/test_scoring_*."""

    name = "synthetic scoring-fixture corpus"

    def replay(self, conn: sqlite3.Connection) -> list[CorpusLabel]:
        box = SyntheticMailbox(conn)
        self._dana_lookalike(box)
        self._compromised_contact(box)
        self._one_way_vendor(box)
        self._cold_contacts(box)
        conn.commit()
        return box.labels

    def _dana_lookalike(self, box: SyntheticMailbox) -> None:
        last = box.establish_contact(
            "dana.ito@acme-corp.com",
            display="Dana Ito",
            n=10,
            scenario="acme_established_baseline",
        )[-1]
        box.deliver(
            make_email(
                "billing@acme-corp.com",
                display="Acme Billing",
                when=T0 + timedelta(days=37),
                message_id=box.message_id("same-domain-benign"),
                body="invoice summary for the quarter is ready.",
            ),
            label="benign",
            subtype="known_org_text",
            scenario="benign_known_org_sender",
        )
        box.deliver(
            make_email(
                "dana.ito@acme-c0rp.com",
                display="Dana Ito",
                when=T0 + timedelta(days=40),
                message_id=box.message_id("lookalike"),
                body="please review the attached invoice: https://acme-pay.net/inv",
                attachments=[("invoice.zip", "application/zip", b"PK\x03\x04fake")],
            ),
            label="malicious",
            subtype="lookalike_domain_phish",
            scenario="lookalike_domain",
        )
        box.deliver(
            make_email(
                "payroll@acme-corp.com",
                display="Acme Payroll",
                when=T0 + timedelta(days=41),
                message_id=box.message_id("known-org-payload"),
                body="open the new payroll portal: https://payroll-review.net/start",
                attachments=[("portal.pdf", "application/pdf", b"%PDF")],
                reply_to="payroll@acme-payroll.net",
            ),
            label="malicious",
            subtype="known_org_payload",
            scenario="tier1_cold_payload",
        )
        box.reply_to(
            "dana.ito@acme-corp.com", last, when=T0 + timedelta(days=42, hours=2)
        )

    def _compromised_contact(self, box: SyntheticMailbox) -> None:
        box.establish_contact(
            "sam.okafor@partnerfirm.com",
            display="Sam Okafor",
            n=20,
            start=T0 + timedelta(days=2),
            scenario="sam_established_baseline",
        )
        box.deliver(
            make_email(
                "sam.okafor@partnerfirm.com",
                display="Sam Okafor",
                when=T0 + timedelta(days=63, hours=1),
                message_id=box.message_id("sam-normal"),
                body="status update as usual, numbers attached below inline.",
            ),
            label="benign",
            subtype="established_normal",
            scenario="benign_established_followup",
        )
        box.deliver(
            make_email(
                "sam.okafor@partnerfirm.com",
                display="Sam Okafor",
                when=datetime(2026, 3, 20, 3, 12, tzinfo=UTC),
                message_id=box.message_id("sam-takeover"),
                body="urgent - wire details changed, see attached and confirm at "
                "https://secure-docs-verify.net/login",
                attachments=[("payment_details.zip", "application/zip", b"PK\x03\x04x")],
                reply_to="sam.okafor@consultant-mail.net",
            ),
            label="malicious",
            subtype="compromised_contact_behavior_shift",
            scenario="behavior_shift_tier3",
        )

    def _one_way_vendor(self, box: SyntheticMailbox) -> None:
        box.establish_contact(
            "alerts@vendor.io",
            display="Vendor Alerts",
            n=12,
            start=T0 + timedelta(days=4),
            every_days=2,
            body="scheduled report available at https://vendor.io/report",
            reply=False,
            subtype="one_way_baseline",
            scenario="vendor_one_way_baseline",
        )
        box.deliver(
            make_email(
                "alerts@vendor.io",
                display="Vendor Alerts",
                when=T0 + timedelta(days=31, hours=1),
                message_id=box.message_id("vendor-normal"),
                body="scheduled report available at https://vendor.io/report",
            ),
            label="benign",
            subtype="one_way_normal",
            scenario="benign_one_way_followup",
        )
        box.deliver(
            make_email(
                "alerts@vendor.io",
                display="Vendor Alerts",
                when=datetime(2026, 2, 12, 2, 8, tzinfo=UTC),
                message_id=box.message_id("vendor-takeover"),
                body="new secure report: https://vendor-secure-review.net/login",
                attachments=[("secure_report.zip", "application/zip", b"PK\x03\x04v")],
                reply_to="alerts@vendor-review.net",
            ),
            label="malicious",
            subtype="one_way_behavior_shift",
            scenario="behavior_shift_tier2",
        )

    def _cold_contacts(self, box: SyntheticMailbox) -> None:
        cold_cases = [
            (
                "old.friend@somewhere.org",
                "old friend",
                "hey, long time! are you going to the reunion?",
                (),
                None,
                "benign",
                "cold_text",
                "benign_cold_text",
            ),
            (
                "news@community.org",
                "Community News",
                "the agenda is posted at https://community.org/agenda",
                (),
                None,
                "benign",
                "cold_single_link",
                "benign_cold_link",
            ),
            (
                "alex@growthly.io",
                "Alex from Growthly",
                "book a demo: https://growthly.io/deck",
                (("deck.pdf", "application/pdf", b"%PDF"),),
                None,
                "benign",
                "cold_sales_deck",
                "benign_cold_payload",
            ),
            (
                "bd@growthly-mail.net",
                "Alex from Growthly",
                "book a demo: https://calendly-growthly.com/x and "
                "https://growthly.io/deck",
                (("deck.pdf", "application/pdf", b"%PDF"),),
                "alex@growthly-mailer.net",
                "malicious",
                "cold_payload",
                "malicious_cold_payload",
            ),
            (
                "notice@bank-alerts.com",
                "Bank Alerts",
                "account notice attached.",
                (),
                None,
                "malicious",
                "auth_failure",
                "malicious_auth_fail",
            ),
            (
                "ap@payment-review.net",
                '"Acme AP ap@acme-corp.com"',
                "please confirm the transfer queue.",
                (),
                None,
                "malicious",
                "embedded_address_mismatch",
                "malicious_embedded_addr",
            ),
        ]
        auth_fail = (
            "mx.myco.com; spf=fail smtp.mailfrom=bank-alerts.com; "
            "dkim=fail; dmarc=fail"
        )
        for i, (addr, display, body, attachments, reply_to, label, subtype, scenario) in enumerate(
            cold_cases
        ):
            box.deliver(
                make_email(
                    addr,
                    display=display,
                    when=T0 + timedelta(days=70 + i),
                    message_id=box.message_id(scenario),
                    body=body,
                    attachments=list(attachments),
                    reply_to=reply_to,
                    auth_results=auth_fail if subtype == "auth_failure" else None,
                ),
                label=label,
                subtype=subtype,
                scenario=scenario,
            )


def run_calibration(source: CorpusSource | None = None) -> CalibrationResult:
    source = source or SyntheticCorpusSource()
    conn = _empty_calibration_db()
    labels = source.replay(conn)
    examples = tuple(_load_examples(conn, labels))
    distributions = tuple(_distribution_rows(examples))
    features = tuple(_feature_frequency_rows(examples))
    thresholds = tuple(_threshold_rows(examples))
    headline = _choose_headline(thresholds)
    return CalibrationResult(
        source.name,
        examples,
        distributions,
        features,
        thresholds,
        headline,
    )


def render_markdown(result: CalibrationResult) -> str:
    lines = [
        "# Nodary Weight Calibration",
        "",
        f"Corpus: {result.source_name}",
        f"Labeled incoming messages: {len(result.examples)}",
        "",
        _render_headline(result),
        "",
        "## Corpus Mix",
        "",
        "| label | subtype | n | mean score |",
        "|---|---|---:|---:|",
        *_corpus_mix_rows(result.examples),
        "",
        "## Score Distribution",
        "",
        "| label | tier | n | min | median | mean | max |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result.distributions:
        lines.append(
            "| "
            f"{row.label} | {_tier_name(row.tier)} | {row.count} | "
            f"{row.min_score:.1f} | {row.median_score:.1f} | "
            f"{row.mean_score:.1f} | {row.max_score:.1f} |"
        )
    lines.extend(
        [
            "",
            "## Feature Firing",
            "",
            "| label | feature | fired | frequency | mean contribution when fired |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for row in result.feature_frequencies:
        lines.append(
            "| "
            f"{row.label} | `{row.feature}` | {row.fired}/{row.total} | "
            f"{_pct(row.frequency)} | {row.mean_contribution:.1f} |"
        )
    lines.extend(
        [
            "",
            "## Threshold Sweep",
            "",
            "| threshold | TPR | FPR | TP | FP | malicious n | benign n |",
            "|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in result.thresholds:
        lines.append(
            "| "
            f"{row.threshold:.0f} | {_pct(row.true_positive_rate)} | "
            f"{_pct(row.false_positive_rate)} | {row.true_positives} | "
            f"{row.false_positives} | {row.malicious_total} | "
            f"{row.benign_total} |"
        )
    return "\n".join(lines) + "\n"


def _empty_calibration_db() -> sqlite3.Connection:
    conn = connect(":memory:")
    conn.execute(
        "INSERT INTO accounts (id, email, imap_host, auth_method, created_at)"
        " VALUES (1, ?, 'imap.test', 'app_password', 0)",
        (ME,),
    )
    conn.execute(
        "INSERT INTO user_identities (account_id, email_norm) VALUES (1, ?)", (ME,)
    )
    conn.execute(
        "INSERT INTO folders (id, account_id, name, role) VALUES"
        " (1, 1, 'INBOX', 'inbox'), (2, 1, 'Sent', 'sent')"
    )
    conn.commit()
    return conn


def _load_examples(
    conn: sqlite3.Connection, labels: list[CorpusLabel]
) -> list[ScoredExample]:
    out = []
    for label in labels:
        score = conn.execute(
            "SELECT trust_tier_at_scoring, baseline_n, anomaly_score"
            " FROM message_scores WHERE message_id = ?",
            (label.message_row_id,),
        ).fetchone()
        features = tuple(
            FeatureObservation(
                r["feature"],
                r["raw_value"],
                r["contribution"],
            )
            for r in conn.execute(
                "SELECT feature, raw_value, contribution"
                " FROM message_score_features WHERE message_id = ?"
                " ORDER BY feature",
                (label.message_row_id,),
            )
        )
        out.append(
            ScoredExample(
                label.message_row_id,
                label.label,
                label.subtype,
                label.scenario,
                score["trust_tier_at_scoring"],
                score["baseline_n"],
                score["anomaly_score"],
                features,
            )
        )
    return out


def _distribution_rows(examples: tuple[ScoredExample, ...]) -> list[DistributionRow]:
    rows = []
    for label in ("benign", "malicious"):
        rows.append(_distribution_row(label, None, examples))
        tiers = sorted({e.trust_tier for e in examples if e.label == label})
        for tier in tiers:
            rows.append(_distribution_row(label, tier, examples))
    return rows


def _distribution_row(
    label: str,
    tier: int | None,
    examples: tuple[ScoredExample, ...],
) -> DistributionRow:
    scores = [
        e.score
        for e in examples
        if e.label == label and (tier is None or e.trust_tier == tier)
    ]
    return DistributionRow(
        label,
        tier,
        len(scores),
        min(scores) if scores else 0.0,
        median(scores) if scores else 0.0,
        mean(scores) if scores else 0.0,
        max(scores) if scores else 0.0,
    )


def _feature_frequency_rows(
    examples: tuple[ScoredExample, ...],
) -> list[FeatureFrequencyRow]:
    rows = []
    for label in ("benign", "malicious"):
        label_examples = [e for e in examples if e.label == label]
        total = len(label_examples)
        for feature in FEATURES:
            contributions = [
                obs.contribution
                for e in label_examples
                for obs in e.features
                if obs.feature == feature
            ]
            rows.append(
                FeatureFrequencyRow(
                    label,
                    feature,
                    len(contributions),
                    total,
                    mean(contributions) if contributions else 0.0,
                )
            )
    return rows


def _threshold_rows(examples: tuple[ScoredExample, ...]) -> list[ThresholdRow]:
    malicious = [e for e in examples if e.label == "malicious"]
    benign = [e for e in examples if e.label == "benign"]
    rows = []
    for threshold in THRESHOLDS:
        tp = sum(1 for e in malicious if e.score >= threshold)
        fp = sum(1 for e in benign if e.score >= threshold)
        rows.append(
            ThresholdRow(
                float(threshold),
                tp / len(malicious) if malicious else 0.0,
                fp / len(benign) if benign else 0.0,
                tp,
                fp,
                len(malicious),
                len(benign),
            )
        )
    return rows


def _choose_headline(rows: tuple[ThresholdRow, ...]) -> ThresholdRow:
    low_fp = [r for r in rows if r.false_positive_rate <= 0.05]
    if low_fp:
        return max(low_fp, key=lambda r: (r.true_positive_rate, -r.threshold))
    return max(rows, key=lambda r: (r.true_positive_rate - r.false_positive_rate, -r.threshold))


def _render_headline(result: CalibrationResult) -> str:
    row = result.headline
    benign_mean = result.mean_score("benign")
    malicious_mean = result.mean_score("malicious")
    return (
        f"Headline: at threshold {row.threshold:.0f}, caught "
        f"{_pct(row.true_positive_rate)} of malicious messages "
        f"({row.true_positives}/{row.malicious_total}) with "
        f"{_pct(row.false_positive_rate)} benign false alarms "
        f"({row.false_positives}/{row.benign_total}). Mean scores: "
        f"benign {benign_mean:.1f}, malicious {malicious_mean:.1f}."
    )


def _corpus_mix_rows(examples: tuple[ScoredExample, ...]) -> list[str]:
    rows = []
    keys = sorted({(e.label, e.subtype) for e in examples})
    for label, subtype in keys:
        scores = [
            e.score for e in examples if e.label == label and e.subtype == subtype
        ]
        rows.append(f"| {label} | `{subtype}` | {len(scores)} | {mean(scores):.1f} |")
    return rows


def _tier_name(tier: int | None) -> str:
    if tier is None:
        return "all"
    return f"T{tier} {TIER_LABELS[tier]}"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"
